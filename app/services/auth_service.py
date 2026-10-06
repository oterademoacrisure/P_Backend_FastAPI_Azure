"""
Login and token verification for the PayerIQ frontend.

Looks up a username in Cosmos DB's UserCredential container (a *different*
database from the one backing the LangGraph checkpointer / document-history
log -- see AUTH_DATABASE_NAME below), verifies the submitted password against
the stored bcrypt hash, and issues a short-lived JWT the frontend attaches as
a Bearer token to every subsequent /v2 call. require_auth() is the FastAPI
dependency that validates that token on the way back in.

This intentionally does not check credentials in the browser: doing so would
require shipping a Cosmos DB key into the frontend JS bundle (readable by
anyone via dev tools, and granting far more than login-check access) and
comparing bcrypt hashes client-side, which defeats the point of hashing them.
Login must be verified server-side, which is what this module is for.

Requires env vars:
    AZURE_COSMOS_ENDPOINT, AZURE_COSMOS_KEY   (same Cosmos account as
        document_history_service.py / cosmos_checkpoint.py -- just a
        different database within it, see AUTH_DATABASE_NAME)
    AUTH_JWT_SECRET                            (signs/verifies issued tokens)
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import bcrypt
import jwt
from azure.cosmos.aio import CosmosClient
from azure.cosmos import exceptions
from dotenv import load_dotenv

from app.services import project_registry

# Defensive, not redundant: app/main.py also calls load_dotenv(), but only
# after importing app.auth_router -> app.services.auth_service (this
# module), so relying on main.py's call would read an empty environment the
# first time this module's top-level os.getenv() calls below actually run.
# Every other service module in this codebase (document_history_service.py,
# azure_search_service.py) calls load_dotenv() itself for the same reason.
load_dotenv()

COSMOS_ENDPOINT = os.getenv("AZURE_COSMOS_ENDPOINT", "")
COSMOS_KEY = os.getenv("AZURE_COSMOS_KEY", "")

# Deliberately its own database, separate from AZURE_COSMOS_DATABASE_NAME
# (default "PayerIQ", used for Checkpoints/DocumentHistory) -- user accounts
# live in a "payeriqdb" database in the same Cosmos account instead.
AUTH_DATABASE_NAME = os.getenv("AZURE_COSMOS_AUTH_DATABASE_NAME", "payeriqdb")
AUTH_CONTAINER_NAME = os.getenv("AZURE_COSMOS_USER_CONTAINER_NAME", "UserCredential")

JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", str(8 * 60 * 60)))  # 8 hours


class AuthError(Exception):
    """Raised for any login failure -- bad username, bad password, inactive
    account, or missing configuration. The router maps this to a single
    generic 401, never distinguishing "unknown user" from "wrong password"
    in the response, so a login attempt can't be used to enumerate valid
    usernames."""


@dataclass
class LoginResult:
    token: str
    username: str
    role: str
    tenant_id: str


def _require_configured() -> None:
    if not (COSMOS_ENDPOINT and COSMOS_KEY):
        raise RuntimeError(
            "AZURE_COSMOS_ENDPOINT / AZURE_COSMOS_KEY are not set -- login cannot "
            "look up user credentials without Cosmos DB."
        )
    if not JWT_SECRET:
        raise RuntimeError(
            "AUTH_JWT_SECRET is not set -- refusing to issue unsigned/insecurely "
            "signed login tokens."
        )


async def _get_user_by_username(username: str) -> dict | None:
    """Cross-partition query rather than a point read by id: UserCredential's
    partition key isn't assumed to be /username (or even /id) here, and this
    container is small enough (user accounts, not per-request data) that the
    scan cost is irrelevant."""
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        container = (
            client.get_database_client(AUTH_DATABASE_NAME)
            .get_container_client(AUTH_CONTAINER_NAME)
        )
        query = "SELECT * FROM c WHERE c.username = @username"
        parameters = [{"name": "@username", "value": username}]
        # No partition_key given -> the SDK already queries across all
        # partitions by default; enable_cross_partition_query is a legacy
        # kwarg the installed azure-cosmos version (see requirements.txt)
        # doesn't consume internally -- passing it leaks straight through to
        # aiohttp's ClientSession.request(), which rejects it outright.
        async for item in container.query_items(query=query, parameters=parameters):
            return item
        return None


async def _touch_last_login(user: dict) -> None:
    """Best-effort -- a failure here must never block a successful login."""
    try:
        async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
            container = (
                client.get_database_client(AUTH_DATABASE_NAME)
                .get_container_client(AUTH_CONTAINER_NAME)
            )
            user["lastLogin"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            await container.upsert_item(user)
    except exceptions.CosmosHttpResponseError:
        pass


def _issue_token(user: dict) -> str:
    now = int(time.time())
    payload = {
        "sub": user["username"],
        "role": user.get("role", "user"),
        "tenantId": user.get("tenantId", "default"),
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def login(username: str, password: str) -> LoginResult:
    _require_configured()

    if not username or not password:
        raise AuthError("Username and password are required.")

    user = await _get_user_by_username(username)
    if user is None:
        raise AuthError("Invalid username or password.")

    if not user.get("isActive", True):
        raise AuthError("This account is disabled.")

    stored_hash = user.get("passwordHash", "")
    try:
        valid = bool(stored_hash) and bcrypt.checkpw(
            password.encode("utf-8"), stored_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        # Malformed stored hash -- treat exactly like a wrong password rather
        # than leaking a 500 that would reveal the account exists.
        valid = False

    if not valid:
        raise AuthError("Invalid username or password.")

    await _touch_last_login(user)

    token = _issue_token(user)
    return LoginResult(
        token=token,
        username=user["username"],
        role=user.get("role", "user"),
        tenant_id=user.get("tenantId", "default"),
    )


class MappingExistsError(Exception):
    """Raised by save_user_mapping() when the same UserName is already mapped
    to the same ClientID/ProjectID -- the admin router maps this to a 409."""


# The Register user page only creates ordinary users -- admin is a fixed
# account set up directly in Cosmos DB, so no one can be made an admin
# through the API.
ALLOWED_ROLES = {"user"}

# Where the admin "Register user" page's mappings are stored. Defaults to the
# UserCredential container, alongside the login documents -- mapping
# documents are told apart by their "type" field and never carry a
# "username" field, so _get_user_by_username() can't match one.
MAPPING_CONTAINER_NAME = os.getenv("AZURE_COSMOS_USER_MAPPING_CONTAINER_NAME", AUTH_CONTAINER_NAME)


def _mapping_container(client: CosmosClient):
    return client.get_database_client(AUTH_DATABASE_NAME).get_container_client(MAPPING_CONTAINER_NAME)


async def _get_mapping(user_name: str, client_id: str, project_id: str) -> dict | None:
    """The user's existing mapping to the same registered project, however
    it was spelled when saved -- a mapping stored as "Payment Integrity"
    before the registry existed is the same project as "payment-integrity"."""
    target = project_registry.resolve(client_id, project_id)
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        query = "SELECT * FROM c WHERE c.type = 'userMapping' AND LOWER(c.UserName) = LOWER(@u)"
        parameters = [{"name": "@u", "value": user_name}]
        async for item in _mapping_container(client).query_items(query=query, parameters=parameters):
            if target and project_registry.resolve(item.get("ClientID"), item.get("ProjectID")) == target:
                return item
        return None


async def _insert_mapping(mapping: dict) -> None:
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        await _mapping_container(client).create_item(mapping)


MIN_PASSWORD_LENGTH = 8


def _hash_password(password: str) -> str:
    # Same bcrypt settings as generatepwd.py, and what login() verifies against.
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


async def _create_login(user: dict) -> None:
    """create_item, not upsert_item, so a race with another registration of
    the same username can't silently overwrite that account."""
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        container = client.get_database_client(AUTH_DATABASE_NAME).get_container_client(AUTH_CONTAINER_NAME)
        await container.create_item(user)


async def save_user_mapping(
    *,
    user_name: str,
    role: str,
    client_id: str,
    project_id: str,
    created_by: str,
    password: str = "",
) -> dict:
    """Saves one UserName -> Role/ClientID/ProjectID mapping. A user can be
    mapped to several client/project pairs, but the same pair only once.

    Also manages the login the admin hands to the user:
      - UserName has no login yet: `password` is required, and a login is
        created (same document shape as generatepwd.py) with `role`.
      - UserName already has a login: `password` must be blank -- there is
        deliberately no way to change an existing password from here. The
        login (password and role) is left unchanged.
    The password itself is only ever stored as a bcrypt hash on the login
    document, never on the mapping."""
    if not (COSMOS_ENDPOINT and COSMOS_KEY):
        raise RuntimeError("AZURE_COSMOS_ENDPOINT / AZURE_COSMOS_KEY are not set.")

    if not (user_name and client_id and project_id):
        raise ValueError("UserName, ClientID and ProjectID are required.")
    project = project_registry.resolve(client_id, project_id)
    if project is None:
        raise ValueError(
            f"Unknown client/project {client_id!r} / {project_id!r}. "
            "Add it to app/config/projects.json first."
        )
    # Always store the registry's ids, whatever spelling was sent.
    client_id, project_id = project.client_id, project.project_id
    if role.lower() not in ALLOWED_ROLES:
        raise ValueError("Role must be User -- admin accounts can't be created here.")
    if password and len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    if await _get_mapping(user_name, client_id, project_id) is not None:
        raise MappingExistsError(
            f'"{user_name}" is already mapped to {client_id} / {project_id}.'
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    login = await _get_user_by_username(user_name)
    if login is None:
        if not password:
            raise ValueError(f'"{user_name}" has no login yet -- enter a password to create one.')
        try:
            await _create_login({
                "id": user_name,
                "username": user_name,
                "passwordHash": _hash_password(password),
                "role": role.lower(),
                "tenantId": "default",
                "createdAt": now,
                "createdBy": created_by,
                "lastLogin": None,
                "isActive": True,
            })
        except exceptions.CosmosResourceExistsError:
            raise MappingExistsError(f'A login for "{user_name}" was just created by someone else -- try again.')
        login_action = "created"
    elif password:
        raise ValueError(
            f'"{user_name}" already has a login and its password cannot be changed here -- '
            "leave Password blank to just add this project."
        )
    else:
        login_action = "unchanged"

    mapping = {
        "id": str(uuid.uuid4()),
        "type": "userMapping",
        "UserName": user_name,
        "Role": role,
        "ClientID": client_id,
        "ProjectID": project_id,
        "createdAt": now,
        "createdBy": created_by,
    }
    await _insert_mapping(mapping)
    return {**mapping, "login": login_action}


async def get_user_mappings(username: str) -> list[dict]:
    """Every client/project an admin has mapped this login username to on
    the "Register user" page. Case-insensitive on UserName, since the admin
    types it by hand."""
    if not (COSMOS_ENDPOINT and COSMOS_KEY):
        raise RuntimeError("AZURE_COSMOS_ENDPOINT / AZURE_COSMOS_KEY are not set.")
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        query = (
            "SELECT c.ClientID, c.ProjectID, c.Role FROM c "
            "WHERE c.type = 'userMapping' AND LOWER(c.UserName) = LOWER(@u)"
        )
        parameters = [{"name": "@u", "value": username}]
        return [
            item
            async for item in _mapping_container(client).query_items(query=query, parameters=parameters)
        ]


async def _query_all_mappings() -> list[dict]:
    async with CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY) as client:
        query = (
            "SELECT c.UserName, c.Role, c.ClientID, c.ProjectID, c.createdAt, c.createdBy "
            "FROM c WHERE c.type = 'userMapping'"
        )
        return [item async for item in _mapping_container(client).query_items(query=query)]


