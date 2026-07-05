from __future__ import annotations

from time import perf_counter
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from services.auth_service import get_auth_service


router = APIRouter(tags=["auth"])
auth_service = get_auth_service()


class AccountUserResponse(BaseModel):
    user_id: str = Field(alias="userId")
    username: str
    email: Optional[str] = None
    nickname: Optional[str] = None
    avatar: Optional[str] = None
    role: str
    is_active: bool = Field(alias="isActive")
    created_at: str = Field(alias="createdAt")
    updated_at: Optional[str] = Field(alias="updatedAt", default=None)
    last_login_at: Optional[str] = Field(alias="lastLoginAt", default=None)

    model_config = ConfigDict(populate_by_name=True)


class AccountSummaryQuotaResponse(BaseModel):
    balance: int = 0
    total_granted: int = Field(alias="totalGranted", default=0)
    total_consumed: int = Field(alias="totalConsumed", default=0)
    is_unlimited: bool = Field(alias="isUnlimited", default=False)
    last_granted_at: Optional[str] = Field(alias="lastGrantedAt", default=None)
    last_consumed_at: Optional[str] = Field(alias="lastConsumedAt", default=None)

    model_config = ConfigDict(populate_by_name=True)


class AccountSummaryProfileResponse(BaseModel):
    default_session_id: Optional[str] = Field(alias="defaultSessionId", default=None)
    default_worldbook_id: Optional[str] = Field(alias="defaultWorldbookId", default=None)
    default_script_id: Optional[str] = Field(alias="defaultScriptId", default=None)
    allow_personal_api_key: bool = Field(alias="allowPersonalApiKey", default=True)
    allow_system_quota: bool = Field(alias="allowSystemQuota", default=True)
    quota_cost_per_generation: int = Field(alias="quotaCostPerGeneration", default=1)

    model_config = ConfigDict(populate_by_name=True)


class AccountSummaryAssetsResponse(BaseModel):
    stories: int = 0
    characters: int = 0
    worldbook: int = 0
    words: int = 0


class AccountSummaryResponse(BaseModel):
    user: AccountUserResponse
    quota: AccountSummaryQuotaResponse
    profile: AccountSummaryProfileResponse
    assets: AccountSummaryAssetsResponse


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None


class RegisterResponse(BaseModel):
    success: bool = True
    message: str
    user: AccountUserResponse


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    user_id: str = Field(alias="userId")
    username: str
    role: str
    user: AccountUserResponse

    model_config = ConfigDict(populate_by_name=True)


class CheckUsernameResponse(BaseModel):
    available: bool


class PersistedSessionResponse(BaseModel):
    authenticated: bool = False
    access_token: str = Field(alias="accessToken", default="")
    user: Optional[AccountUserResponse] = None

    model_config = ConfigDict(populate_by_name=True)


class UpdateProfileRequest(BaseModel):
    nickname: Optional[str] = None
    email: Optional[str] = None
    avatar: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(alias="oldPassword")
    new_password: str = Field(alias="newPassword")

    model_config = ConfigDict(populate_by_name=True)


class UpdatePasswordRequest(BaseModel):
    current_password: str = Field(alias="currentPassword")
    new_password: str = Field(alias="newPassword")

    model_config = ConfigDict(populate_by_name=True)


class ActionMessageResponse(BaseModel):
    success: bool = True
    message: str


def _build_trace(started: float, trace_id: str, *, tool_calls: int = 1) -> ApiTrace:
    return ApiTrace(
        traceId=trace_id,
        durationMs=int((perf_counter() - started) * 1000),
        toolCalls=tool_calls,
    )


def _require_bearer_token(authorization: Optional[str]) -> str:
    header_value = str(authorization or "").strip()
    scheme = "bearer "
    if not header_value.lower().startswith(scheme):
        return ""
    return header_value[len(scheme) :].strip()


def _current_user_payload(authorization: Optional[str]) -> dict[str, Any]:
    return auth_service.authenticate_token(_require_bearer_token(authorization))


