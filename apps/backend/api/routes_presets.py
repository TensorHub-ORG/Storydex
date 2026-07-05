"""T-D · 结构化预设管理 API。

读写 ``.storydex/presets/{active,library}/<stem>.md`` + ``<stem>.preset.json``。
所有路径均相对 workspace_root；后端拒绝 ``..`` 穿越。
"""
from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiTrace, success_response
from core.exceptions import StorydexError
from services.preset_compiler import compile_preset
from services.preset_schema import PresetDocument, load_preset_sidecar
from services.project_service import get_project_service
from services.silly_tavern_preset_importer import convert_silly_tavern_preset, safe_preset_filename_stem
from services.story_project_service import get_story_project_service

router = APIRouter(tags=["preset"])
project_service = get_project_service()
story_project_service = get_story_project_service()


def _workspace_root() -> Path:
    project = project_service.current_project()
    return Path(project["workspaceRoot"]).resolve()


def _trace(request: Request) -> ApiTrace:
    return ApiTrace(traceId=request.headers.get("x-trace-id") or str(uuid4()))


def _resolve_safe_md_path(workspace_root: Path, relative: str) -> Path:
    """把 stem 或相对路径解析为 active/library 下的 .md 路径，拒绝穿越。"""
    raw = (relative or "").strip().lstrip("/")
    if not raw or ".." in raw.split("/"):
        raise HTTPException(status_code=400, detail="invalid preset path")
    # 若是 stem（无后缀且无目录），在 active 优先查找
    if "/" not in raw and not raw.lower().endswith(".md"):
        for section in ("active", "library"):
            candidate = workspace_root / ".storydex" / "presets" / section / f"{raw}.md"
            if candidate.exists():
                return candidate.resolve()
        raise HTTPException(status_code=404, detail=f"preset not found: {raw}")
    # 否则要求是相对路径，且必须在 presets 目录下
    candidate = (workspace_root / raw).resolve()
    preset_root = (workspace_root / ".storydex" / "presets").resolve()
    try:
        candidate.relative_to(preset_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="preset path must be under .storydex/presets/")
    if candidate.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="preset path must end with .md")
    return candidate


class PresetActivateRequest(BaseModel):
    relative_path: str = Field(alias="relativePath")
    model_config = ConfigDict(populate_by_name=True)


class PresetDocumentEnvelope(BaseModel):
    document: PresetDocument
    warnings: List[str] = Field(default_factory=list)


class PresetCompileRequest(BaseModel):
    document: Optional[PresetDocument] = None
    preset_overrides: Optional[Dict[str, Any]] = Field(default=None, alias="presetOverrides")
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class SillyTavernPresetImportFile(BaseModel):
    name: str
    content_base64: str = Field(alias="contentBase64")
    model_config = ConfigDict(populate_by_name=True)


class SillyTavernPresetImportRequest(BaseModel):
    files: List[SillyTavernPresetImportFile] = Field(default_factory=list)
    model_config = ConfigDict(populate_by_name=True)


