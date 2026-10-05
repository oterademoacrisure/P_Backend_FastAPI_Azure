"""
POST /v2/admin/users -- saves a UserName -> Role/ClientID/ProjectID mapping
from the frontend's admin "Register user" page
(Payeriq-Frontend/src/pages/AdminRegisterPage.jsx, request sent by
registerUserViaBackend in src/utils/api.js). For a UserName with no login yet
it also creates one with the password the admin enters -- see
auth_service.save_user_mapping().

Every route here is guarded by require_admin, so a token whose role isn't
"admin" gets a 403 even if it calls the endpoint directly.

Mounted from app/main.py at prefix "/v2/admin".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth_router import require_admin
from app.services import auth_service, project_registry

logger = logging.getLogger(__name__)
router = APIRouter()


class UserMappingRequest(BaseModel):
    # Field names match the JSON body the frontend sends (its EMPTY_FORM keys).
    UserName: str = Field(min_length=1, max_length=200)
    Role: str = Field(min_length=1, max_length=20)
    ClientID: str = Field(min_length=1, max_length=100)
    ProjectID: str = Field(min_length=1, max_length=100)
    # Required when UserName has no login yet; must be blank for one that
    # already has a login. See auth_service.save_user_mapping().
    Password: str = Field(default="", max_length=200)


@router.get("/projects")
async def list_projects(admin: dict = Depends(require_admin)):
    """The registered clients and their projects (app/config/projects.json),
    for the Register user page's ClientID / ProjectID dropdowns -- the same
    list the backend validates against, so the page can't offer a project
    the backend would reject."""
    return {"clients": project_registry.clients()}


@router.post("/users", status_code=201)
async def save_user_mapping(payload: UserMappingRequest, admin: dict = Depends(require_admin)):
    try:
        return await auth_service.save_user_mapping(
            user_name=payload.UserName.strip(),
            role=payload.Role,
            client_id=payload.ClientID,
            project_id=payload.ProjectID,
            created_by=admin.get("sub", ""),
            password=payload.Password,
        )
    except auth_service.MappingExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error("User mapping is misconfigured: %s", e)
        raise HTTPException(
            status_code=503,
            detail="User mapping is not available (service misconfigured).",
        )
    except Exception as e:
        logger.exception("Unhandled error in /v2/admin/users")
        raise HTTPException(status_code=500, detail=str(e))


__all__ = ["router"]
