from __future__ import annotations

import json
import types
from datetime import datetime
from urllib import error as urllib_error

import pytest
from sqlalchemy.exc import IntegrityError

from core.exceptions import StorydexError
from services import auth_service


class GlobalConfig:
    def __init__(self):
        self.session = {}
        self.writes = []
        self.clears = 0

    def read_auth_session(self):
        return dict(self.session)

    def write_auth_session(self, payload):
        self.session = dict(payload)
        self.writes.append(dict(payload))
        return payload

    def clear_auth_session(self, **kwargs):
        self.session = {}
        self.clears += 1


def _service():
    service = auth_service.AuthService()
    service.settings = types.SimpleNamespace(storykeeper_base_url="https://storykeeper.test", novel_database_url="")
    service.global_config = GlobalConfig()
    return service


def test_remote_register_login_auth_profile_password_logout_and_summary(monkeypatch):
    service = _service()
    responses = {
        ("POST", "/api/auth/register"): {"user": {"userId": "u", "username": "alice"}},
        ("POST", "/api/auth/login"): {"accessToken": "token"},
        ("GET", "/api/auth/me"): {"user_id": "u", "username": "alice", "role": "USER"},
        ("PUT", "/api/auth/profile"): {"userId": "u", "username": "alice", "nickname": "A"},
        ("PUT", "/api/auth/password"): {"success": True, "message": "changed"},
        ("POST", "/api/auth/logout"): {},
        ("GET", "/api/auth/check-username/alice"): {"available": True},
        ("GET", "/api/auth/account-summary"): {
            "user": {"userId": "u", "username": "alice"},
            "quota": {"balance": 10, "totalGranted": 20},
            "profile": {"allowPersonalApiKey": False},
            "assets": {"stories": 1, "characters": 2, "worldbook": 3, "words": 4},
        },
    }

    def request(method, path, **kwargs):
        return responses[(method, path)]

    monkeypatch.setattr(service, "_storykeeper_request", request)
    assert service._remote_register_user(username=" alice ", password="secret", email=" ")["userId"] == "u"
    logged = service._remote_login_user(username="alice", password="secret")
    assert logged["accessToken"] == "token" and service.global_config.session["userId"] == "u"
    assert service._remote_authenticate_token("token")["username"] == "alice"
    service.global_config.session = {"accessToken": "token", "userId": "wrong", "username": "wrong"}
    assert service._remote_authenticate_token("token")["userId"] == "u"
    with pytest.raises(StorydexError):
        service._remote_authenticate_token("")

    service.global_config.session = {"accessToken": "token"}
    profile = service._remote_update_profile(payload={"nickname": "A", "ignored": "x"}, provided_fields=["nickname", "ignored"])
    assert profile["nickname"] == "A"
    assert service._remote_update_password(current_password="old", new_password="new")["success"] is True
    assert service._remote_check_username_available("alice") == {"available": True}
    summary = service._remote_get_account_summary()
    assert summary["quota"]["balance"] == 10 and summary["assets"]["words"] == 4
    assert service._remote_logout_token("token")["success"] is True
    assert service.global_config.clears >= 1

    service.global_config.session = {}
    with pytest.raises(StorydexError):
        service._remote_update_profile(payload={}, provided_fields=[])
    with pytest.raises(StorydexError):
        service._remote_update_password(current_password="", new_password="")
    with pytest.raises(StorydexError):
        service._remote_get_account_summary()

    monkeypatch.setattr(service, "_storykeeper_request", lambda *args, **kwargs: {})
    with pytest.raises(StorydexError):
        service._remote_register_user(username="a", password="secret", email=None)
    with pytest.raises(StorydexError):
        service._remote_login_user(username="a", password="secret")


def test_remote_persisted_session_variants(monkeypatch):
    service = _service()
    assert service._remote_get_persisted_session()["authenticated"] is False
    service.global_config.session = {"accessToken": "token", "serverBaseUrl": "https://other.test"}
    assert service._remote_get_persisted_session()["authenticated"] is False
    service.global_config.session = {"accessToken": "tok_legacy", "serverBaseUrl": "https://storykeeper.test"}
    assert service._remote_get_persisted_session()["authenticated"] is False

    service.global_config.session = {"accessToken": "token", "serverBaseUrl": "https://storykeeper.test", "user": {"userId": "old"}}
    monkeypatch.setattr(service, "_storykeeper_request", lambda *args, **kwargs: {"userId": "u", "username": "alice"})
    restored = service._remote_get_persisted_session()
    assert restored["authenticated"] is True and restored["user"]["userId"] == "u"

    def unauthorized(*args, **kwargs):
        raise StorydexError("unauthorized", status_code=401)

    monkeypatch.setattr(service, "_storykeeper_request", unauthorized)
    service.global_config.session = {"accessToken": "token", "serverBaseUrl": "https://storykeeper.test"}
    assert service._remote_get_persisted_session()["authenticated"] is False

    def missing(*args, **kwargs):
        raise StorydexError("missing", status_code=404)

    monkeypatch.setattr(service, "_storykeeper_request", missing)
    service.global_config.session = {"accessToken": "token", "serverBaseUrl": "https://storykeeper.test"}
    with pytest.raises(StorydexError) as exc:
        service._remote_get_persisted_session()
    assert exc.value.code == "storykeeper_auth_route_missing"

    monkeypatch.setattr(service, "_storykeeper_request", lambda *args, **kwargs: (_ for _ in ()).throw(StorydexError("boom", status_code=500)))
    with pytest.raises(StorydexError):
        service._remote_get_persisted_session()


def test_storykeeper_request_success_empty_invalid_http_and_connection(monkeypatch):
    service = _service()

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    monkeypatch.setattr(auth_service.urllib_request, "urlopen", lambda request, timeout: Response(b'{"ok":true}'))
    assert service._storykeeper_request("post", "/x", payload={"x": 1}, token="t") == {"ok": True}
    monkeypatch.setattr(auth_service.urllib_request, "urlopen", lambda request, timeout: Response(b""))
    assert service._storykeeper_request("GET", "/x") == {}
    monkeypatch.setattr(auth_service.urllib_request, "urlopen", lambda request, timeout: Response(b"[]"))
    with pytest.raises(StorydexError) as payload_error:
        service._storykeeper_request("GET", "/x")
    assert payload_error.value.code == "storykeeper_invalid_payload"
    monkeypatch.setattr(auth_service.urllib_request, "urlopen", lambda request, timeout: Response(b"bad"))
    with pytest.raises(StorydexError) as json_error:
        service._storykeeper_request("GET", "/x")
    assert json_error.value.code == "storykeeper_invalid_json"

    class FakeHTTPError(urllib_error.HTTPError):
        def read(self):
            return b'{"message":"denied","code":"remote_denied"}'

    monkeypatch.setattr(auth_service.urllib_request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(FakeHTTPError("u", 403, "", {}, None)))
    with pytest.raises(StorydexError) as http_error:
        service._storykeeper_request("GET", "/x")
    assert http_error.value.code == "remote_denied"
    monkeypatch.setattr(auth_service.urllib_request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(StorydexError) as offline:
        service._storykeeper_request("GET", "/x")
    assert offline.value.code == "storykeeper_unreachable"
    service.settings.storykeeper_base_url = ""
    with pytest.raises(StorydexError):
        service._storykeeper_base_url()


def test_auth_serializers_validation_integrity_http_errors_and_dates():
    with pytest.raises(StorydexError) as username:
        auth_service._validate_registration("", "secret")
    assert username.value.code == "username_required"
    with pytest.raises(StorydexError) as password:
        auth_service._validate_registration("user", "123")
    assert password.value.code == "password_too_short"
    auth_service._validate_registration("user", "123456")
    assert auth_service._normalize_username(" x ") == "x"
    assert auth_service._normalize_optional_text(" ") is None
    now = datetime(2025, 1, 1)
    user = auth_service._serialize_user({"user_id": "u", "username": "alice", "created_at": now})
    assert user["nickname"] == "alice" and user["createdAt"].startswith("2025")
    assert auth_service._serialize_profile(None) == auth_service.DEFAULT_PROFILE
    assert auth_service._serialize_profile({"quota_cost_per_generation": 2})["quotaCostPerGeneration"] == 2
    assert auth_service._serialize_quota(None) == auth_service.DEFAULT_QUOTA
    assert auth_service._serialize_quota({"balance": 2})["balance"] == 2
    remote = auth_service._serialize_remote_user({"user_id": "u", "username": "alice", "is_active": False})
    assert remote["isActive"] is False
    assert auth_service._serialize_remote_profile(None) == auth_service.DEFAULT_PROFILE
    assert auth_service._serialize_remote_profile({"allow_personal_api_key": False})["allowPersonalApiKey"] is False
    assert auth_service._serialize_remote_quota(None) == auth_service.DEFAULT_QUOTA
    assert auth_service._serialize_remote_quota({"total_granted": 3})["totalGranted"] == 3
    assert auth_service._serialize_remote_assets(None) == auth_service.DEFAULT_ASSETS
    assert auth_service._serialize_remote_assets({"stories": 1})["stories"] == 1
    assert auth_service._iso_or_none(None) is None and auth_service._iso_or_none(now).startswith("2025") and auth_service._iso_or_none("x") == "x"

    orig = types.SimpleNamespace(diag=types.SimpleNamespace(constraint_name="named"))
    exc = IntegrityError("stmt", {}, orig)
    assert auth_service._integrity_constraint_name(exc) == "named"
    for text, expected in (("ix_users_username", "ix_users_username"), ("ix_users_email", "ix_users_email"), ("other", "")):
        assert auth_service._integrity_constraint_name(IntegrityError("stmt", {}, Exception(text))) == expected
    assert auth_service._database_error(RuntimeError("bad")).code == "account_database_unavailable"
    assert auth_service._storykeeper_http_error(400, '{"detail":"bad","code":"x"}').code == "x"
    assert auth_service._storykeeper_http_error(500, "plain").message == "plain"
    assert auth_service._storykeeper_http_error(0, "").status_code == 500