@router.get("/presets/list")
def list_presets(request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    listing = story_project_service.list_presets(root)
    pointer = story_project_service.read_active_pointer(root)
    return success_response(
        data={
            "active": listing.get("active", []),
            "library": listing.get("library", []),
            "activeMainPreset": pointer.get("activeMainPreset", ""),
        },
        trace=_trace(request),
    ).model_dump(by_alias=True)


@router.post("/presets/import/sillytavern")
def import_silly_tavern_presets(payload: SillyTavernPresetImportRequest, request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    library_dir = story_project_service.preset_root(root) / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    items: List[Dict[str, Any]] = []

    for file_item in payload.files:
        raw_bytes = _decode_base64_file(file_item.content_base64)
        converted = convert_silly_tavern_preset(raw_bytes, filename=file_item.name)
        stem = _unique_library_stem(
            library_dir,
            safe_preset_filename_stem(converted.title, fallback=Path(file_item.name).stem),
        )
        md_path = library_dir / f"{stem}.md"
        md_path.write_text(converted.markdown, encoding="utf-8")
        story_project_service.write_preset_sidecar(md_path, converted.document)
        sidecar_path = md_path.with_name(md_path.stem + ".preset.json")
        items.append(
            _build_import_item(file_item.name, converted, root, md_path, sidecar_path)
        )

    return success_response(
        data={"items": items},
        trace=_trace(request),
        audit=[
            {
                "action": "import_silly_tavern_presets",
                "count": len(items),
                "relativePaths": [str(item.get("relativePath") or "") for item in items],
            }
        ],
    ).model_dump(by_alias=True)


@router.post("/presets/import/preview")
def preview_silly_tavern_import(payload: SillyTavernPresetImportRequest, request: Request) -> Dict[str, Any]:
    """导入预览：解析但不写盘，返回解析结果供前端展示。"""
    items: List[Dict[str, Any]] = []

    for file_item in payload.files:
        raw_bytes = _decode_base64_file(file_item.content_base64)
        converted = convert_silly_tavern_preset(raw_bytes, filename=file_item.name)
        # 预览不写盘，relativePath/sidecarPath 为空
        items.append(_build_import_item(file_item.name, converted, None, None, None, preview=True))

    return success_response(
        data={"items": items},
        trace=_trace(request),
    ).model_dump(by_alias=True)


@router.get("/presets/_schema")
def get_preset_schema(request: Request) -> Dict[str, Any]:
    """v1.3: 返回 PresetDocument 的 JSON schema，让前端 schema-driven form 自动渲染所有字段。"""
    schema = PresetDocument.model_json_schema()
    return success_response(
        data={"schema": schema, "version": 1},
        trace=_trace(request),
    ).model_dump(by_alias=True)


@router.get("/presets/active")
def get_active_preset(request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    pointer = story_project_service.read_active_pointer(root)
    rel = pointer.get("activeMainPreset", "") if isinstance(pointer, dict) else ""
    document: Optional[PresetDocument] = None
    warnings: List[str] = []
    if rel:
        md_path = (root / rel).resolve()
        if md_path.exists():
            from services.preset_schema import find_sidecar_path

            sidecar = find_sidecar_path(md_path)
            if sidecar.exists():
                document, warnings = load_preset_sidecar(sidecar)
    if document is None:
        document = PresetDocument()
    return success_response(
        data={
            "activeMainPreset": rel,
            "document": document.model_dump(mode="json", by_alias=True),
            "warnings": warnings,
        },
        trace=_trace(request),
    ).model_dump(by_alias=True)


@router.get("/presets/{name:path}/document")
def get_preset_document(name: str, request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    md_path = _resolve_safe_md_path(root, name)
    from services.preset_schema import find_sidecar_path

    sidecar = find_sidecar_path(md_path)
    warnings: List[str] = []
    if sidecar.exists():
        document, warnings = load_preset_sidecar(sidecar)
    else:
        document = PresetDocument()
        warnings.append("no sidecar JSON; returning empty document")
    return success_response(
        data={
            "relativePath": md_path.relative_to(root).as_posix(),
            "document": document.model_dump(mode="json", by_alias=True),
            "warnings": warnings,
        },
        trace=_trace(request),
    ).model_dump(by_alias=True)


@router.post("/presets/{name:path}/compile")
def compile_preset_document(
    name: str,
    request: Request,
    payload: Optional[PresetCompileRequest] = None,
) -> Dict[str, Any]:
    root = _workspace_root()
    compile_payload = payload or PresetCompileRequest()
    md_path, document, load_warnings = _load_compile_document(root, name, compile_payload)
    result = compile_preset(document, overrides=compile_payload.preset_overrides)
    data = result.model_dump(mode="json", by_alias=True)
    if load_warnings:
        data["warnings"] = [*load_warnings, *data.get("warnings", [])]
    data["relativePath"] = md_path.relative_to(root).as_posix()
    return success_response(data=data, trace=_trace(request)).model_dump(by_alias=True)


@router.post("/presets/{name:path}/risk-check")
def risk_check_preset_document(
    name: str,
    request: Request,
    payload: Optional[PresetCompileRequest] = None,
) -> Dict[str, Any]:
    # Deprecated: 与 /compile 实现完全一致，前端应统一调 /compile（它已返回 risks）
    root = _workspace_root()
    compile_payload = payload or PresetCompileRequest()
    md_path, document, load_warnings = _load_compile_document(root, name, compile_payload)
    result = compile_preset(document, overrides=compile_payload.preset_overrides)
    data = result.model_dump(mode="json", by_alias=True)
    if load_warnings:
        data["warnings"] = [*load_warnings, *data.get("warnings", [])]
    data["relativePath"] = md_path.relative_to(root).as_posix()
    response = success_response(data=data, trace=_trace(request)).model_dump(by_alias=True)
    # 标记 deprecated（前端可据此提示）
    return response


@router.put("/presets/{name:path}/document")
def put_preset_document(name: str, payload: PresetDocument, request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    md_path = _resolve_safe_md_path(root, name)
    story_project_service.write_preset_sidecar(md_path, payload)
    return success_response(
        data={
            "relativePath": md_path.relative_to(root).as_posix(),
            "ok": True,
        },
        trace=_trace(request),
    ).model_dump(by_alias=True)


@router.patch("/presets/{name:path}/params")
def patch_preset_params(name: str, payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    md_path = _resolve_safe_md_path(root, name)
    existing = story_project_service.load_preset_sidecar(md_path) or PresetDocument()
    merged = existing.model_dump(mode="python")
    _deep_merge(merged, payload or {})
    try:
        new_doc = PresetDocument.model_validate(merged)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid preset patch: {exc}")
    story_project_service.write_preset_sidecar(md_path, new_doc)
    return success_response(
        data={"ok": True, "relativePath": md_path.relative_to(root).as_posix()},
        trace=_trace(request),
    ).model_dump(by_alias=True)


@router.post("/presets/{name:path}/activate")
def activate_preset(name: str, request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    md_path = _resolve_safe_md_path(root, name)
    rel = md_path.relative_to(root).as_posix()
    try:
        pointer = story_project_service.activate_preset(root, rel)
    except StorydexError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return success_response(data=pointer, trace=_trace(request)).model_dump(by_alias=True)


@router.post("/presets/{name:path}/deactivate")
def deactivate_preset(name: str, request: Request) -> Dict[str, Any]:
    root = _workspace_root()
    md_path = _resolve_safe_md_path(root, name)
    rel = md_path.relative_to(root).as_posix()
    pointer = story_project_service.deactivate_preset(root, rel)
    return success_response(data=pointer, trace=_trace(request)).model_dump(by_alias=True)


def _deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _load_compile_document(
    root: Path,
    name: str,
    payload: PresetCompileRequest,
) -> tuple[Path, PresetDocument, List[str]]:
    md_path = _resolve_safe_md_path(root, name)
    if payload.document is not None:
        return md_path, payload.document, []

    from services.preset_schema import find_sidecar_path

    sidecar = find_sidecar_path(md_path)
    if sidecar.exists():
        document, warnings = load_preset_sidecar(sidecar)
        return md_path, document, warnings
    return md_path, PresetDocument(), ["no sidecar JSON; compiling empty document"]


def _decode_base64_file(content_base64: str) -> bytes:
    encoded = str(content_base64 or "").strip()
    if "," in encoded and encoded.lower().startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid base64 content: {exc}")


def _build_import_item(
    file_name: str,
    converted: Any,
    root: Optional[Path],
    md_path: Optional[Path],
    sidecar_path: Optional[Path],
    *,
    preview: bool = False,
) -> Dict[str, Any]:
    """构造导入响应项（实际导入和预览共用）。"""
    # 从 document 提取模块列表供前端预览展示
    doc_dump = converted.document.model_dump(mode="json", by_alias=True)
    modules_meta = doc_dump.get("modules", [])
    modules_preview = [
        {
            "id": m.get("id", ""),
            "title": m.get("title", ""),
            "slot": m.get("slot", ""),
            "priority": m.get("priority", 0),
            "enabledByDefault": m.get("enabledByDefault", True),
        }
        for m in modules_meta
    ]

    item: Dict[str, Any] = {
        "name": file_name,
        "title": converted.title,
        "moduleCount": converted.module_count,
        "filteredCount": converted.filtered_count,
        "filteredBlocks": [
            {
                "name": block.name,
                "identifier": block.identifier,
                "reason": block.reason,
            }
            for block in converted.filtered_blocks
        ],
        "warnings": converted.warnings,
        "importWarnings": converted.import_warnings,
        "displayRegexes": converted.display_regexes,
        "chatSquashMeta": converted.chat_squash_meta,
        "modules": modules_preview,
        "sampling": doc_dump.get("sampling", {}),
    }

    if not preview and root is not None and md_path is not None and sidecar_path is not None:
        item["relativePath"] = md_path.relative_to(root).as_posix()
        item["sidecarPath"] = sidecar_path.relative_to(root).as_posix()
    else:
        item["relativePath"] = ""
        item["sidecarPath"] = ""

    return item


def _unique_library_stem(library_dir: Path, stem: str) -> str:
    base = safe_preset_filename_stem(stem)
    candidate = base
    index = 1
    while (library_dir / f"{candidate}.md").exists() or (library_dir / f"{candidate}.preset.json").exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate
