"""
POST /v2/auth/login -- verifies a username/password against Cosmos DB's
UserCredential container (app/services/auth_service.py) and issues a JWT.

Also exports require_auth(), a FastAPI dependency that validates that JWT on
the way back in -- wired onto /v2/generate, /v2/refine, and /v2/status in
routergenerator.py so a login is actually enforced server-side, not just
gating the frontend's UI.

Mounted from app/main.py at prefix "/v2/auth", matching the frontend's
BASE_URL + "/auth/login" (see Payeriq-Frontend/src/utils/api.js).
"""

from __future__ import annotations

import logging

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.services import auth_service, project_registry

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(payload: LoginRequest):
    try:
        result = await auth_service.login(payload.username, payload.password)
    except auth_service.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except RuntimeError as e:
        logger.error("Login is misconfigured: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Login is not available (authentication service misconfigured).",
        )
    except Exception as e:
        logger.exception("Unhandled error in /v2/auth/login")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "token": result.token,
        "username": result.username,
        "role": result.role,
        "tenantId": result.tenant_id,
        # The frontend shows the Admin button / Register page from this rather
        # than comparing role itself, so which roles count as admin is decided
        # in one place (auth_service.ADMIN_ROLES).
        "isAdmin": auth_service.is_admin_role(result.role),
    }


async def require_auth(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency guarding a protected /v2 route. Raises 401 on a
    missing header, a malformed one, or a token that fails to decode/verify
    (expired, wrong signature, tampered) -- never distinguishes which, so a
    request against a protected endpoint can't be used to probe token
    validity beyond "accepted" or "rejected"."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return auth_service.decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    except RuntimeError as e:
        # AUTH_JWT_SECRET missing -- a configuration problem, not a bad
        # token, so this must not be reported the same way a rejected login
        # attempt would be.
        logger.error("Auth verification is misconfigured: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Authentication is not available (service misconfigured).",
        )


async def require_admin(claims: dict = Depends(require_auth)) -> dict:
    """require_auth() plus a role check -- the frontend only hides the admin
    page for non-admins, so this is what actually stops a regular user's
    token from calling an admin endpoint directly."""
    if not auth_service.is_admin(claims):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return claims


async def check_project_access(claims: dict, client_id: str, project_id: str) -> None:
    """Raises 403 unless the caller is an admin or has been mapped to this
    client/project on the admin "Register user" page. Looked up on every
    call rather than baked into the token, so adding or removing a mapping
    takes effect immediately."""
    try:
        allowed = await auth_service.has_project_access(claims, client_id, project_id)
    except RuntimeError as e:
        logger.error("Project access check is misconfigured: %s", e)
        raise HTTPException(status_code=503, detail="Project access check is not available.")
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"You don't have access to {client_id} / {project_id}. Ask an admin to assign it to you.",
        )


@router.get("/me/projects")
async def my_projects(claims: dict = Depends(require_auth)):
    """The client/project pairs assigned to the logged-in user on the admin
    "Register user" page. The main page sends the first one with every
    generate call (no project picker there). allProjects is true for
    admins, whom check_project_access lets use any project."""
    try:
        mappings = await auth_service.get_user_mappings(claims.get("sub", ""))
    except RuntimeError as e:
        logger.error("Project lookup is misconfigured: %s", e)
        raise HTTPException(status_code=503, detail="Project lookup is not available.")
    # Resolved through the registry: the frontend gets stable ids to send
    # back plus display names to show, whichever spelling the mapping was
    # saved with. A mapping to a project no longer in the registry is left
    # out rather than offered and then refused.
    projects = []
    for m in mappings:
        project = project_registry.resolve(m.get("ClientID"), m.get("ProjectID"))
        if project is None:
            logger.warning("Mapping %s / %s for %s is not in the project registry", m.get("ClientID"), m.get("ProjectID"), claims.get("sub"))
        elif project.as_dict() not in projects:
            projects.append(project.as_dict())
    return {"allProjects": auth_service.is_admin(claims), "projects": projects}


__all__ = ["router", "require_auth", "require_admin", "check_project_access"]