async def list_user_mappings(project_query: str = "") -> list[dict]:
    """Every user -> client/project mapping, for the admin page's user list,
    optionally only those whose project id or name contains `project_query`
    (case-insensitive, so "payment" finds payment-integrity). Ids are shown
    as the registry spells them, so a mapping saved as "Payment Integrity"
    before the registry existed lists as payment-integrity. A mapping whose
    project is no longer registered (e.g. the removed medical-claims) keeps
    its stored ids and is flagged "registered": False -- that user can no
    longer generate for it and should be reassigned."""
    if not (COSMOS_ENDPOINT and COSMOS_KEY):
        raise RuntimeError("AZURE_COSMOS_ENDPOINT / AZURE_COSMOS_KEY are not set.")
    needle = (project_query or "").strip().lower()
    rows = []
    for item in await _query_all_mappings():
        project = project_registry.resolve(item.get("ClientID"), item.get("ProjectID"))
        row = {
            "UserName": item.get("UserName", ""),
            "Role": item.get("Role", ""),
            "ClientID": project.client_id if project else item.get("ClientID", ""),
            "ProjectID": project.project_id if project else item.get("ProjectID", ""),
            "projectName": project.project_name if project else item.get("ProjectID", ""),
            "registered": project is not None,
            "createdAt": item.get("createdAt", ""),
            "createdBy": item.get("createdBy", ""),
        }
        if needle and needle not in row["ProjectID"].lower() and needle not in row["projectName"].lower():
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["ClientID"].lower(), r["ProjectID"].lower(), r["UserName"].lower()))
    return rows


# Login roles that get admin rights (the Register user page, every
# project). Existing admin accounts were created with role "superuser", new
# ones from the Register page get "admin". Comma-separated, case-insensitive.
ADMIN_ROLES = {
    r.strip().lower()
    for r in os.getenv("AUTH_ADMIN_ROLES", "admin,superuser").split(",")
    if r.strip()
}


def is_admin_role(role: str) -> bool:
    return str(role or "").strip().lower() in ADMIN_ROLES


def is_admin(claims: dict) -> bool:
    return is_admin_role(claims.get("role", ""))


async def has_project_access(claims: dict, client_id: str, project_id: str) -> bool:
    """Admins can use every registered project; anyone else only the
    client/project pairs they've been mapped to. Both sides are resolved
    through project_registry, so a mapping saved with display names
    ("Excellus" / "Payment Integrity") matches a request sent with ids
    ("excellus" / "payment-integrity") -- and an unregistered project is
    never accessible, not even to an admin."""
    requested = project_registry.resolve(client_id, project_id)
    if requested is None:
        return False
    if is_admin(claims):
        return True
    mappings = await get_user_mappings(claims.get("sub", ""))
    return any(
        project_registry.resolve(m.get("ClientID"), m.get("ProjectID")) == requested for m in mappings
    )


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError (ExpiredSignatureError, InvalidTokenError, ...)
    on any invalid/expired/malformed token -- callers should catch the base
    jwt.PyJWTError and turn it into a 401."""
    if not JWT_SECRET:
        raise RuntimeError("AUTH_JWT_SECRET is not set -- cannot verify tokens.")
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
