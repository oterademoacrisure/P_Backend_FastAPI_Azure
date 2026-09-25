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
from dataclasses import dataclass
from datetime import datetime, timezone

import bcrypt
import jwt
from azure.cosmos.aio import CosmosClient
from azure.cosmos import exceptions
from dotenv import load_dotenv

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


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError (ExpiredSignatureError, InvalidTokenError, ...)
    on any invalid/expired/malformed token -- callers should catch the base
    jwt.PyJWTError and turn it into a 401."""
    if not JWT_SECRET:
        raise RuntimeError("AUTH_JWT_SECRET is not set -- cannot verify tokens.")
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
