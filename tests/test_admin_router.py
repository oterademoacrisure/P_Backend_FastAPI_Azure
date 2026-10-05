"""
Tests for POST /v2/admin/users (app/admin_router.py) and
auth_service.save_user_mapping(). Cosmos DB calls (_get_mapping,
_insert_mapping, _get_user_by_username, _create_login) are monkeypatched, so
nothing here touches a real database.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import bcrypt
import jwt
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import auth_service

client = TestClient(main.app)

# Exact body AdminRegisterPage.jsx sends on OK.
BODY = {
    "UserName": "jdoe",
    "Role": "User",
    "ClientID": "excellus",
    "ProjectID": "payment-integrity",
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(auth_service, "COSMOS_ENDPOINT", "https://fake.example.com:443/")
    monkeypatch.setattr(auth_service, "COSMOS_KEY", "fake-key")
    monkeypatch.setattr(auth_service, "JWT_SECRET", "test-only-secret")


EXISTING_LOGIN = {"id": "jdoe", "username": "jdoe", "passwordHash": "old-hash", "role": "user"}


@pytest.fixture
def cosmos(monkeypatch):
    """Defaults to UserName "jdoe" already having a login; tests for a new
    user override _get_user_by_username to return None."""
    insert = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "_get_mapping", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_service, "_insert_mapping", insert)
    monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=dict(EXISTING_LOGIN)))
    monkeypatch.setattr(auth_service, "_create_login", AsyncMock(return_value=None))
    return insert


def _auth(role: str) -> dict:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "boss", "role": role, "tenantId": "default", "iat": now, "exp": now + 3600},
        auth_service.JWT_SECRET,
        algorithm=auth_service.JWT_ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


class TestAdminGuard:
    def test_no_token_is_401(self, cosmos):
        r = client.post("/v2/admin/users", json=BODY)
        assert r.status_code == 401
        cosmos.assert_not_called()

    def test_non_admin_token_is_403(self, cosmos):
        r = client.post("/v2/admin/users", json=BODY, headers=_auth("user"))
        assert r.status_code == 403
        cosmos.assert_not_called()


class TestSaveUserMapping:
    def test_admin_saves_mapping(self, cosmos):
        r = client.post("/v2/admin/users", json=BODY, headers=_auth("admin"))

        assert r.status_code == 201
        stored = cosmos.await_args.args[0]
        assert {k: stored[k] for k in BODY} == BODY
        assert stored["type"] == "userMapping"
        assert stored["createdBy"] == "boss"
        # Must not look like a login document to _get_user_by_username().
        assert "username" not in stored and "passwordHash" not in stored
        assert r.json()["login"] == "unchanged"
        auth_service._create_login.assert_not_called()

    def test_new_user_gets_login_with_hashed_password(self, cosmos, monkeypatch):
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=None))
        r = client.post(
            "/v2/admin/users", json={**BODY, "UserName": "newbie", "Password": "Welcome#2026"}, headers=_auth("admin")
        )

        assert r.status_code == 201
        assert r.json()["login"] == "created"
        login = auth_service._create_login.await_args.args[0]
        assert login["id"] == login["username"] == "newbie"
        assert login["role"] == "user" and login["isActive"] is True
        assert bcrypt.checkpw(b"Welcome#2026", login["passwordHash"].encode())
        mapping = cosmos.await_args.args[0]
        assert "Password" not in mapping and "passwordHash" not in mapping
        assert "Password" not in r.json()

    def test_new_user_without_password_is_400(self, cosmos, monkeypatch):
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=None))
        r = client.post("/v2/admin/users", json=BODY, headers=_auth("admin"))
        assert r.status_code == 400
        assert "password" in r.json()["detail"].lower()
        cosmos.assert_not_called()

    def test_short_password_is_400(self, cosmos, monkeypatch):
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=None))
        r = client.post("/v2/admin/users", json={**BODY, "Password": "short"}, headers=_auth("admin"))
        assert r.status_code == 400
        cosmos.assert_not_called()

    def test_existing_user_password_cannot_be_changed(self, cosmos):
        r = client.post("/v2/admin/users", json={**BODY, "Password": "Another#2026"}, headers=_auth("admin"))
        assert r.status_code == 400
        cosmos.assert_not_called()
        auth_service._create_login.assert_not_called()

    def test_duplicate_mapping_is_409(self, cosmos, monkeypatch):
        monkeypatch.setattr(auth_service, "_get_mapping", AsyncMock(return_value={"id": "x"}))
        r = client.post("/v2/admin/users", json=BODY, headers=_auth("admin"))
        assert r.status_code == 409
        cosmos.assert_not_called()

    def test_unknown_role_is_400(self, cosmos):
        r = client.post("/v2/admin/users", json={**BODY, "Role": "superuser"}, headers=_auth("admin"))
        assert r.status_code == 400
        cosmos.assert_not_called()

    def test_blank_user_name_is_400(self, cosmos):
        r = client.post("/v2/admin/users", json={**BODY, "UserName": "   "}, headers=_auth("admin"))
        assert r.status_code == 400
        cosmos.assert_not_called()

    def test_missing_field_is_422(self, cosmos):
        body = {k: v for k, v in BODY.items() if k != "ProjectID"}
        r = client.post("/v2/admin/users", json=body, headers=_auth("admin"))
        assert r.status_code == 422


class TestAdminRoles:
    def test_superuser_counts_as_admin(self, cosmos):
        r = client.post("/v2/admin/users", json=BODY, headers=_auth("superuser"))
        assert r.status_code == 201

    def test_cannot_create_an_admin(self, cosmos):
        r = client.post("/v2/admin/users", json={**BODY, "Role": "Admin"}, headers=_auth("admin"))
        assert r.status_code == 400
        cosmos.assert_not_called()

    def test_role_check_is_case_insensitive(self):
        assert auth_service.is_admin_role("Admin")
        assert not auth_service.is_admin_role("user")
