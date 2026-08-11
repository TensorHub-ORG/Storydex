from __future__ import annotations

from datetime import datetime, timezone
import base64
import binascii
import os
import re
import sys
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from core.config import get_settings
from services.global_config_service import get_global_config_service
from services.project_service import get_project_service
from services.coomi_version_service import check_coomi_version

router = APIRouter(tags=["sys"])

_FEEDBACK_DEFAULT_URL = "https://updates.septemc.com/storydex/feedback/api"
_FEEDBACK_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
_FEEDBACK_MAX_IMAGES = 4
_FEEDBACK_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_SENSITIVE_FEEDBACK_KEYS = {
    "apikey", "api_key", "authorization", "token", "access_token",
    "prompt", "conversation", "messages", "manuscript", "content",
}
_SENSITIVE_FEEDBACK_KEY_MARKERS = (
    "apikey", "authorization", "token", "secret", "prompt", "conversation",
    "messages", "manuscript", "content", "requestbody", "responsebody",
)


def _is_sensitive_feedback_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return normalized in _SENSITIVE_FEEDBACK_KEYS or any(
        marker in normalized for marker in _SENSITIVE_FEEDBACK_KEY_MARKERS
    )


class FeedbackImageRequest(BaseModel):
    name: str = Field(max_length=160)
    mime_type: str = Field(alias="mimeType", max_length=80)
    data_url: str = Field(alias="dataUrl", max_length=7_500_000)

    model_config = ConfigDict(populate_by_name=True)


class FeedbackSubmitRequest(BaseModel):
    source: str = Field(pattern="^(error|settings)$")
    category: str = Field(default="bug", max_length=40)
    description: str = Field(min_length=5, max_length=5000)
    contact: str = Field(default="", max_length=200)
    error_message: str = Field(default="", alias="errorMessage", max_length=5000)
    error_type: str = Field(default="", alias="errorType", max_length=160)
    error_details: dict = Field(default_factory=dict, alias="errorDetails")
    diagnostics: dict = Field(default_factory=dict)
    images: list[FeedbackImageRequest] = Field(default_factory=list, max_length=_FEEDBACK_MAX_IMAGES)

    model_config = ConfigDict(populate_by_name=True)


def _sanitize_feedback_value(value, *, depth: int = 0):
    if depth > 4:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key)[:80]: _sanitize_feedback_value(item, depth=depth + 1)
            for key, item in value.items()
            if not _is_sensitive_feedback_key(key)
        }
    if isinstance(value, list):
        return [_sanitize_feedback_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:4000]


def _validated_feedback_images(images: list[FeedbackImageRequest]) -> list[dict]:
    output = []
    for image in images:
        mime = image.mime_type.strip().lower()
        prefix = f"data:{mime};base64,"
        if mime not in _FEEDBACK_IMAGE_MIMES or not image.data_url.startswith(prefix):
            raise HTTPException(status_code=422, detail="仅支持 PNG、JPEG 或 WebP 图片。")
        try:
            decoded = base64.b64decode(image.data_url[len(prefix):], validate=True)
        except (ValueError, binascii.Error) as error:
            raise HTTPException(status_code=422, detail="反馈图片编码无效。") from error
        if not decoded or len(decoded) > _FEEDBACK_MAX_IMAGE_BYTES:
            raise HTTPException(status_code=422, detail="每张反馈图片必须小于 5 MB。")
        output.append({
            "name": os.path.basename(image.name)[:160],
            "mimeType": mime,
            "dataBase64": base64.b64encode(decoded).decode("ascii"),
        })
    return output


async def _forward_feedback(payload: dict) -> dict:
    endpoint = os.environ.get("STORYDEX_FEEDBACK_URL", _FEEDBACK_DEFAULT_URL).strip()
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"反馈服务器暂时不可用：{error}") from error
    return body if isinstance(body, dict) else {}


