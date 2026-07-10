from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import routes_auth
from core.exceptions import StorydexError
from main import app
from services.auth_service import (
    AuthService,
    _normalize_optional_text,
    _serialize_profile,
    _serialize_quota,
    _serialize_remote_assets,
    _serialize_remote_profile,
    _serialize_remote_quota,
    _serialize_remote_user,
    _serialize_user,
    _storykeeper_http_error,
    _validate_registration,
)


pytestmark = pytest.mark.contract


def user_payload(**patch):
    payload = {
        "userId": "u1", "username": "alice", "email": "alice@example.com", "nickname": "Alice",
        "avatar": None, "role": "USER", "isActive": True, "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": None, "lastLoginAt": None,
    }
    payload.update(patch)
    return payload


class Config:
    def __init__(self):
        self.session = {}

    def write_auth_session(self, payload): self.session = dict(payload); return self.session
    def read_auth_session(self): return dict(self.session)
    def clear_auth_session(self, **kwargs): self.session = {}


@pytest.fixture
def remote_service():
    service = AuthService()
    service.settings = SimpleNamespace(storykeeper_base_url="https://storykeeper.test")
    service.global_config = Config()
    service._use_storykeeper_remote = True
    calls = []

    def request(method, path, *, payload=None, token=""):
        calls.append((method, path, payload, token))
        if path == "/api/auth/register": return {"user": user_payload()}
        if path == "/api/auth/login": return {"accessToken": "remote-token", "user": user_payload()}
        if path == "/api/auth/me": return user_payload()
        if path == "/api/auth/profile": return user_payload(nickname=payload.get("nickname") or "Alice")
        if path == "/api/auth/password": return {"success": True, "message": "changed"}
        if path == "/api/auth/logout": return {"success": True}
        if path.startswith("/api/auth/check-username/"): return {"available": True}
        if path == "/api/auth/account-summary":
            return {"user": user_payload(), "quota": {"balance": 9}, "profile": {}, "assets": {"stories": 2}}
        raise AssertionError(path)

    service._storykeeper_request = request
    return service, calls


def test_remote_auth_complete_lifecycle(remote_service):
    service, calls = remote_service
    assert service.register_user(username=" alice ", password="secret1", email=" a@b.com ")["username"] == "alice"
    login = service.login_user(username="alice", password="secret1")
    assert login["accessToken"] == "remote-token"
    assert service.authenticate_token("remote-token")["userId"] == "u1"
    assert service.update_profile(user_id="u1", payload={"nickname": "Updated", "ignored": "x"}, provided_fields={"nickname", "ignored"})["nickname"] == "Updated"
    assert service.update_password(user_id="u1", current_password="secret1", new_password="secret2")["success"] is True
    assert service.get_persisted_session()["authenticated"] is True
    assert service.check_username_available("new user")["available"] is True
    summary = service.get_account_summary(user_id="u1")
    assert summary["quota"]["balance"] == 9 and summary["assets"]["stories"] == 2
    assert service.logout_token("remote-token")["success"] is True
    assert service.get_persisted_session()["authenticated"] is False
    assert any(call[1] == "/api/auth/account-summary" for call in calls)


def test_remote_auth_invalid_and_expired_session_paths(remote_service):
    service, _ = remote_service
    with pytest.raises(StorydexError) as missing:
        service.authenticate_token("")
    assert missing.value.code == "auth_token_missing"
    with pytest.raises(StorydexError):
        service.update_profile(user_id="u1", payload={}, provided_fields=set())
    with pytest.raises(StorydexError):
        service.update_password(user_id="u1", current_password="a", new_password="b")

    service.global_config.session = {"accessToken": "tok_legacy", "serverBaseUrl": "https://storykeeper.test"}
    assert service.get_persisted_session()["authenticated"] is False
    service.global_config.session = {"accessToken": "remote", "serverBaseUrl": "https://wrong.test"}
    assert service.get_persisted_session()["authenticated"] is False

    service.global_config.session = {"accessToken": "remote", "serverBaseUrl": "https://storykeeper.test"}
    service._storykeeper_request = lambda *args, **kwargs: (_ for _ in ()).throw(
        StorydexError("expired", code="expired", status_code=401)
    )
    assert service.get_persisted_session()["authenticated"] is False


