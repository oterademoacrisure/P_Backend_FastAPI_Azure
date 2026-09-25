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

from app.services import auth_service

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


__all__ = ["router", "require_auth"]