@router.post("/sys/feedback", response_model=ApiEnvelope)
async def submit_feedback(request: FeedbackSubmitRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    allowed_diagnostics = {
        key: request.diagnostics.get(key)
        for key in (
            "storydexVersion", "coomiVersion", "platform", "provider", "model",
            "traceId", "sessionId", "updateState", "runtime",
        )
        if request.diagnostics.get(key) not in (None, "")
    }
    remote_payload = {
        "schemaVersion": 1,
        "submissionId": trace_id,
        "submittedAt": datetime.now(timezone.utc).isoformat(),
        "source": request.source,
        "category": request.category,
        "description": request.description,
        "contact": request.contact,
        "error": {
            "message": request.error_message,
            "type": request.error_type,
            "details": _sanitize_feedback_value(request.error_details),
        } if request.source == "error" else None,
        "diagnostics": _sanitize_feedback_value(allowed_diagnostics),
        "images": _validated_feedback_images(request.images),
        "privacy": {"conversationIncluded": False, "projectFilesIncluded": False},
    }
    result = await _forward_feedback(remote_payload)
    data = {"feedbackId": str(result.get("id") or result.get("feedbackId") or trace_id)}
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data, trace=trace, audit=[{"action": "submit_feedback"}])


class UIPreferencesResponse(BaseModel):
    theme: str = "default"
    active_activity: str = Field(alias="activeActivity", default="resources")
    workbench_mode: str = Field(alias="workbenchMode", default="storydex")
    sidebar_width: int = Field(alias="sidebarWidth", default=320)
    sidebar_collapsed: bool = Field(alias="sidebarCollapsed", default=False)
    agent_collapsed: bool = Field(alias="agentCollapsed", default=False)
    agent_width: int = Field(alias="agentWidth", default=560)
    left_pane_font_scale: int = Field(alias="leftPaneFontScale", default=100)
    center_pane_font_scale: int = Field(alias="centerPaneFontScale", default=100)
    right_pane_font_scale: int = Field(alias="rightPaneFontScale", default=100)
    font_family: str = Field(alias="fontFamily", default="system")
    file_font_size: int = Field(alias="fileFontSize", default=16)
    player_font_size: int = Field(alias="playerFontSize", default=14)
    updated_at: str = Field(alias="updatedAt", default="")

    model_config = ConfigDict(populate_by_name=True)


class UIPreferencesUpdateRequest(BaseModel):
    theme: str = "default"
    active_activity: str = Field(alias="activeActivity", default="resources")
    workbench_mode: str = Field(alias="workbenchMode", default="storydex")
    sidebar_width: int = Field(alias="sidebarWidth", default=320)
    sidebar_collapsed: bool = Field(alias="sidebarCollapsed", default=False)
    agent_collapsed: bool = Field(alias="agentCollapsed", default=False)
    agent_width: int = Field(alias="agentWidth", default=560)
    left_pane_font_scale: int = Field(alias="leftPaneFontScale", default=100)
    center_pane_font_scale: int = Field(alias="centerPaneFontScale", default=100)
    right_pane_font_scale: int = Field(alias="rightPaneFontScale", default=100)
    font_family: str = Field(alias="fontFamily", default="system")
    file_font_size: int = Field(alias="fileFontSize", default=16)
    player_font_size: int = Field(alias="playerFontSize", default=14)

    model_config = ConfigDict(populate_by_name=True)


class RecentProjectResponse(BaseModel):
    project_name: str = Field(alias="projectName")
    workspace_root: str = Field(alias="workspaceRoot")
    opened_at: str = Field(alias="openedAt")

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceStateResponse(BaseModel):
    last_project_path: str = Field(alias="lastProjectPath", default="")
    recent_projects: list[RecentProjectResponse] = Field(default_factory=list, alias="recentProjects")
    updated_at: str = Field(alias="updatedAt", default="")

    model_config = ConfigDict(populate_by_name=True)


class AgentSettingsResponse(BaseModel):
    coomi_memory_enabled: bool = Field(alias="coomiMemoryEnabled", default=True)
    wiki_context_enabled: bool = Field(alias="wikiContextEnabled", default=True)
    updated_at: str = Field(alias="updatedAt", default="")

    model_config = ConfigDict(populate_by_name=True)


class AgentSettingsUpdateRequest(BaseModel):
    coomi_memory_enabled: bool = Field(alias="coomiMemoryEnabled")
    wiki_context_enabled: bool = Field(alias="wikiContextEnabled")

    model_config = ConfigDict(populate_by_name=True)