def test_auth_serializers_validation_and_http_error_mapping():
    with pytest.raises(StorydexError) as username:
        _validate_registration("", "secret1")
    assert username.value.code == "username_required"
    with pytest.raises(StorydexError) as password:
        _validate_registration("alice", "123")
    assert password.value.code == "password_too_short"
    assert _normalize_optional_text("  ") is None
    now = datetime.now(timezone.utc)
    local = _serialize_user({"user_id": "u", "username": "a", "created_at": now, "is_active": True})
    assert local["nickname"] == "a" and local["createdAt"]
    assert _serialize_profile(None)["quotaCostPerGeneration"] == 1
    assert _serialize_quota(None)["balance"] == 0
    assert _serialize_remote_user({"user_id": "u", "username": "a"})["isActive"] is True
    assert _serialize_remote_profile({"allow_personal_api_key": False})["allowPersonalApiKey"] is False
    assert _serialize_remote_quota({"total_granted": 3})["totalGranted"] == 3
    assert _serialize_remote_assets({"words": "12"})["words"] == 12
    assert _storykeeper_http_error(401, '{"detail":"bad token"}').status_code == 401
    assert _storykeeper_http_error(500, "not json").status_code == 500


class FakeAuth:
    def __init__(self):
        self.user = user_payload()
        self.token = "token"

    def register_user(self, **kwargs): return self.user
    def login_user(self, **kwargs): return {"accessToken": self.token, "userId": "u1", "username": "alice", "role": "USER", "user": self.user}
    def get_persisted_session(self): return {"authenticated": True, "accessToken": self.token, "user": self.user}
    def authenticate_token(self, token):
        if token != self.token: raise StorydexError("missing", code="auth_token_missing", status_code=401)
        return self.user
    def update_profile(self, **kwargs): self.user = {**self.user, **{k: v for k, v in kwargs["payload"].items() if v is not None}}; return self.user
    def update_password(self, **kwargs): return {"success": True, "message": "changed"}
    def logout_token(self, token): return {"success": True, "message": "Logged out."}
    def check_username_available(self, username): return {"available": username != "alice"}
    def get_account_summary(self, **kwargs): return {"user": self.user, "quota": {}, "profile": {}, "assets": {}}


def test_auth_api_all_routes(monkeypatch):
    fake = FakeAuth()
    monkeypatch.setattr(routes_auth, "auth_service", fake)
    headers = {"Authorization": "Bearer token"}
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/v1/auth/register", json={"username": "alice", "password": "secret1"}).status_code == 200
        login = client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret1"}).json()["data"]
        assert login["accessToken"] == "token"
        assert client.get("/api/v1/auth/session").json()["data"]["authenticated"] is True
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
        assert client.get("/api/v1/auth/profile", headers=headers).status_code == 200
        assert client.put("/api/v1/auth/me", headers=headers, json={"nickname": "A"}).status_code == 200
        assert client.put("/api/v1/auth/profile", headers=headers, json={"avatar": "x"}).status_code == 200
        assert client.post("/api/v1/auth/change-password", headers=headers, json={"oldPassword": "a", "newPassword": "bbbbbb"}).status_code == 200
        assert client.put("/api/v1/auth/password", headers=headers, json={"currentPassword": "a", "newPassword": "bbbbbb"}).status_code == 200
        assert client.get("/api/v1/auth/check-username/bob").json()["data"]["available"] is True
        assert client.get("/api/v1/auth/account-summary", headers=headers).status_code == 200
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
        unauthorized = client.get("/api/v1/auth/me")
        assert unauthorized.status_code == 401 and unauthorized.json()["error"]["code"] == "auth_token_missing"
