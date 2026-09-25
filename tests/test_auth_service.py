"""
Unit tests for app/services/auth_service.py.

Never talks to a real Cosmos DB account: _get_user_by_username() and
_touch_last_login() are monkeypatched to return/accept plain dicts, since
this module's own job (bcrypt verification, token issuance/verification,
error mapping) is independent of the actual Cosmos wire calls -- those are
exercised manually against the real account instead (see README section 17).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import bcrypt
import jwt
import pytest

from app.services import auth_service


@pytest.fixture
def real_password() -> str:
    return "correct horse battery staple"


@pytest.fixture
def fake_user(real_password) -> dict:
    hashed = bcrypt.hashpw(real_password.encode("utf-8"), bcrypt.gensalt(rounds=4))
    return {
        "id": "admin",
        "username": "admin",
        "passwordHash": hashed.decode("utf-8"),
        "role": "superuser",
        "tenantId": "default",
        "isActive": True,
    }


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """Every test gets a 'fully configured' auth_service by default --
    individual tests override these back to '' to exercise the
    not-configured path."""
    monkeypatch.setattr(auth_service, "COSMOS_ENDPOINT", "https://fake.example.com:443/")
    monkeypatch.setattr(auth_service, "COSMOS_KEY", "fake-key")
    monkeypatch.setattr(auth_service, "JWT_SECRET", "test-only-secret")


class TestRequireConfigured:
    def test_raises_when_cosmos_endpoint_missing(self, monkeypatch):
        monkeypatch.setattr(auth_service, "COSMOS_ENDPOINT", "")
        with pytest.raises(RuntimeError, match="AZURE_COSMOS_ENDPOINT"):
            auth_service._require_configured()

    def test_raises_when_jwt_secret_missing(self, monkeypatch):
        monkeypatch.setattr(auth_service, "JWT_SECRET", "")
        with pytest.raises(RuntimeError, match="AUTH_JWT_SECRET"):
            auth_service._require_configured()

    def test_passes_when_fully_configured(self):
        auth_service._require_configured()  # must not raise


class TestLogin:
    async def test_missing_username_or_password_rejected(self):
        with pytest.raises(auth_service.AuthError):
            await auth_service.login("", "something")
        with pytest.raises(auth_service.AuthError):
            await auth_service.login("admin", "")

    async def test_unconfigured_raises_runtime_error_not_auth_error(self, monkeypatch):
        monkeypatch.setattr(auth_service, "JWT_SECRET", "")
        with pytest.raises(RuntimeError):
            await auth_service.login("admin", "whatever")

    async def test_unknown_user_rejected(self, monkeypatch):
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=None))
        with pytest.raises(auth_service.AuthError, match="Invalid username or password"):
            await auth_service.login("nobody", "whatever")

    async def test_wrong_password_rejected(self, monkeypatch, fake_user):
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=fake_user))
        with pytest.raises(auth_service.AuthError, match="Invalid username or password"):
            await auth_service.login("admin", "definitely-wrong")

    async def test_inactive_account_rejected_even_with_correct_password(
        self, monkeypatch, fake_user, real_password
    ):
        fake_user["isActive"] = False
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=fake_user))
        with pytest.raises(auth_service.AuthError, match="disabled"):
            await auth_service.login("admin", real_password)

    async def test_malformed_stored_hash_rejected_like_wrong_password(self, monkeypatch, fake_user):
        fake_user["passwordHash"] = "not-a-real-bcrypt-hash"
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=fake_user))
        with pytest.raises(auth_service.AuthError, match="Invalid username or password"):
            await auth_service.login("admin", "anything")

    async def test_successful_login_returns_token_and_role(self, monkeypatch, fake_user, real_password):
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=fake_user))
        monkeypatch.setattr(auth_service, "_touch_last_login", AsyncMock(return_value=None))

        result = await auth_service.login("admin", real_password)

        assert isinstance(result, auth_service.LoginResult)
        assert result.username == "admin"
        assert result.role == "superuser"
        assert result.tenant_id == "default"
        assert result.token  # non-empty

        # And the issued token actually decodes back to the same claims.
        payload = auth_service.decode_token(result.token)
        assert payload["sub"] == "admin"
        assert payload["role"] == "superuser"
        assert payload["tenantId"] == "default"

    async def test_login_failure_never_calls_touch_last_login(self, monkeypatch, fake_user):
        touch = AsyncMock(return_value=None)
        monkeypatch.setattr(auth_service, "_get_user_by_username", AsyncMock(return_value=fake_user))
        monkeypatch.setattr(auth_service, "_touch_last_login", touch)

        with pytest.raises(auth_service.AuthError):
            await auth_service.login("admin", "wrong-password")

        touch.assert_not_called()


class TestDecodeToken:
    def test_raises_without_secret_configured(self, monkeypatch):
        monkeypatch.setattr(auth_service, "JWT_SECRET", "")
        with pytest.raises(RuntimeError, match="AUTH_JWT_SECRET"):
            auth_service.decode_token("irrelevant")

    def test_rejects_garbage_token(self):
        with pytest.raises(jwt.PyJWTError):
            auth_service.decode_token("not.a.jwt")

    def test_rejects_token_signed_with_a_different_secret(self):
        forged = jwt.encode({"sub": "admin"}, "some-other-secret", algorithm="HS256")
        with pytest.raises(jwt.PyJWTError):
            auth_service.decode_token(forged)

    def test_rejects_expired_token(self):
        now = int(time.time())
        expired = jwt.encode(
            {"sub": "admin", "iat": now - 100, "exp": now - 1},
            auth_service.JWT_SECRET,
            algorithm=auth_service.JWT_ALGORITHM,
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            auth_service.decode_token(expired)