class SystemBootstrapResponse(BaseModel):
    global_root: str = Field(alias="globalRoot")
    ui_preferences: UIPreferencesResponse = Field(alias="uiPreferences")
    workspace_state: WorkspaceStateResponse = Field(alias="workspaceState")

    model_config = ConfigDict(populate_by_name=True)


@router.get("/sys/health", response_model=ApiEnvelope)
def health_check() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    settings = get_settings()
    project = get_project_service().current_project()
    coomi_version = check_coomi_version()
    data = {
        "status": "ok",
        "service": settings.app_name,
        "time": datetime.now(timezone.utc).isoformat(),
        "workspaceRoot": project["workspaceRoot"],
        "storydexRoot": project["storydexRoot"],
        "projectName": project["projectName"],
        "hasStorydexConfig": project["hasStorydexConfig"],
        "requiresInitialization": project["requiresInitialization"],
        "missingDirectories": project["missingDirectories"],
        "frontendStaticMode": settings.serve_frontend_static,
        "coomiVersion": coomi_version,
        "warnings": coomi_version["warnings"],
        "memoryUsageMb": _process_memory_usage_mb(),
    }
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data, trace=trace, audit=[])


def _process_memory_usage_mb() -> int | None:
    """Read resident memory without requiring a third-party package."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            get_current_process = kernel32.GetCurrentProcess
            get_current_process.argtypes = []
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info = psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL
            if not get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
                return None
            return max(0, round(counters.WorkingSetSize / (1024 * 1024)))

        statm = "/proc/self/statm"
        if os.path.exists(statm):
            with open(statm, "r", encoding="ascii") as handle:
                resident_pages = int(handle.read().split()[1])
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            return max(0, round(resident_pages * page_size / (1024 * 1024)))

        import resource

        usage = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        bytes_used = usage if sys.platform == "darwin" else usage * 1024
        return max(0, round(bytes_used / (1024 * 1024)))
    except (ImportError, OSError, ValueError, AttributeError, IndexError):
        return None


@router.get("/sys/bootstrap", response_model=ApiEnvelope)
def read_system_bootstrap() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    global_config = get_global_config_service()
    ui_preferences = global_config.read_ui_preferences()
    workspace_state = global_config.read_workspace_state()
    data = SystemBootstrapResponse(
        globalRoot=global_config.root.as_posix(),
        uiPreferences=UIPreferencesResponse(**ui_preferences),
        workspaceState=WorkspaceStateResponse(**workspace_state),
    )
    audit = [{"action": "read_system_bootstrap"}]
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data.model_dump(by_alias=True), trace=trace, audit=audit)


@router.get("/sys/ui-preferences", response_model=ApiEnvelope)
def read_ui_preferences() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    payload = get_global_config_service().read_ui_preferences()
    data = UIPreferencesResponse(**payload)
    audit = [{"action": "read_ui_preferences"}]
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data.model_dump(by_alias=True), trace=trace, audit=audit)


@router.put("/sys/ui-preferences", response_model=ApiEnvelope)
def update_ui_preferences(payload: UIPreferencesUpdateRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    updated = get_global_config_service().write_ui_preferences(payload.model_dump(by_alias=True))
    data = UIPreferencesResponse(**updated)
    audit = [{"action": "update_ui_preferences"}]
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data.model_dump(by_alias=True), trace=trace, audit=audit)


@router.get("/sys/agent-settings", response_model=ApiEnvelope)
def read_agent_settings() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    payload = get_global_config_service().read_agent_settings()
    data = AgentSettingsResponse(**payload)
    audit = [{"action": "read_agent_settings"}]
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data.model_dump(by_alias=True), trace=trace, audit=audit)


@router.put("/sys/agent-settings", response_model=ApiEnvelope)
def update_agent_settings(payload: AgentSettingsUpdateRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    updated = get_global_config_service().write_agent_settings(payload.model_dump(by_alias=True))
    data = AgentSettingsResponse(**updated)
    audit = [{"action": "update_agent_settings"}]
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data.model_dump(by_alias=True), trace=trace, audit=audit)


@router.get("/sys/workspace-state", response_model=ApiEnvelope)
def read_workspace_state() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    payload = get_global_config_service().read_workspace_state()
    data = WorkspaceStateResponse(**payload)
    audit = [{"action": "read_workspace_state"}]
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data.model_dump(by_alias=True), trace=trace, audit=audit)

