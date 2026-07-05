from __future__ import annotations

from typing import Any, Optional

from core.exceptions import StorydexError
from services.auth_service import get_auth_service


def require_bearer_token(authorization: Optional[str]) -> str:
    header_value = str(authorization or "").strip()
    if not header_value.lower().startswith("bearer "):
        raise StorydexError(
            "Authorization header is missing a Bearer token.",
            code="auth_header_invalid",
            status_code=401,
        )

    token = header_value.split(" ", 1)[1].strip()
    if not token:
        raise StorydexError(
            "Authentication token is required.",
            code="auth_token_missing",
            status_code=401,
        )
    return token


def resolve_request_user_optional(authorization: Optional[str]) -> Optional[dict[str, Any]]:
    header_value = str(authorization or "").strip()
    if not header_value:
        return None
    token = require_bearer_token(header_value)
    return get_auth_service().authenticate_token(token)


def resolve_request_bearer_token_optional(authorization: Optional[str]) -> str:
    header_value = str(authorization or "").strip()
    if not header_value:
        return ""
    return require_bearer_token(header_value)
