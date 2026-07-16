from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from core.config import get_settings
from services.global_config_service import get_global_config_service
from services.project_service import get_project_service
from services.coomi_version_service import check_coomi_version

router = APIRouter(tags=["sys"])


class UIPreferencesResponse(BaseModel):
    theme: str = "default"
    active_activity: str = Field(alias="activeActivity", default="resources")
    workbench_mode: str = Field(alias="workbenchMode", default="storydex")
    sidebar_width: int = Field(alias="sidebarWidth", default=320)
    sidebar_collapsed: bool = Field(alias="sidebarCollapsed", default=False)
    agent_collapsed: bool = Field(alias="agentCollapsed", default=False)
    agent_width: int = Field(alias="agentWidth", default=560)
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


@router.get("/sys/workspace-state", response_model=ApiEnvelope)
def read_workspace_state() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    payload = get_global_config_service().read_workspace_state()
    data = WorkspaceStateResponse(**payload)
    audit = [{"action": "read_workspace_state"}]
    trace = ApiTrace(traceId=trace_id, durationMs=int((perf_counter() - started) * 1000))
    return success_response(data=data.model_dump(by_alias=True), trace=trace, audit=audit)

