from __future__ import annotations

import base64
from time import perf_counter
from typing import Any, Dict, Optional, Union
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from services.editor_service import EditorService
from services.diagnostics_service import get_diagnostics_service
from services.git_service import get_git_service
from services.project_service import get_project_service
from services.story_project_service import get_story_project_service

router = APIRouter(tags=["file"])
editor_service = EditorService()
project_service = get_project_service()
story_project_service = get_story_project_service()
git_service = get_git_service()


class FileReadRequest(BaseModel):
    relative_path: str = Field(alias="relativePath")
    offset: Optional[int] = Field(default=None, ge=0)
    limit: Optional[int] = Field(default=None, ge=1, le=2000)

    model_config = ConfigDict(populate_by_name=True)


class FileWriteRequest(BaseModel):
    relative_path: str = Field(alias="relativePath")
    content: str

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceCreateFileRequest(BaseModel):
    relative_path: str = Field(alias="relativePath")
    content: str = ""

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceCreateDirectoryRequest(BaseModel):
    relative_path: str = Field(alias="relativePath")

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceImportFileItem(BaseModel):
    name: str
    content_base64: str = Field(alias="contentBase64")

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceImportFilesRequest(BaseModel):
    target_directory: str = Field(alias="targetDirectory")
    files: list[WorkspaceImportFileItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceRenameRequest(BaseModel):
    from_relative_path: str = Field(alias="fromRelativePath")
    to_relative_path: str = Field(alias="toRelativePath")

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceDeleteRequest(BaseModel):
    relative_path: str = Field(alias="relativePath")

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceTransferRequest(BaseModel):
    from_relative_path: str = Field(alias="fromRelativePath")
    to_relative_path: str = Field(alias="toRelativePath")

    model_config = ConfigDict(populate_by_name=True)


class ProjectPathRequest(BaseModel):
    project_path: str = Field(alias="projectPath")

    model_config = ConfigDict(populate_by_name=True)


class FileContentResponse(BaseModel):
    relative_path: str = Field(alias="relativePath")
    content: str
    size: int = 0
    word_count: int = Field(default=0, alias="wordCount")
    updated_at: str = Field(alias="updatedAt", default="")
    extension: str = ""
    kind: str = "file"
    line_count: int = Field(default=0, alias="lineCount")
    line_count_exact: bool = Field(default=True, alias="lineCountExact")
    offset: Optional[int] = None
    limit: Optional[int] = None
    is_partial_view: bool = Field(default=False, alias="isPartialView")
    full_content_sha256: str = Field(default="", alias="fullContentSha256")
    mtime_ms: Optional[int] = Field(default=None, alias="mtimeMs")
    media: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class WorkspacePathInfoResponse(BaseModel):
    relative_path: str = Field(alias="relativePath")
    exists: bool
    kind: str = "file"
    size: int = 0
    mtime_ms: Optional[int] = Field(default=None, alias="mtimeMs")
    sha256: str = ""

    model_config = ConfigDict(populate_by_name=True)


class DiagnosticsRequest(BaseModel):
    relative_paths: list[str] = Field(default_factory=list, alias="relativePaths")

    model_config = ConfigDict(populate_by_name=True)


class ProjectInfoResponse(BaseModel):
    project_name: str = Field(alias="projectName")
    workspace_root: str = Field(alias="workspaceRoot")
    storydex_root: str = Field(alias="storydexRoot")
    storydex_dir_name: str = Field(alias="storydexDirName")
    has_storydex_config: bool = Field(alias="hasStorydexConfig")
    requires_initialization: bool = Field(alias="requiresInitialization")
    missing_directories: list[str] = Field(alias="missingDirectories", default_factory=list)
    project_state: str = Field(alias="projectState")
    opened_at: str = Field(alias="openedAt")

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceTreeResponse(BaseModel):
    workspace_root: str = Field(alias="workspaceRoot")
    storydex_root: str = Field(alias="storydexRoot")
    project_name: str = Field(alias="projectName")
    has_storydex_config: bool = Field(alias="hasStorydexConfig")
    requires_initialization: bool = Field(alias="requiresInitialization")
    missing_directories: list[str] = Field(alias="missingDirectories", default_factory=list)
    opened_at: str = Field(alias="openedAt")
    default_file: Optional[str] = Field(default=None, alias="defaultFile")
    roots: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class StoryProjectSettingsResponse(BaseModel):
    segment_extension: str = Field(alias="segmentExtension")
    max_segments_per_chapter: int = Field(default=3, alias="maxSegmentsPerChapter")
    auto_name_chapter_title: bool = Field(default=False, alias="autoNameChapterTitle")
    context_concision_min_calls: int = Field(default=1, alias="contextConcisionMinCalls")
    context_concision_max_calls: int = Field(default=2, alias="contextConcisionMaxCalls")
    context_concision_max_input_tokens: int = Field(default=32000, alias="contextConcisionMaxInputTokens")
    story_fragment_count: int = Field(default=1, alias="storyFragmentCount")
    story_fragment_word_count: int = Field(default=2000, alias="storyFragmentWordCount")
    auto_update_variables: bool = Field(default=False, alias="autoUpdateVariables")
    auto_update_wiki: bool = Field(default=False, alias="autoUpdateWiki")
    auto_update_variables_note: str = Field(default="", alias="autoUpdateVariablesNote")
    agent_commit_prompt_enabled: bool = Field(default=True, alias="agentCommitPromptEnabled")
    chapter_completion: Dict[str, bool] = Field(default_factory=dict, alias="chapterCompletion")
    updated_at: str = Field(alias="updatedAt", default="")
    settings_path: str = Field(alias="settingsPath", default="")

    model_config = ConfigDict(populate_by_name=True)


class StoryProjectSettingsUpdateRequest(BaseModel):
    segment_extension: str = Field(alias="segmentExtension")
    max_segments_per_chapter: Optional[int] = Field(default=None, alias="maxSegmentsPerChapter")
    auto_name_chapter_title: Optional[bool] = Field(default=None, alias="autoNameChapterTitle")
    context_concision_min_calls: Optional[int] = Field(default=None, alias="contextConcisionMinCalls")
    context_concision_max_calls: Optional[int] = Field(default=None, alias="contextConcisionMaxCalls")
    context_concision_max_input_tokens: Optional[int] = Field(default=None, alias="contextConcisionMaxInputTokens")
    story_fragment_count: Optional[int] = Field(default=None, alias="storyFragmentCount")
    story_fragment_word_count: Optional[int] = Field(default=None, alias="storyFragmentWordCount")
    auto_update_variables: Optional[bool] = Field(default=None, alias="autoUpdateVariables")
    auto_update_wiki: Optional[bool] = Field(default=None, alias="autoUpdateWiki")
    agent_commit_prompt_enabled: Optional[bool] = Field(default=None, alias="agentCommitPromptEnabled")
    chapter_completion: Dict[str, bool] = Field(default_factory=dict, alias="chapterCompletion")

    model_config = ConfigDict(populate_by_name=True)


class StoryChapterTemplateResponse(BaseModel):
    id: str
    name: str
    relative_path: str = Field(alias="relativePath")
    description: str = ""
    chapter_mode: str = Field(default="directory", alias="chapterMode")
    chapter_name_pattern: str = Field(default="", alias="chapterNamePattern")
    segment_naming: str = Field(default="001.md", alias="segmentNaming")

    model_config = ConfigDict(populate_by_name=True)


class StoryChapterCompletionRequest(BaseModel):
    chapter_path: str = Field(alias="chapterPath")
    completed: bool

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceGitCommitRequest(BaseModel):
    message: str = ""

    model_config = ConfigDict(populate_by_name=True)


class WorkspaceGitRestoreRequest(BaseModel):
    commit_id: str = Field(alias="commitId")
    create_backup: bool = Field(default=True, alias="createBackup")

    model_config = ConfigDict(populate_by_name=True)


def _build_trace(*, started: float, trace_id: str, tool_calls: int = 1) -> ApiTrace:
    return ApiTrace(
        traceId=trace_id,
        durationMs=int((perf_counter() - started) * 1000),
        toolCalls=tool_calls,
    )


def _build_story_settings_payload() -> Dict[str, Any]:
    workspace_root = project_service.workspace_root
    project_settings = story_project_service.read_project_settings(workspace_root)
    progress = story_project_service.read_chapter_progress(workspace_root)
    chapter_entries = progress.get("chapters") if isinstance(progress.get("chapters"), dict) else {}
    chapter_completion = {
        str(relative_path): bool(item.get("completed", False))
        for relative_path, item in chapter_entries.items()
        if isinstance(item, dict)
    }
    settings_path = story_project_service.project_settings_path(workspace_root).relative_to(workspace_root).as_posix()
    updated_at_candidates = [
        str(project_settings.get("updatedAt") or "").strip(),
        str(progress.get("updatedAt") or "").strip(),
    ]
    updated_at = next((value for value in updated_at_candidates if value), "")
    return {
        "segmentExtension": "." + str(project_settings.get("storySegmentFormat") or "md"),
        "maxSegmentsPerChapter": _bounded_int(project_settings.get("maxSegmentsPerChapter"), default=3, minimum=1, maximum=99),
        "autoNameChapterTitle": bool(project_settings.get("autoNameChapterTitle", False)),
        "contextConcisionMinCalls": _bounded_int(project_settings.get("contextConcisionMinCalls"), default=1, minimum=1, maximum=20),
        "contextConcisionMaxCalls": _bounded_int(project_settings.get("contextConcisionMaxCalls"), default=2, minimum=1, maximum=20),
        "contextConcisionMaxInputTokens": _bounded_int(
            project_settings.get("contextConcisionMaxInputTokens"),
            default=32000,
            minimum=1000,
            maximum=500000,
        ),
        "storyFragmentCount": _bounded_int(project_settings.get("storyFragmentCount"), default=1, minimum=1, maximum=20),
        "storyFragmentWordCount": _bounded_int(
            project_settings.get("storyFragmentWordCount"),
            default=2000,
            minimum=100,
            maximum=20000,
        ),
        "autoUpdateVariables": bool(project_settings.get("autoUpdateVariables", False)),
        "autoUpdateWiki": bool(project_settings.get("autoUpdateWiki", False)),
        "agentCommitPromptEnabled": bool(project_settings.get("agentCommitPromptEnabled", True)),
        "autoUpdateVariablesNote": str(
            project_settings.get("autoUpdateVariablesNote")
            or "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。"
        ),
        "chapterCompletion": chapter_completion,
        "updatedAt": updated_at,
        "settingsPath": settings_path,
    }


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _build_git_summary_payload() -> Dict[str, Any]:
    return git_service.read_summary(project_service.workspace_root)


def _build_git_diff_payload() -> Dict[str, Any]:
    return git_service.read_diff(project_service.workspace_root)


@router.get("/workspace/tree", response_model=ApiEnvelope)
def read_workspace_tree() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    tree = editor_service.list_workspace_tree()
    data = WorkspaceTreeResponse(**tree)
    audit = [
        {
            "action": "read_workspace_tree",
            "rootCount": len(data.roots),
            "workspaceRoot": data.workspace_root,
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/workspace/project", response_model=ApiEnvelope)
def read_current_project() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    project = ProjectInfoResponse(**project_service.current_project())
    audit = [{"action": "read_current_project", "workspaceRoot": project.workspace_root}]
    return success_response(
        data=project.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/project/open", response_model=ApiEnvelope)
def open_project(payload: ProjectPathRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    project = ProjectInfoResponse(**project_service.open_project(payload.project_path))
    audit = [
        {
            "action": "open_project",
            "workspaceRoot": project.workspace_root,
            "requiresInitialization": project.requires_initialization,
        }
    ]
    return success_response(
        data=project.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/project/create", response_model=ApiEnvelope)
def create_project(payload: ProjectPathRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    project = ProjectInfoResponse(**project_service.create_project(payload.project_path))
    audit = [{"action": "create_project", "workspaceRoot": project.workspace_root}]
    return success_response(
        data=project.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/project/initialize", response_model=ApiEnvelope)
def initialize_project(payload: Optional[ProjectPathRequest] = None) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    target_path = payload.project_path if payload is not None else ""
    project = ProjectInfoResponse(**project_service.initialize_project(target_path))
    audit = [{"action": "initialize_project", "workspaceRoot": project.workspace_root}]
    return success_response(
        data=project.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/file/read", response_model=ApiEnvelope)
def read_file(payload: FileReadRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    document = editor_service.read_document(payload.relative_path, offset=payload.offset, limit=payload.limit)
    data = FileContentResponse(**document)
    audit = [{"action": "read_file", "relativePath": payload.relative_path}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/diagnostics", response_model=ApiEnvelope)
def read_workspace_diagnostics(payload: DiagnosticsRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    diagnostics = get_diagnostics_service().diagnose_paths(payload.relative_paths)
    audit = [{"action": "read_workspace_diagnostics", "count": len(diagnostics)}]
    return success_response(
        data={"items": diagnostics},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/workspace/git/summary", response_model=ApiEnvelope)
def read_workspace_git_summary() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = _build_git_summary_payload()
    audit = [
        {
            "action": "read_workspace_git_summary",
            "initialized": bool(data.get("initialized")),
            "changedCount": len(data.get("changedFiles") if isinstance(data.get("changedFiles"), list) else []),
        }
    ]
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/workspace/git/diff", response_model=ApiEnvelope)
def read_workspace_git_diff() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = _build_git_diff_payload()
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    audit = [
        {
            "action": "read_workspace_git_diff",
            "initialized": bool(data.get("initialized")),
            "fileCount": int(totals.get("files") or 0),
            "added": int(totals.get("added") or 0),
            "removed": int(totals.get("removed") or 0),
        }
    ]
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/git/init", response_model=ApiEnvelope)
def initialize_workspace_git_repository() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = git_service.initialize_repository(project_service.workspace_root)
    audit = [{"action": "initialize_workspace_git_repository", "branch": str(data.get("branch") or "")}]
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/git/commit", response_model=ApiEnvelope)
def commit_workspace_git_changes(payload: WorkspaceGitCommitRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    result = git_service.commit_all(project_service.workspace_root, message=payload.message)
    audit = [
        {
            "action": "commit_workspace_git_changes",
            "created": bool(result.get("created")),
            "message": str(payload.message or "").strip(),
        }
    ]
    return success_response(
        data=result,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/git/restore", response_model=ApiEnvelope)
def restore_workspace_git_commit(payload: WorkspaceGitRestoreRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    result = git_service.restore_to_commit(
        project_service.workspace_root,
        commit_id=payload.commit_id,
        create_backup=payload.create_backup,
    )
    audit = [
        {
            "action": "restore_workspace_git_commit",
            "commitId": payload.commit_id,
            "createBackup": payload.create_backup,
            "restored": bool(result.get("restored")),
            "backupRef": str(result.get("backupRef") or ""),
        }
    ]
    return success_response(
        data=result,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/workspace/story/settings", response_model=ApiEnvelope)
def read_story_project_settings() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = StoryProjectSettingsResponse(**_build_story_settings_payload())
    audit = [{"action": "read_story_project_settings", "settingsPath": data.settings_path}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.put("/workspace/story/settings", response_model=ApiEnvelope)
def update_story_project_settings(payload: StoryProjectSettingsUpdateRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    workspace_root = project_service.workspace_root
    settings_patch: Dict[str, Any] = {
        "storySegmentFormat": str(payload.segment_extension or "").strip().lstrip("."),
    }
    optional_fields = {
        "maxSegmentsPerChapter": payload.max_segments_per_chapter,
        "autoNameChapterTitle": payload.auto_name_chapter_title,
        "contextConcisionMinCalls": payload.context_concision_min_calls,
        "contextConcisionMaxCalls": payload.context_concision_max_calls,
        "contextConcisionMaxInputTokens": payload.context_concision_max_input_tokens,
        "storyFragmentCount": payload.story_fragment_count,
        "storyFragmentWordCount": payload.story_fragment_word_count,
        "autoUpdateVariables": payload.auto_update_variables,
        "autoUpdateWiki": payload.auto_update_wiki,
        "agentCommitPromptEnabled": payload.agent_commit_prompt_enabled,
    }
    for key, value in optional_fields.items():
        if value is not None:
            settings_patch[key] = value
    if "autoUpdateVariables" in settings_patch:
        settings_patch["autoUpdateVariablesNote"] = "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。"
    story_project_service.write_project_settings(workspace_root, settings_patch)
    story_project_service.replace_chapter_completion(workspace_root, dict(payload.chapter_completion))
    data = StoryProjectSettingsResponse(**_build_story_settings_payload())
    audit = [{"action": "update_story_project_settings", "settingsPath": data.settings_path}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/workspace/story/templates/chapters", response_model=ApiEnvelope)
def read_workspace_story_chapter_templates() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    items = [
        StoryChapterTemplateResponse(**item)
        for item in story_project_service.list_chapter_templates(project_service.workspace_root)
    ]
    audit = [{"action": "read_workspace_story_chapter_templates", "count": len(items)}]
    return success_response(
        data={"items": [item.model_dump(by_alias=True) for item in items]},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.put("/workspace/story/chapters/completion", response_model=ApiEnvelope)
def update_story_chapter_completion(payload: StoryChapterCompletionRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    story_project_service.set_chapter_completed(
        project_service.workspace_root,
        payload.chapter_path,
        payload.completed,
    )
    data = StoryProjectSettingsResponse(**_build_story_settings_payload())
    audit = [
        {
            "action": "update_story_chapter_completion",
            "chapterPath": payload.chapter_path,
            "completed": payload.completed,
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/file/write", response_model=ApiEnvelope)
def write_file(payload: FileWriteRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    document = editor_service.write_text(payload.relative_path, payload.content)
    data = FileContentResponse(**document)
    audit = [
        {
            "action": "write_file",
            "relativePath": payload.relative_path,
            "size": len(payload.content.encode("utf-8")),
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/file/create", response_model=ApiEnvelope)
def create_workspace_file(payload: WorkspaceCreateFileRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    document = editor_service.create_file(payload.relative_path, payload.content)
    data = FileContentResponse(**document)
    audit = [{"action": "create_workspace_file", "relativePath": payload.relative_path}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/directory/create", response_model=ApiEnvelope)
def create_workspace_directory(payload: WorkspaceCreateDirectoryRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    metadata = editor_service.create_directory(payload.relative_path)
    data = WorkspacePathInfoResponse(**metadata)
    audit = [{"action": "create_workspace_directory", "relativePath": payload.relative_path}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/files/import", response_model=ApiEnvelope)
def import_workspace_files(payload: WorkspaceImportFilesRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    items: list[dict[str, Any]] = []
    for item in payload.files:
        encoded = str(item.content_base64 or "").strip()
        if "," in encoded and encoded.lower().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        content = base64.b64decode(encoded, validate=True)
        metadata = editor_service.import_file_bytes(payload.target_directory, item.name, content)
        data = WorkspacePathInfoResponse(**metadata)
        items.append(data.model_dump(by_alias=True))

    audit = [
        {
            "action": "import_workspace_files",
            "targetDirectory": payload.target_directory,
            "count": len(items),
            "relativePaths": [str(item.get("relativePath") or "") for item in items],
        }
    ]
    return success_response(
        data={"items": items},
        trace=_build_trace(started=started, trace_id=trace_id, tool_calls=max(1, len(items))),
        audit=audit,
    )


@router.post("/workspace/path/rename", response_model=ApiEnvelope)
def rename_workspace_path(payload: WorkspaceRenameRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    metadata = editor_service.rename_path(payload.from_relative_path, payload.to_relative_path)
    data = WorkspacePathInfoResponse(**metadata)
    audit = [
        {
            "action": "rename_workspace_path",
            "fromRelativePath": payload.from_relative_path,
            "toRelativePath": payload.to_relative_path,
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/path/delete", response_model=ApiEnvelope)
def delete_workspace_path(payload: WorkspaceDeleteRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    metadata = editor_service.delete_path(payload.relative_path)
    data = WorkspacePathInfoResponse(**metadata)
    audit = [{"action": "delete_workspace_path", "relativePath": payload.relative_path}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/path/copy", response_model=ApiEnvelope)
def copy_workspace_path(payload: WorkspaceTransferRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    metadata = editor_service.copy_path(payload.from_relative_path, payload.to_relative_path)
    data = WorkspacePathInfoResponse(**metadata)
    audit = [
        {
            "action": "copy_workspace_path",
            "fromRelativePath": payload.from_relative_path,
            "toRelativePath": payload.to_relative_path,
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/workspace/path/move", response_model=ApiEnvelope)
def move_workspace_path(payload: WorkspaceTransferRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    metadata = editor_service.move_path(payload.from_relative_path, payload.to_relative_path)
    data = WorkspacePathInfoResponse(**metadata)
    audit = [
        {
            "action": "move_workspace_path",
            "fromRelativePath": payload.from_relative_path,
            "toRelativePath": payload.to_relative_path,
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )
