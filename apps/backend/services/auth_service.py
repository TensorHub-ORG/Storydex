from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from threading import Lock
from typing import Any, Dict, Iterable, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote
from uuid import uuid4

from passlib.context import CryptContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.config import get_settings
from core.db import get_account_engine
from core.exceptions import StorydexError
from core.logger import get_logger
from services.global_config_service import get_global_config_service


app_logger = get_logger(__name__)
password_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")

DEFAULT_PROFILE = {
    "defaultSessionId": None,
    "defaultWorldbookId": None,
    "defaultScriptId": None,
    "allowPersonalApiKey": True,
    "allowSystemQuota": True,
    "quotaCostPerGeneration": 1,
}

DEFAULT_QUOTA = {
    "balance": 0,
    "totalGranted": 0,
    "totalConsumed": 0,
    "isUnlimited": False,
    "lastGrantedAt": None,
    "lastConsumedAt": None,
}

DEFAULT_ASSETS = {
    "stories": 0,
    "characters": 0,
    "worldbook": 0,
    "words": 0,
}


SELECT_USER_COLUMNS = """
    user_id,
    username,
    email,
    password_hash,
    role::text AS role,
    nickname,
    avatar,
    COALESCE(is_active, true) AS is_active,
    created_at,
    updated_at,
    last_login_at
"""


