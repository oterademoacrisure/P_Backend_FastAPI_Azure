"""
Integration tests for the /v2/auth/login endpoint and the require_auth()
dependency guarding /v2/generate, /v2/refine, and /v2/status.

Uses FastAPI's TestClient against the real app (app.main.app) so routing,
dependency wiring, and status-code mapping are exercised end to end --
but auth_service.login() itself is monkeypatched per-test so nothing here
makes a real Cosmos DB call. See tests/test_auth_service.py for coverage of
login()'s own logic (bcrypt check, Cosmos lookup, token issuance).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import auth_service

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch):
    monkeypatch.setattr(auth_service, "JWT_SECRET", "test-only-secret")


def _make_token(**overrides) -> str:
    now = int(time.time())
    payload = {
        "sub": "admin",
        "role": "superuser",
        "tenantId": "default",
        "iat": now,
        "exp": now + 3600,
        **overrides,
    }
    return jwt.encode(payload, auth_service.JWT_SECRET, algorithm=auth_service.JWT_ALGORITHM)


class TestLoginEndpoint:
    def test_successful_login_returns_token_shape_the_frontend_expects(self, monkeypatch):
        monkeypatch.setattr(
            auth_service,
            "login",
            AsyncMock(
                return_value=auth_service.LoginResult(
                    token="signed.jwt.token",
                    username="admin",
                    role="superuser",
                    tenant_id="default",
                )
            ),
        )

        r = client.post("/v2/auth/login", json={"username": "admin", "password": "Admin@123"})

        assert r.status_code == 200
        body = r.json()
        # Exact shape Payeriq-Frontend/src/utils/api.js's loginViaBackend() reads.
        assert body == {
            "token": "signed.jwt.token",
            "username": "admin",
            "role": "superuser",
            "tenantId": "default",
        }

    def test_bad_credentials_return_401_not_500(self, monkeypatch):
        monkeypatch.setattr(
            auth_service, "login", AsyncMock(side_effect=auth_service.AuthError("Invalid username or password."))
        )

        r = client.post("/v2/auth/login", json={"username": "admin", "password": "wrong"})

        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid username or password."}

    def test_missing_cosmos_config_returns_503_not_500(self, monkeypatch):
        monkeypatch.setattr(
            auth_service,
            "login",
            AsyncMock(side_effect=RuntimeError("AZURE_COSMOS_ENDPOINT / AZURE_COSMOS_KEY are not set")),
        )

        r = client.post("/v2/auth/login", json={"username": "admin", "password": "x"})

        assert r.status_code == 503

    def test_missing_fields_return_422(self):
        r = client.post("/v2/auth/login", json={"username": "admin"})
        assert r.status_code == 422


class TestRequireAuthGating:
    """All three assertions target /v2/status since it needs no multipart
    body, but the same dependency guards /v2/generate and /v2/refine too."""

    def test_missing_authorization_header_is_401(self):
        r = client.get("/v2/status/some-session?output_format=STTM")
        assert r.status_code == 401

    def test_non_bearer_scheme_is_401(self):
        r = client.get(
            "/v2/status/some-session?output_format=STTM",
            headers={"Authorization": "Token abc123"},
        )
        assert r.status_code == 401

    def test_garbage_token_is_401(self):
        r = client.get(
            "/v2/status/some-session?output_format=STTM",
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
        assert r.status_code == 401

    def test_expired_token_is_401(self):
        token = _make_token(iat=int(time.time()) - 100, exp=int(time.time()) - 1)
        r = client.get(
            "/v2/status/some-session?output_format=STTM",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_token_signed_with_wrong_secret_is_401(self):
        forged = jwt.encode({"sub": "admin"}, "not-the-real-secret", algorithm="HS256")
        r = client.get(
            "/v2/status/some-session?output_format=STTM",
            headers={"Authorization": f"Bearer {forged}"},
        )
        assert r.status_code == 401

    def test_valid_token_clears_the_auth_gate(self):
        """A valid token must never itself produce a 401 -- whatever happens
        next (404/503/whatever, depending on whether the LangGraph pipeline
        is initialized in this test process) is a different concern from
        authentication."""
        token = _make_token()
        r = client.get(
            "/v2/status/some-session?output_format=STTM",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code != 401

    def test_missing_jwt_secret_is_503_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(auth_service, "JWT_SECRET", "")
        r = client.get(
            "/v2/status/some-session?output_format=STTM",
            headers={"Authorization": "Bearer whatever"},
        )
        assert r.status_code == 503