@router.post("/auth/register", response_model=ApiEnvelope)
def register_account(payload: RegisterRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    user = auth_service.register_user(username=payload.username, password=payload.password, email=payload.email)
    data = RegisterResponse(
        success=True,
        message="Registered successfully.",
        user=AccountUserResponse(**user),
    )
    audit = [{"action": "register_account", "username": user["username"], "userId": user["userId"]}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.post("/auth/login", response_model=ApiEnvelope)
def login_account(payload: LoginRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    result = auth_service.login_user(username=payload.username, password=payload.password)
    data = LoginResponse(
        accessToken=result["accessToken"],
        userId=result["userId"],
        username=result["username"],
        role=result["role"],
        user=AccountUserResponse(**result["user"]),
    )
    audit = [{"action": "login_account", "username": result["username"], "userId": result["userId"]}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.get("/auth/session", response_model=ApiEnvelope)
def read_persisted_session() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    result = auth_service.get_persisted_session()
    user_payload = result.get("user") if isinstance(result.get("user"), dict) else None
    data = PersistedSessionResponse(
        authenticated=bool(result.get("authenticated", False)),
        accessToken=str(result.get("accessToken") or ""),
        user=AccountUserResponse(**user_payload) if isinstance(user_payload, dict) else None,
    )
    audit = [{"action": "read_persisted_session", "authenticated": data.authenticated}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.get("/auth/me", response_model=ApiEnvelope)
def read_current_user(authorization: Optional[str] = Header(default=None)) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    user = AccountUserResponse(**_current_user_payload(authorization))
    audit = [{"action": "read_current_account", "userId": user.user_id}]
    return success_response(
        data=user.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.get("/auth/profile", response_model=ApiEnvelope)
def read_current_profile(authorization: Optional[str] = Header(default=None)) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    user = AccountUserResponse(**_current_user_payload(authorization))
    audit = [{"action": "read_current_profile", "userId": user.user_id}]
    return success_response(
        data=user.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.put("/auth/me", response_model=ApiEnvelope)
def update_current_user(
    payload: UpdateProfileRequest,
    authorization: Optional[str] = Header(default=None),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    current_user = _current_user_payload(authorization)
    user = auth_service.update_profile(
        user_id=current_user["userId"],
        payload=payload.model_dump(),
        provided_fields=payload.model_fields_set,
    )
    data = AccountUserResponse(**user)
    audit = [{"action": "update_current_account", "userId": data.user_id}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.put("/auth/profile", response_model=ApiEnvelope)
def update_current_profile(
    payload: UpdateProfileRequest,
    authorization: Optional[str] = Header(default=None),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    current_user = _current_user_payload(authorization)
    user = auth_service.update_profile(
        user_id=current_user["userId"],
        payload=payload.model_dump(),
        provided_fields=payload.model_fields_set,
    )
    data = AccountUserResponse(**user)
    audit = [{"action": "update_current_profile", "userId": data.user_id}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.post("/auth/change-password", response_model=ApiEnvelope)
def change_password(
    payload: ChangePasswordRequest,
    authorization: Optional[str] = Header(default=None),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    current_user = _current_user_payload(authorization)
    result = auth_service.update_password(
        user_id=current_user["userId"],
        current_password=payload.old_password,
        new_password=payload.new_password,
    )
    data = ActionMessageResponse(**result)
    audit = [{"action": "change_password", "userId": current_user["userId"]}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.put("/auth/password", response_model=ApiEnvelope)
def update_password(
    payload: UpdatePasswordRequest,
    authorization: Optional[str] = Header(default=None),
) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    current_user = _current_user_payload(authorization)
    result = auth_service.update_password(
        user_id=current_user["userId"],
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    data = ActionMessageResponse(**result)
    audit = [{"action": "update_password", "userId": current_user["userId"]}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.post("/auth/logout", response_model=ApiEnvelope)
def logout_account(authorization: Optional[str] = Header(default=None)) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    token = _require_bearer_token(authorization)
    result = auth_service.logout_token(token)
    data = ActionMessageResponse(**result)
    audit = [{"action": "logout_account"}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.get("/auth/check-username/{username}", response_model=ApiEnvelope)
def check_username(username: str) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    result = CheckUsernameResponse(**auth_service.check_username_available(username))
    audit = [{"action": "check_username", "username": username, "available": result.available}]
    return success_response(
        data=result.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )


@router.get("/auth/account-summary", response_model=ApiEnvelope)
def read_account_summary(authorization: Optional[str] = Header(default=None)) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    current_user = _current_user_payload(authorization)
    summary = auth_service.get_account_summary(user_id=current_user["userId"])
    data = AccountSummaryResponse(
        user=AccountUserResponse(**summary["user"]),
        quota=AccountSummaryQuotaResponse(**summary["quota"]),
        profile=AccountSummaryProfileResponse(**summary["profile"]),
        assets=AccountSummaryAssetsResponse(**summary["assets"]),
    )
    audit = [{"action": "read_account_summary", "userId": current_user["userId"]}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started, trace_id),
        audit=audit,
    )