class AuthService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.global_config = get_global_config_service()
        self._token_lock = Lock()
        self._tokens_by_value: dict[str, str] = {}
        database_url = str(self.settings.novel_database_url or "").strip()
        storykeeper_base = str(self.settings.storykeeper_base_url or "").strip().rstrip("/").lower()
        # The hosted quota gateway owns the Storydex account-token table now.
        # Keep the old Storykeeper HTTP path only as a no-database fallback so stale
        # STORYKEEPER_BASE_URL values cannot hijack desktop login.
        self._use_storykeeper_remote = not bool(database_url) and bool(storykeeper_base) and storykeeper_base not in {
            "http://127.0.0.1:8020",
            "http://localhost:8020",
        }

    def register_user(self, *, username: str, password: str, email: Optional[str] = None) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_register_user(username=username, password=password, email=email)

        normalized_username = _normalize_username(username)
        normalized_email = _normalize_optional_text(email)
        _validate_registration(normalized_username, password)

        try:
            with get_account_engine().begin() as connection:
                if self._username_exists(connection, normalized_username):
                    raise StorydexError(
                        "Username already exists.",
                        code="username_already_exists",
                        status_code=409,
                    )

                if normalized_email and self._email_exists(connection, normalized_email):
                    raise StorydexError(
                        "Email already exists.",
                        code="email_already_exists",
                        status_code=409,
                    )

                user_id = self._allocate_user_id(connection)
                now = _now()
                connection.execute(
                    text(
                        """
                        INSERT INTO public.users (
                            user_id,
                            username,
                            email,
                            password_hash,
                            role,
                            nickname,
                            avatar,
                            is_active,
                            created_at,
                            updated_at,
                            last_login_at
                        )
                        VALUES (
                            :user_id,
                            :username,
                            :email,
                            :password_hash,
                            :role,
                            :nickname,
                            :avatar,
                            :is_active,
                            :created_at,
                            :updated_at,
                            :last_login_at
                        )
                        """
                    ),
                    {
                        "user_id": user_id,
                        "username": normalized_username,
                        "email": normalized_email,
                        "password_hash": password_context.hash(password, scheme="pbkdf2_sha256"),
                        "role": "USER",
                        "nickname": normalized_username,
                        "avatar": None,
                        "is_active": True,
                        "created_at": now,
                        "updated_at": now,
                        "last_login_at": None,
                    },
                )
                row = self._select_user_by_id(connection, user_id)
        except IntegrityError as exc:
            raise self._map_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        if not row:
            raise StorydexError(
                "Registered user could not be loaded.",
                code="account_user_load_failed",
                status_code=500,
            )

        return _serialize_user(row)

    def login_user(self, *, username: str, password: str) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_login_user(username=username, password=password)

        normalized_username = _normalize_username(username)
        if not normalized_username or not password:
            raise StorydexError(
                "Invalid username or password.",
                code="invalid_credentials",
                status_code=401,
            )

        try:
            with get_account_engine().begin() as connection:
                row = self._select_user_by_username(connection, normalized_username)
                if not row or not password_context.verify(password, str(row["password_hash"] or "")):
                    raise StorydexError(
                        "Invalid username or password.",
                        code="invalid_credentials",
                        status_code=401,
                    )

                if not bool(row["is_active"]):
                    raise StorydexError(
                        "Account is disabled.",
                        code="account_disabled",
                        status_code=403,
                    )

                now = _now()
                connection.execute(
                    text(
                        """
                        UPDATE public.users
                        SET last_login_at = :last_login_at
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "user_id": row["user_id"],
                        "last_login_at": now,
                    },
                )
                user_row = self._select_user_by_id(connection, str(row["user_id"]))
        except StorydexError:
            raise
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        if not user_row:
            raise StorydexError(
                "Logged in user could not be loaded.",
                code="account_user_load_failed",
                status_code=500,
            )

        try:
            with get_account_engine().begin() as connection:
                access_token = self._get_or_create_user_token(connection, str(user_row["user_id"]))
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        user = _serialize_user(user_row)
        with self._token_lock:
            self._tokens_by_value[access_token] = user["userId"]
        self.global_config.write_auth_session(
            {
                "accessToken": access_token,
                "userId": user["userId"],
                "username": user["username"],
            }
        )
        return {
            "accessToken": access_token,
            "userId": user["userId"],
            "username": user["username"],
            "role": user["role"],
            "user": user,
        }

    def authenticate_token(self, token: str) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_authenticate_token(token)

        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise StorydexError(
                "Authentication token is required.",
                code="auth_token_missing",
                status_code=401,
            )

        with self._token_lock:
            user_id = self._tokens_by_value.get(normalized_token)

        if not user_id:
            try:
                with get_account_engine().begin() as connection:
                    user_id = self._select_user_id_by_token(connection, normalized_token)
                    if user_id:
                        self._touch_user_token(connection, normalized_token)
            except SQLAlchemyError as exc:
                raise _database_error(exc) from exc

            if user_id:
                with self._token_lock:
                    self._tokens_by_value[normalized_token] = user_id

        if not user_id:
            raise StorydexError(
                "Authentication token is invalid.",
                code="auth_token_invalid",
                status_code=401,
            )

        try:
            with get_account_engine().connect() as connection:
                row = self._select_user_by_id(connection, user_id)
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        if not row:
            self.logout_token(normalized_token)
            raise StorydexError(
                "Authentication token is invalid.",
                code="auth_token_invalid",
                status_code=401,
            )

        return _serialize_user(row)

    def update_profile(
        self,
        *,
        user_id: str,
        payload: dict[str, Any],
        provided_fields: Iterable[str],
    ) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_update_profile(payload=payload, provided_fields=provided_fields)

        editable_fields = [field for field in provided_fields if field in {"nickname", "email", "avatar"}]
        if not editable_fields:
            return self.get_user_by_id(user_id)

        values: dict[str, Any] = {}
        if "nickname" in editable_fields:
            values["nickname"] = _normalize_optional_text(payload.get("nickname"))
        if "email" in editable_fields:
            values["email"] = _normalize_optional_text(payload.get("email"))
        if "avatar" in editable_fields:
            values["avatar"] = _normalize_optional_text(payload.get("avatar"))

        assignments = [f"{field} = :{field}" for field in values.keys()]
        values["updated_at"] = _now()
        assignments.append("updated_at = :updated_at")
        values["user_id"] = user_id

        try:
            with get_account_engine().begin() as connection:
                self._ensure_user_exists(connection, user_id)
                connection.execute(
                    text(
                        f"""
                        UPDATE public.users
                        SET {", ".join(assignments)}
                        WHERE user_id = :user_id
                        """
                    ),
                    values,
                )
                row = self._select_user_by_id(connection, user_id)
        except IntegrityError as exc:
            raise self._map_integrity_error(exc) from exc
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        if not row:
            raise StorydexError(
                "Updated user could not be loaded.",
                code="account_user_load_failed",
                status_code=500,
            )

        return _serialize_user(row)

    def update_password(self, *, user_id: str, current_password: str, new_password: str) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_update_password(current_password=current_password, new_password=new_password)

        if len(str(new_password or "")) < 6:
            raise StorydexError(
                "New password must be at least 6 characters long.",
                code="password_too_short",
                status_code=400,
            )

        try:
            with get_account_engine().begin() as connection:
                row = self._select_user_by_id(connection, user_id)
                if not row:
                    raise StorydexError(
                        "User account was not found.",
                        code="account_user_not_found",
                        status_code=404,
                    )
                if not password_context.verify(current_password, str(row["password_hash"] or "")):
                    raise StorydexError(
                        "Current password is incorrect.",
                        code="password_incorrect",
                        status_code=400,
                    )

                connection.execute(
                    text(
                        """
                        UPDATE public.users
                        SET password_hash = :password_hash,
                            updated_at = :updated_at
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "user_id": user_id,
                        "password_hash": password_context.hash(new_password, scheme="pbkdf2_sha256"),
                        "updated_at": _now(),
                    },
                )
        except StorydexError:
            raise
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        return {
            "success": True,
            "message": "Password updated.",
        }

    def logout_token(self, token: str) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_logout_token(token)

        normalized_token = str(token or "").strip()
        if normalized_token:
            with self._token_lock:
                self._tokens_by_value.pop(normalized_token, None)
            persisted = self.global_config.read_auth_session()
            if normalized_token == str(persisted.get("accessToken") or "").strip():
                self.global_config.clear_auth_session(remove_record=True)
        else:
            self.global_config.clear_auth_session(remove_record=True)

        return {
            "success": True,
            "message": "Logged out.",
        }

    def get_persisted_session(self) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_get_persisted_session()

        session = self.global_config.read_auth_session()
        access_token = str(session.get("accessToken") or "").strip()
        if not access_token:
            return {
                "authenticated": False,
                "accessToken": "",
                "user": None,
            }

        try:
            with get_account_engine().begin() as connection:
                user_id = self._select_user_id_by_token(connection, access_token)
                if user_id:
                    self._touch_user_token(connection, access_token)
            if not user_id:
                self.global_config.clear_auth_session(remove_record=True)
                with self._token_lock:
                    self._tokens_by_value.pop(access_token, None)
                return {
                    "authenticated": False,
                    "accessToken": "",
                    "user": None,
                }

            with get_account_engine().connect() as connection:
                row = self._select_user_by_id(connection, user_id)
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        if not row:
            self.global_config.clear_auth_session()
            with self._token_lock:
                self._tokens_by_value.pop(access_token, None)
            return {
                "authenticated": False,
                "accessToken": "",
                "user": None,
            }

        with self._token_lock:
            self._tokens_by_value[access_token] = user_id

        return {
            "authenticated": True,
            "accessToken": access_token,
            "user": _serialize_user(row),
        }

    def get_user_by_id(self, user_id: str) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            session = self.global_config.read_auth_session()
            access_token = str(session.get("accessToken") or "").strip()
            if not access_token:
                raise StorydexError(
                    "Authentication token is required.",
                    code="auth_token_missing",
                    status_code=401,
                )
            return self._remote_authenticate_token(access_token)

        try:
            with get_account_engine().connect() as connection:
                row = self._select_user_by_id(connection, user_id)
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        if not row:
            raise StorydexError(
                "User account was not found.",
                code="account_user_not_found",
                status_code=404,
            )
        return _serialize_user(row)

    def check_username_available(self, username: str) -> dict[str, bool]:
        if self._use_storykeeper_remote:
            return self._remote_check_username_available(username)

        normalized_username = _normalize_username(username)
        if not normalized_username:
            return {"available": False}

        try:
            with get_account_engine().connect() as connection:
                available = not self._username_exists(connection, normalized_username)
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        return {"available": available}

    def get_account_summary(self, *, user_id: str) -> dict[str, Any]:
        if self._use_storykeeper_remote:
            return self._remote_get_account_summary()

        try:
            with get_account_engine().connect() as connection:
                user_row = self._select_user_by_id(connection, user_id)
                if not user_row:
                    raise StorydexError(
                        "User account was not found.",
                        code="account_user_not_found",
                        status_code=404,
                    )

                profile_row = connection.execute(
                    text(
                        """
                        SELECT
                            default_session_id,
                            default_worldbook_id,
                            default_script_id,
                            allow_personal_api_key,
                            allow_system_quota,
                            quota_cost_per_generation
                        FROM public.user_admin_profiles
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().first()

                quota_row = connection.execute(
                    text(
                        """
                        SELECT
                            balance,
                            total_granted,
                            total_consumed,
                            is_unlimited,
                            last_granted_at,
                            last_consumed_at
                        FROM public.user_quota_accounts
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().first()
        except StorydexError:
            raise
        except SQLAlchemyError as exc:
            raise _database_error(exc) from exc

        return {
            "user": _serialize_user(user_row),
            "quota": _serialize_quota(quota_row),
            "profile": _serialize_profile(profile_row),
            "assets": dict(DEFAULT_ASSETS),
        }

    def _remote_register_user(self, *, username: str, password: str, email: Optional[str]) -> dict[str, Any]:
        payload = {
            "username": str(username or "").strip(),
            "password": str(password or ""),
            "email": _normalize_optional_text(email),
        }
        response = self._storykeeper_request("POST", "/api/auth/register", payload=payload)
        user = response.get("user") if isinstance(response.get("user"), dict) else None
        if not isinstance(user, dict):
            raise StorydexError(
                "Storykeeper register response is invalid.",
                code="storykeeper_register_invalid",
                status_code=502,
            )
        return _serialize_remote_user(user)

    def _remote_login_user(self, *, username: str, password: str) -> dict[str, Any]:
        payload = {
            "username": str(username or "").strip(),
            "password": str(password or ""),
        }
        response = self._storykeeper_request("POST", "/api/auth/login", payload=payload)
        access_token = str(response.get("access_token") or response.get("accessToken") or "").strip()
        if not access_token:
            raise StorydexError(
                "Storykeeper login response is invalid.",
                code="storykeeper_login_invalid",
                status_code=502,
            )

        user_payload = response.get("user") if isinstance(response.get("user"), dict) else None
        if not isinstance(user_payload, dict):
            user_payload = self._storykeeper_request("GET", "/api/auth/me", token=access_token)
        if not isinstance(user_payload, dict):
            raise StorydexError(
                "Storykeeper login did not return user info.",
                code="storykeeper_login_invalid_user",
                status_code=502,
            )

        user = _serialize_remote_user(user_payload)
        self.global_config.write_auth_session(
            {
                "accessToken": access_token,
                "userId": user["userId"],
                "username": user["username"],
                "serverBaseUrl": self._storykeeper_base_url(),
                "user": user,
            }
        )
        with self._token_lock:
            self._tokens_by_value[access_token] = user["userId"]
        return {
            "accessToken": access_token,
            "userId": user["userId"],
            "username": user["username"],
            "role": user["role"],
            "user": user,
        }

    def _remote_authenticate_token(self, token: str) -> dict[str, Any]:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise StorydexError(
                "Authentication token is required.",
                code="auth_token_missing",
                status_code=401,
            )

        user_payload = self._storykeeper_request("GET", "/api/auth/me", token=normalized_token)
        user = _serialize_remote_user(user_payload)
        with self._token_lock:
            self._tokens_by_value[normalized_token] = user["userId"]

        current = self.global_config.read_auth_session()
        if (
            normalized_token == str(current.get("accessToken") or "").strip()
            and (
                str(current.get("userId") or "").strip() != user["userId"]
                or str(current.get("username") or "").strip() != user["username"]
            )
        ):
            self.global_config.write_auth_session(
                {
                    "accessToken": normalized_token,
                    "userId": user["userId"],
                    "username": user["username"],
                    "serverBaseUrl": self._storykeeper_base_url(),
                    "user": user,
                }
            )
        return user

    def _remote_update_profile(self, *, payload: dict[str, Any], provided_fields: Iterable[str]) -> dict[str, Any]:
        session = self.global_config.read_auth_session()
        access_token = str(session.get("accessToken") or "").strip()
        if not access_token:
            raise StorydexError(
                "Authentication token is required.",
                code="auth_token_missing",
                status_code=401,
            )

        outgoing: dict[str, Any] = {}
        for field in provided_fields:
            if field in {"nickname", "email", "avatar"}:
                outgoing[field] = payload.get(field)

        user_payload = self._storykeeper_request("PUT", "/api/auth/profile", payload=outgoing, token=access_token)
        user = _serialize_remote_user(user_payload)
        self.global_config.write_auth_session(
            {
                "accessToken": access_token,
                "userId": user["userId"],
                "username": user["username"],
                "serverBaseUrl": self._storykeeper_base_url(),
                "user": user,
            }
        )
        return user

    def _remote_update_password(self, *, current_password: str, new_password: str) -> dict[str, Any]:
        session = self.global_config.read_auth_session()
        access_token = str(session.get("accessToken") or "").strip()
        if not access_token:
            raise StorydexError(
                "Authentication token is required.",
                code="auth_token_missing",
                status_code=401,
            )
        payload = {
            "current_password": str(current_password or ""),
            "new_password": str(new_password or ""),
        }
        response = self._storykeeper_request("PUT", "/api/auth/password", payload=payload, token=access_token)
        return {
            "success": bool(response.get("success", True)),
            "message": str(response.get("message") or "Password updated."),
        }

    def _remote_logout_token(self, token: str) -> dict[str, Any]:
        normalized_token = str(token or "").strip()
        if normalized_token:
            try:
                self._storykeeper_request("POST", "/api/auth/logout", token=normalized_token)
            except StorydexError:
                pass
        with self._token_lock:
            self._tokens_by_value.pop(normalized_token, None)
        current = self.global_config.read_auth_session()
        if not normalized_token or normalized_token == str(current.get("accessToken") or "").strip():
            self.global_config.clear_auth_session(remove_record=True)
        return {
            "success": True,
            "message": "Logged out.",
        }

    def _remote_get_persisted_session(self) -> dict[str, Any]:
        session = self.global_config.read_auth_session()
        access_token = str(session.get("accessToken") or "").strip()
        user_payload = session.get("user") if isinstance(session.get("user"), dict) else None
        if not access_token:
            return {
                "authenticated": False,
                "accessToken": "",
                "user": None,
            }

        configured_base_url = self._storykeeper_base_url().rstrip("/").lower()
        session_base_url = str(session.get("serverBaseUrl") or "").strip().rstrip("/").lower()
        if session_base_url != configured_base_url or access_token.startswith("tok_"):
            self.global_config.clear_auth_session(remove_record=True)
            with self._token_lock:
                self._tokens_by_value.pop(access_token, None)
            return {
                "authenticated": False,
                "accessToken": "",
                "user": None,
            }

        try:
            current_user = _serialize_remote_user(
                self._storykeeper_request("GET", "/api/auth/me", token=access_token)
            )
        except StorydexError as exc:
            if exc.status_code == 401:
                self.global_config.clear_auth_session(remove_record=True)
                with self._token_lock:
                    self._tokens_by_value.pop(access_token, None)
                return {
                    "authenticated": False,
                    "accessToken": "",
                    "user": None,
                }
            if exc.status_code == 404:
                raise StorydexError(
                    "Storykeeper server is missing the desktop auth API. Please redeploy Storykeeper with the latest account-system routes.",
                    code="storykeeper_auth_route_missing",
                    status_code=503,
                    details={"path": "/api/auth/me", "serverBaseUrl": self._storykeeper_base_url()},
                ) from exc
            raise

        self.global_config.write_auth_session(
            {
                "accessToken": access_token,
                "userId": current_user["userId"],
                "username": current_user["username"],
                "serverBaseUrl": self._storykeeper_base_url(),
                "user": current_user,
            }
        )
        return {
            "authenticated": True,
            "accessToken": access_token,
            "user": current_user if isinstance(current_user, dict) else user_payload,
        }

    def _remote_check_username_available(self, username: str) -> dict[str, bool]:
        response = self._storykeeper_request(
            "GET",
            f"/api/auth/check-username/{quote(str(username or '').strip())}",
        )
        return {"available": bool(response.get("available", False))}

    def _remote_get_account_summary(self) -> dict[str, Any]:
        session = self.global_config.read_auth_session()
        access_token = str(session.get("accessToken") or "").strip()
        if not access_token:
            raise StorydexError(
                "Authentication token is required.",
                code="auth_token_missing",
                status_code=401,
            )
        response = self._storykeeper_request("GET", "/api/auth/account-summary", token=access_token)
        if not isinstance(response, dict):
            raise StorydexError(
                "Storykeeper account summary response is invalid.",
                code="storykeeper_account_summary_invalid",
                status_code=502,
            )
        user = _serialize_remote_user(response.get("user") if isinstance(response.get("user"), dict) else {})
        self.global_config.write_auth_session(
            {
                "accessToken": access_token,
                "userId": user["userId"],
                "username": user["username"],
                "serverBaseUrl": self._storykeeper_base_url(),
                "user": user,
            }
        )
        return {
            "user": user,
            "quota": _serialize_remote_quota(response.get("quota") if isinstance(response.get("quota"), dict) else None),
            "profile": _serialize_remote_profile(
                response.get("profile") if isinstance(response.get("profile"), dict) else None
            ),
            "assets": _serialize_remote_assets(response.get("assets") if isinstance(response.get("assets"), dict) else None),
        }

    def _storykeeper_request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        token: str = "",
    ) -> dict[str, Any]:
        url = f"{self._storykeeper_base_url()}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "StorydexDesktop/1.0",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        normalized_token = str(token or "").strip()
        if normalized_token:
            headers["Authorization"] = f"Bearer {normalized_token}"

        request = urllib_request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib_request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise _storykeeper_http_error(exc.code, body_text) from exc
        except Exception as exc:
            raise StorydexError(
                "Failed to connect to Storykeeper.",
                code="storykeeper_unreachable",
                status_code=503,
                details={"reason": str(exc)},
            ) from exc

        if not raw.strip():
            return {}
        try:
            decoded = json.loads(raw)
        except Exception as exc:
            raise StorydexError(
                "Storykeeper returned invalid JSON.",
                code="storykeeper_invalid_json",
                status_code=502,
                details={"path": path},
            ) from exc
        if not isinstance(decoded, dict):
            raise StorydexError(
                "Storykeeper returned invalid response payload.",
                code="storykeeper_invalid_payload",
                status_code=502,
                details={"path": path},
            )
        return decoded

    def _storykeeper_base_url(self) -> str:
        base_url = str(self.settings.storykeeper_base_url or "").strip().rstrip("/")
        if not base_url:
            raise StorydexError(
                "Storykeeper base URL is not configured.",
                code="storykeeper_base_url_missing",
                status_code=503,
            )
        return base_url

    def _issue_token(self, user_id: str) -> str:
        token = f"tok_{uuid4().hex}"
        return token

    def _ensure_user_token_table(self, connection: Connection) -> None:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.storydex_user_tokens (
                    user_id VARCHAR(64) PRIMARY KEY REFERENCES public.users(user_id) ON DELETE CASCADE,
                    access_token VARCHAR(128) NOT NULL UNIQUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_storydex_user_tokens_access_token
                ON public.storydex_user_tokens (access_token)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO public.storydex_user_tokens (
                    user_id,
                    access_token,
                    created_at,
                    updated_at
                )
                SELECT
                    u.user_id,
                    'tok_' || md5(u.user_id || random()::text || clock_timestamp()::text),
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                FROM public.users u
                ON CONFLICT (user_id) DO NOTHING
                """
            )
        )

    def _get_or_create_user_token(self, connection: Connection, user_id: str) -> str:
        self._ensure_user_token_table(connection)
        existing = connection.execute(
            text(
                """
                SELECT access_token
                FROM public.storydex_user_tokens
                WHERE user_id = :user_id
                """
            ),
            {"user_id": user_id},
        ).scalar_one_or_none()
        if existing:
            return str(existing).strip()

        token = self._issue_token(user_id)
        connection.execute(
            text(
                """
                INSERT INTO public.storydex_user_tokens (
                    user_id,
                    access_token,
                    created_at,
                    updated_at,
                    last_used_at
                )
                VALUES (
                    :user_id,
                    :access_token,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (user_id) DO UPDATE SET
                    access_token = :access_token,
                    updated_at = CURRENT_TIMESTAMP,
                    last_used_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "user_id": user_id,
                "access_token": token,
            },
        )
        return token

    def _select_user_id_by_token(self, connection: Connection, access_token: str) -> str:
        self._ensure_user_token_table(connection)
        result = connection.execute(
            text(
                """
                SELECT user_id
                FROM public.storydex_user_tokens
                WHERE access_token = :access_token
                """
            ),
            {"access_token": access_token},
        ).scalar_one_or_none()
        return str(result or "").strip()

    def _touch_user_token(self, connection: Connection, access_token: str) -> None:
        self._ensure_user_token_table(connection)
        connection.execute(
            text(
                """
                UPDATE public.storydex_user_tokens
                SET last_used_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE access_token = :access_token
                """
            ),
            {"access_token": access_token},
        )

    def _select_user_by_id(self, connection: Connection, user_id: str) -> Optional[dict[str, Any]]:
        return connection.execute(
            text(
                f"""
                SELECT {SELECT_USER_COLUMNS}
                FROM public.users
                WHERE user_id = :user_id
                """
            ),
            {"user_id": user_id},
        ).mappings().first()

    def _select_user_by_username(self, connection: Connection, username: str) -> Optional[dict[str, Any]]:
        return connection.execute(
            text(
                f"""
                SELECT {SELECT_USER_COLUMNS}
                FROM public.users
                WHERE username = :username
                """
            ),
            {"username": username},
        ).mappings().first()

    def _username_exists(self, connection: Connection, username: str) -> bool:
        return bool(
            connection.execute(
                text("SELECT 1 FROM public.users WHERE username = :username"),
                {"username": username},
            ).scalar()
        )

    def _email_exists(self, connection: Connection, email: str) -> bool:
        return bool(
            connection.execute(
                text("SELECT 1 FROM public.users WHERE email = :email"),
                {"email": email},
            ).scalar()
        )

    def _ensure_user_exists(self, connection: Connection, user_id: str) -> None:
        if not self._select_user_by_id(connection, user_id):
            raise StorydexError(
                "User account was not found.",
                code="account_user_not_found",
                status_code=404,
            )

    def _allocate_user_id(self, connection: Connection) -> str:
        for _ in range(16):
            candidate = uuid4().hex[:12]
            if not connection.execute(
                text("SELECT 1 FROM public.users WHERE user_id = :user_id"),
                {"user_id": candidate},
            ).scalar():
                return candidate

        raise StorydexError(
            "User id generation failed.",
            code="account_user_id_generation_failed",
            status_code=500,
        )

    def _map_integrity_error(self, exc: IntegrityError) -> StorydexError:
        constraint_name = _integrity_constraint_name(exc)
        if constraint_name == "ix_users_username":
            return StorydexError(
                "Username already exists.",
                code="username_already_exists",
                status_code=409,
            )
        if constraint_name == "ix_users_email":
            return StorydexError(
                "Email already exists.",
                code="email_already_exists",
                status_code=409,
            )
        return _database_error(exc, code="account_database_integrity_error", status_code=409)


def _validate_registration(username: str, password: str) -> None:
    if not username:
        raise StorydexError(
            "Username is required.",
            code="username_required",
            status_code=400,
        )
    if len(str(password or "")) < 6:
        raise StorydexError(
            "Password must be at least 6 characters long.",
            code="password_too_short",
            status_code=400,
        )


def _normalize_username(value: str) -> str:
    return str(value or "").strip()


def _normalize_optional_text(value: Any) -> Optional[str]:
    text_value = str(value or "").strip()
    return text_value or None


def _serialize_user(row: dict[str, Any]) -> dict[str, Any]:
    nickname = _normalize_optional_text(row.get("nickname")) or str(row.get("username") or "")
    return {
        "userId": str(row.get("user_id") or ""),
        "username": str(row.get("username") or ""),
        "email": row.get("email"),
        "nickname": nickname,
        "avatar": row.get("avatar"),
        "role": str(row.get("role") or "USER"),
        "isActive": bool(row.get("is_active", True)),
        "createdAt": _iso_or_none(row.get("created_at")) or "",
        "updatedAt": _iso_or_none(row.get("updated_at")),
        "lastLoginAt": _iso_or_none(row.get("last_login_at")),
    }


def _serialize_profile(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return dict(DEFAULT_PROFILE)

    return {
        "defaultSessionId": row.get("default_session_id"),
        "defaultWorldbookId": row.get("default_worldbook_id"),
        "defaultScriptId": row.get("default_script_id"),
        "allowPersonalApiKey": bool(row.get("allow_personal_api_key", True)),
        "allowSystemQuota": bool(row.get("allow_system_quota", True)),
        "quotaCostPerGeneration": int(row.get("quota_cost_per_generation") or 1),
    }


def _serialize_quota(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return dict(DEFAULT_QUOTA)

    return {
        "balance": int(row.get("balance") or 0),
        "totalGranted": int(row.get("total_granted") or 0),
        "totalConsumed": int(row.get("total_consumed") or 0),
        "isUnlimited": bool(row.get("is_unlimited", False)),
        "lastGrantedAt": _iso_or_none(row.get("last_granted_at")),
        "lastConsumedAt": _iso_or_none(row.get("last_consumed_at")),
    }


def _serialize_remote_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "userId": str(row.get("userId") or row.get("user_id") or "").strip(),
        "username": str(row.get("username") or "").strip(),
        "email": _normalize_optional_text(row.get("email")),
        "nickname": _normalize_optional_text(row.get("nickname")) or str(row.get("username") or "").strip(),
        "avatar": _normalize_optional_text(row.get("avatar")),
        "role": str(row.get("role") or "USER").strip() or "USER",
        "isActive": bool(row.get("isActive", row.get("is_active", True))),
        "createdAt": _iso_or_none(row.get("createdAt") or row.get("created_at")) or "",
        "updatedAt": _iso_or_none(row.get("updatedAt") or row.get("updated_at")),
        "lastLoginAt": _iso_or_none(row.get("lastLoginAt") or row.get("last_login_at")),
    }


def _serialize_remote_profile(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return dict(DEFAULT_PROFILE)
    return {
        "defaultSessionId": row.get("defaultSessionId", row.get("default_session_id")),
        "defaultWorldbookId": row.get("defaultWorldbookId", row.get("default_worldbook_id")),
        "defaultScriptId": row.get("defaultScriptId", row.get("default_script_id")),
        "allowPersonalApiKey": bool(row.get("allowPersonalApiKey", row.get("allow_personal_api_key", True))),
        "allowSystemQuota": bool(row.get("allowSystemQuota", row.get("allow_system_quota", True))),
        "quotaCostPerGeneration": int(row.get("quotaCostPerGeneration", row.get("quota_cost_per_generation", 1)) or 1),
    }


def _serialize_remote_quota(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return dict(DEFAULT_QUOTA)
    return {
        "balance": int(row.get("balance") or 0),
        "totalGranted": int(row.get("totalGranted", row.get("total_granted", 0)) or 0),
        "totalConsumed": int(row.get("totalConsumed", row.get("total_consumed", 0)) or 0),
        "isUnlimited": bool(row.get("isUnlimited", row.get("is_unlimited", False))),
        "lastGrantedAt": _iso_or_none(row.get("lastGrantedAt") or row.get("last_granted_at")),
        "lastConsumedAt": _iso_or_none(row.get("lastConsumedAt") or row.get("last_consumed_at")),
    }


def _serialize_remote_assets(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return dict(DEFAULT_ASSETS)
    return {
        "stories": int(row.get("stories") or 0),
        "characters": int(row.get("characters") or 0),
        "worldbook": int(row.get("worldbook") or 0),
        "words": int(row.get("words") or 0),
    }


def _integrity_constraint_name(exc: IntegrityError) -> str:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", "")
    if constraint_name:
        return str(constraint_name)

    message = str(original or exc)
    if "ix_users_username" in message:
        return "ix_users_username"
    if "ix_users_email" in message:
        return "ix_users_email"
    return ""


def _database_error(
    exc: Exception,
    *,
    code: str = "account_database_unavailable",
    status_code: int = 503,
) -> StorydexError:
    app_logger.exception("Account database operation failed")
    return StorydexError(
        "Account database is temporarily unavailable.",
        code=code,
        status_code=status_code,
        details={"exceptionType": exc.__class__.__name__},
    )


def _storykeeper_http_error(status_code: int, raw_body: str) -> StorydexError:
    message = ""
    code = ""
    try:
        payload = json.loads(raw_body)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("detail") or "").strip()
        code = str(payload.get("code") or "").strip()
    message = message or (raw_body.strip()[:240] if raw_body.strip() else "Storykeeper request failed.")
    return StorydexError(
        message,
        code=code or "storykeeper_request_failed",
        status_code=int(status_code or 500),
        details={"remoteStatus": int(status_code or 500)},
    )


def _now() -> datetime:
    return datetime.utcnow()


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    return AuthService()
