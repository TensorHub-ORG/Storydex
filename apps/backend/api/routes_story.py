from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from api.response import ApiEnvelope, ApiTrace, success_response
from core.bounded_text_io import read_text_preview as read_bounded_text_preview
from services.project_service import get_project_service
from services.story_project_service import get_story_project_service

router = APIRouter(tags=["story"])
project_service = get_project_service()
story_project_service = get_story_project_service()


class StoryProjectSettingsResponse(BaseModel):
    version: int = 1
    story_segment_format: str = Field(alias="storySegmentFormat")
    default_dialogue_quote: str = Field(alias="defaultDialogueQuote")
    segment_naming_mode: str = Field(alias="segmentNamingMode")
    max_segments_per_chapter: int = Field(alias="maxSegmentsPerChapter")
    story_fragment_count: int = Field(default=1, alias="storyFragmentCount")
    story_fragment_word_count: int = Field(default=2000, alias="storyFragmentWordCount")
    story_chapter_template_id: str = Field(default="default_chapter_directory", alias="storyChapterTemplateId")
    auto_update_variables: bool = Field(default=False, alias="autoUpdateVariables")
    auto_update_wiki: bool = Field(default=False, alias="autoUpdateWiki")
    auto_update_variables_note: str = Field(default="", alias="autoUpdateVariablesNote")
    agent_commit_prompt_enabled: bool = Field(default=True, alias="agentCommitPromptEnabled")
    auto_name_chapter_title: bool = Field(alias="autoNameChapterTitle")
    context_concision_min_calls: int = Field(alias="contextConcisionMinCalls", default=1)
    context_concision_max_calls: int = Field(alias="contextConcisionMaxCalls", default=2)
    context_concision_max_input_tokens: int = Field(alias="contextConcisionMaxInputTokens", default=32000)
    updated_at: str = Field(alias="updatedAt")
    settings_path: str = Field(alias="settingsPath")
    chapter_progress_path: str = Field(alias="chapterProgressPath")
    snapshot_root: str = Field(alias="snapshotRoot")
    current_state_root: str = Field(alias="currentStateRoot")
    memory_root: str = Field(alias="memoryRoot")

    model_config = ConfigDict(populate_by_name=True)


class StoryProjectSettingsUpdateRequest(BaseModel):
    story_segment_format: str = Field(alias="storySegmentFormat")
    default_dialogue_quote: Optional[str] = Field(default=None, alias="defaultDialogueQuote")
    segment_naming_mode: Optional[str] = Field(default=None, alias="segmentNamingMode")
    max_segments_per_chapter: Optional[int] = Field(default=None, alias="maxSegmentsPerChapter")
    story_fragment_count: Optional[int] = Field(default=None, alias="storyFragmentCount")
    story_fragment_word_count: Optional[int] = Field(default=None, alias="storyFragmentWordCount")
    story_chapter_template_id: Optional[str] = Field(default=None, alias="storyChapterTemplateId")
    auto_update_variables: Optional[bool] = Field(default=None, alias="autoUpdateVariables")
    auto_update_wiki: Optional[bool] = Field(default=None, alias="autoUpdateWiki")
    agent_commit_prompt_enabled: Optional[bool] = Field(default=None, alias="agentCommitPromptEnabled")
    auto_name_chapter_title: Optional[bool] = Field(default=None, alias="autoNameChapterTitle")
    context_concision_min_calls: Optional[int] = Field(default=None, alias="contextConcisionMinCalls")
    context_concision_max_calls: Optional[int] = Field(default=None, alias="contextConcisionMaxCalls")
    context_concision_max_input_tokens: Optional[int] = Field(default=None, alias="contextConcisionMaxInputTokens")

    model_config = ConfigDict(populate_by_name=True)


class StoryCharacterTemplateResponse(BaseModel):
    template: Dict[str, Any]
    markdown: str
    template_json_path: str = Field(alias="templateJsonPath")
    template_markdown_path: str = Field(alias="templateMarkdownPath")

    model_config = ConfigDict(populate_by_name=True)


class StoryCharacterTemplateUpdateRequest(BaseModel):
    markdown: str

    model_config = ConfigDict(populate_by_name=True)


class StoryChapterTemplateResponse(BaseModel):
    id: str
    name: str
    relative_path: str = Field(alias="relativePath")
    description: str = ""
    chapter_mode: str = Field(default="directory", alias="chapterMode")
    content_mode: str = Field(default="multi_fragment", alias="contentMode")
    chapter_name_pattern: str = Field(default="", alias="chapterNamePattern")
    segment_naming: str = Field(default="001.md", alias="segmentNaming")

    model_config = ConfigDict(populate_by_name=True)


class StoryChapterStateResponse(BaseModel):
    relative_path: str = Field(alias="relativePath")
    name: str
    display_name: str = Field(alias="displayName")
    chapter_number: int = Field(alias="chapterNumber")
    completed: bool
    updated_at: str = Field(alias="updatedAt")

    model_config = ConfigDict(populate_by_name=True)


class StoryChapterCompletionRequest(BaseModel):
    chapter_relative_path: str = Field(alias="chapterRelativePath")
    completed: bool

    model_config = ConfigDict(populate_by_name=True)


class StoryChapterCompletionResponse(BaseModel):
    completed: bool
    updated_at: str = Field(alias="updatedAt")
    display_name: str = Field(alias="displayName")

    model_config = ConfigDict(populate_by_name=True)


class StoryChapterProgressResponse(BaseModel):
    version: int = 1
    updated_at: str = Field(alias="updatedAt")
    chapters: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class StoryCurrentStateResponse(BaseModel):
    current_state_path: str = Field(alias="currentStatePath")
    latest_snapshot_index_path: str = Field(alias="latestSnapshotIndexPath")
    data: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class StoryLatestSnapshotResponse(BaseModel):
    relative_path: str = Field(default="", alias="relativePath")
    snapshot: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class StorySyncCurrentStateResponse(BaseModel):
    written_paths: List[str] = Field(default_factory=list, alias="writtenPaths")
    latest_snapshot_path: str = Field(default="", alias="latestSnapshotPath")
    current_state: Dict[str, Any] = Field(default_factory=dict, alias="currentState")

    model_config = ConfigDict(populate_by_name=True)


def _build_trace(*, started: float, trace_id: str, tool_calls: int = 1) -> ApiTrace:
    return ApiTrace(
        traceId=trace_id,
        durationMs=int((perf_counter() - started) * 1000),
        toolCalls=tool_calls,
    )


def _workspace_root():
    return project_service.workspace_root


def _settings_response_payload() -> StoryProjectSettingsResponse:
    root = _workspace_root()
    settings = story_project_service.read_project_settings(root)
    return StoryProjectSettingsResponse(
        **settings,
        settingsPath=story_project_service.project_settings_path(root).relative_to(root).as_posix(),
        chapterProgressPath=story_project_service.chapter_progress_path(root).relative_to(root).as_posix(),
        snapshotRoot=(story_project_service.storydex_root(root) / "memory" / "chapters").relative_to(root).as_posix(),
        currentStateRoot=(story_project_service.storydex_root(root) / "memory" / "current-state").relative_to(root).as_posix(),
        memoryRoot=(story_project_service.storydex_root(root) / "memory").relative_to(root).as_posix(),
    )


@router.get("/story/settings", response_model=ApiEnvelope)
def read_story_settings() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = _settings_response_payload()
    audit = [{"action": "read_story_settings"}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.put("/story/settings", response_model=ApiEnvelope)
def update_story_settings(payload: StoryProjectSettingsUpdateRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    story_project_service.write_project_settings(_workspace_root(), payload.model_dump(by_alias=True, exclude_none=True))
    data = _settings_response_payload()
    audit = [
        {
            "action": "update_story_settings",
            "storySegmentFormat": data.story_segment_format,
            "maxSegmentsPerChapter": data.max_segments_per_chapter,
            "autoNameChapterTitle": data.auto_name_chapter_title,
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/story/templates/character", response_model=ApiEnvelope)
def read_story_character_template() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = StoryCharacterTemplateResponse(**story_project_service.read_character_template(_workspace_root()))
    audit = [{"action": "read_story_character_template"}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.put("/story/templates/character", response_model=ApiEnvelope)
def update_story_character_template(payload: StoryCharacterTemplateUpdateRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = StoryCharacterTemplateResponse(
        **story_project_service.write_character_template_from_markdown(_workspace_root(), payload.markdown)
    )
    audit = [{"action": "update_story_character_template"}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/story/templates/chapters", response_model=ApiEnvelope)
def read_story_chapter_templates() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    items = [
        StoryChapterTemplateResponse(**item)
        for item in story_project_service.list_chapter_templates(_workspace_root())
    ]
    audit = [{"action": "read_story_chapter_templates", "count": len(items)}]
    return success_response(
        data={"items": [item.model_dump(by_alias=True) for item in items]},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/story/chapters", response_model=ApiEnvelope)
def read_story_chapters() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    items = [StoryChapterStateResponse(**item.__dict__) for item in story_project_service.list_chapter_states(_workspace_root())]
    audit = [{"action": "read_story_chapters", "count": len(items)}]
    return success_response(
        data={"items": [item.model_dump(by_alias=True) for item in items]},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/story/chapter-progress", response_model=ApiEnvelope)
def read_story_chapter_progress() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = StoryChapterProgressResponse(**story_project_service.read_chapter_progress(_workspace_root()))
    audit = [{"action": "read_story_chapter_progress", "count": len(data.chapters)}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/story/chapter-completion", response_model=ApiEnvelope)
def update_story_chapter_completion(payload: StoryChapterCompletionRequest) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = StoryChapterCompletionResponse(
        **story_project_service.set_chapter_completed(
            _workspace_root(),
            payload.chapter_relative_path,
            payload.completed,
        )
    )
    audit = [
        {
            "action": "update_story_chapter_completion",
            "chapterRelativePath": payload.chapter_relative_path,
            "completed": data.completed,
        }
    ]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.get("/story/current-state", response_model=ApiEnvelope)
def read_story_current_state() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()
    data = StoryCurrentStateResponse(
        currentStatePath=story_project_service.current_state_master_path(root).relative_to(root).as_posix(),
        latestSnapshotIndexPath=story_project_service.latest_snapshot_index_path(root).relative_to(root).as_posix(),
        data=story_project_service.read_current_state(root),
    )
    audit = [{"action": "read_story_current_state"}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


def _read_optional_json(path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _compact_graph_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _relative_project_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _character_name_from_stem(stem: str) -> str:
    raw = _compact_graph_text(stem)
    name = re.sub(r"^\d+[_\-\s]+", "", raw).strip()
    return name or raw


def _character_name_from_markdown(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    break
                title = line.strip()
                if title.startswith("# "):
                    return _compact_graph_text(title.lstrip("#").strip())
    except Exception:
        pass
    return _character_name_from_stem(path.stem)


def _character_graph_node_from_file(root: Path, path: Path) -> Optional[Dict[str, Any]]:
    suffix = path.suffix.lower()
    card_id = ""
    name = ""
    if suffix == ".json":
        payload = _read_optional_json(path)
        card_id = _compact_graph_text(payload.get("id") or path.stem) if payload else _character_name_from_stem(path.stem)
        name = _compact_graph_text(payload.get("name") or card_id or path.stem) if payload else card_id
    elif suffix in {".md", ".txt"}:
        name = _character_name_from_markdown(path)
    else:
        return None

    node_id = name or card_id or _character_name_from_stem(path.stem)
    if not node_id:
        return None
    node: Dict[str, Any] = {
        "id": node_id,
        "label": name or node_id,
        "kind": "character",
        "source": _relative_project_path(root, path),
    }
    if card_id and card_id != node_id:
        node["characterId"] = card_id
    return node


def _iter_character_graph_paths(root: Path) -> List[Path]:
    storydex_root = story_project_service.storydex_root(root)
    characters_root = storydex_root / "characters"
    paths: List[Path] = []
    cards_dir = characters_root / "cards"
    if cards_dir.exists() and cards_dir.is_dir():
        paths.extend(sorted((path for path in cards_dir.glob("*.json") if path.is_file()), key=lambda path: path.name.lower()))

    if characters_root.exists() and characters_root.is_dir():
        direct_files = []
        for path in characters_root.iterdir():
            if not path.is_file():
                continue
            if path.name.lower() == "readme.md":
                continue
            if path.suffix.lower() in {".json", ".md", ".txt"}:
                direct_files.append(path)
        paths.extend(sorted(direct_files, key=lambda path: path.name.lower()))
    return paths


def _read_character_graph_nodes(root: Path) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for path in _iter_character_graph_paths(root):
        node = _character_graph_node_from_file(root, path)
        if not node:
            continue
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        nodes.append(node)
    return nodes


def _relationship_node_lookup(nodes: List[Dict[str, Any]]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = _compact_graph_text(node.get("id"))
        if not node_id:
            continue
        for value in (node_id, node.get("label"), node.get("name"), node.get("characterId")):
            key = _compact_graph_text(value)
            if key and key not in lookup:
                lookup[key] = node_id
    return lookup


def _is_character_graph_node_kind(value: Any) -> bool:
    key = _compact_graph_text(value).lower()
    return key in {"", "character", "role", "person", "\u89d2\u8272", "\u4eba\u7269"}


def _character_id_for_raw_relationship_node(raw_node: Dict[str, Any], node_lookup: Dict[str, str]) -> str:
    if not isinstance(raw_node, dict) or not _is_character_graph_node_kind(raw_node.get("kind")):
        return ""
    for value in (
        raw_node.get("id"),
        raw_node.get("label"),
        raw_node.get("name"),
        raw_node.get("characterId"),
        raw_node.get("character_id"),
    ):
        key = _compact_graph_text(value)
        if key in node_lookup:
            return node_lookup[key]
    return ""


def _canonical_relationship_endpoint(value: Any, node_lookup: Dict[str, str]) -> str:
    key = _compact_graph_text(value)
    return node_lookup.get(key, key)


def _relationship_dimension_from_text(text: str) -> str:
    normalized = _compact_graph_text(text).lower()
    dimension_tokens = (
        ("hostility", ("hostility", "enemy", "hostile", "\u654c\u5bf9", "\u4ec7", "\u6028")),
        ("rivalry", ("rivalry", "rival", "\u7ade\u4e89", "\u5bf9\u624b", "\u8f83\u91cf", "\u51b2\u7a81")),
        ("alliance", ("alliance", "ally", "partner", "\u540c\u76df", "\u5408\u4f5c", "\u7ed3\u76df", "\u8054\u624b", "\u4f19\u4f34")),
        ("trust", ("trust", "trusted", "\u4fe1\u4efb", "\u4fe1\u8d56", "\u4e0d\u4f1a\u8f7b\u6613\u5bb3\u4eba", "\u6258\u4ed8")),
        ("loyalty", ("loyalty", "loyal", "\u5fe0\u8bda", "\u6548\u5fe0", "\u8ffd\u968f")),
        ("intimacy", ("intimacy", "friend", "\u4eb2\u5bc6", "\u670b\u53cb", "\u53cb\u4eba", "\u6545\u4ea4", "\u4eb2\u8fd1")),
        ("professional", ("professional", "mentor", "student", "\u5e08\u5f92", "\u5e08\u7236", "\u5e08\u95e8", "\u638c\u67dc", "\u4e0a\u53f8", "\u4e0b\u5c5e", "\u540c\u4e8b")),
        ("family", ("family", "\u5bb6\u4eba", "\u4eb2\u5c5e", "\u7236\u4eb2", "\u6bcd\u4eb2", "\u5144", "\u5f1f", "\u59d0", "\u59b9", "\u59bb", "\u592b", "\u53d4", "\u59d1", "\u8205", "\u59e8")),
    )
    for dimension, tokens in dimension_tokens:
        if any(token in normalized for token in tokens):
            return dimension
    return "intimacy"


def _relationship_level_for_dimension(dimension: str) -> int:
    if dimension in {"hostility", "rivalry"}:
        return -2
    if dimension in {"trust", "intimacy", "loyalty", "alliance"}:
        return 2
    return 0


def _relationship_edge_key(edge: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        _compact_graph_text(edge.get("source")),
        _compact_graph_text(edge.get("target")),
        _compact_graph_text(edge.get("dimension")).lower() or "intimacy",
    )


def _clean_relationship_target(value: Any) -> str:
    target = _compact_graph_text(value).strip("*_ ")
    target = re.sub(r"^(?:\u4e0e|\u548c|\u5bf9)\s*", "", target)
    target = re.sub(r"(?:\u7684)?\u5173\u7cfb$", "", target)
    return target.strip()


def _build_derived_relationship_edge(
    root: Path,
    path: Path,
    *,
    source: str,
    target: str,
    relation: str,
    detail: str,
) -> Optional[Dict[str, Any]]:
    source = _compact_graph_text(source)
    target = _clean_relationship_target(target)
    if not source or not target or source == target:
        return None
    relation = _compact_graph_text(relation)
    detail = _compact_graph_text(detail)
    combined_detail = " - ".join(part for part in (relation, detail) if part)
    if not combined_detail:
        return None
    dimension = _relationship_dimension_from_text(combined_detail)
    source_path = _relative_project_path(root, path)
    evidence = detail or relation or source_path
    return {
        "source": source,
        "target": target,
        "dimension": dimension,
        "current_level": _relationship_level_for_dimension(dimension),
        "sourcePath": source_path,
        "derivedFrom": "character_asset",
        "history": [
            {
                "delta": "reveal",
                "magnitude": "minor",
                "detail": combined_detail,
                "segment_id": source_path,
                "evidence": evidence,
                "at": "",
            }
        ],
    }


def _relationship_from_mapping(
    root: Path,
    path: Path,
    *,
    source: str,
    payload: Dict[str, Any],
    node_lookup: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    target = _clean_relationship_target(
        payload.get("target")
        or payload.get("target_id")
        or payload.get("targetId")
        or payload.get("character")
        or payload.get("name")
    )
    if target in node_lookup:
        target = node_lookup[target]
    relation = _compact_graph_text(
        payload.get("relation_type")
        or payload.get("relationType")
        or payload.get("relation")
        or payload.get("type")
        or payload.get("status")
        or payload.get("summary")
    )
    detail = _compact_graph_text(payload.get("note") or payload.get("detail") or payload.get("description"))
    return _build_derived_relationship_edge(
        root,
        path,
        source=source,
        target=target,
        relation=relation,
        detail=detail,
    )


def _parse_markdown_relationship_line(
    root: Path,
    path: Path,
    *,
    source: str,
    line: str,
    node_lookup: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    text = _compact_graph_text(re.sub(r"^[-*+\u2022]\s*", "", str(line or "").strip()))
    text = re.sub(r"^\d+[.)]\s*", "", text)
    text = text.strip("*_ ")
    if not text or text in {"\u6682\u65e0", "\u65e0", "none", "n/a"}:
        return None
    match = re.match(r"^(.{1,40}?)[\uff1a:]\s*(.+)$", text)
    if not match:
        return None
    target = _clean_relationship_target(match.group(1))
    detail = _compact_graph_text(match.group(2))
    if target in node_lookup:
        target = node_lookup[target]
    return _build_derived_relationship_edge(
        root,
        path,
        source=source,
        target=target,
        relation="",
        detail=detail,
    )


def _relationships_from_markdown_card(
    root: Path,
    path: Path,
    *,
    source: str,
    node_lookup: Dict[str, str],
) -> List[Dict[str, Any]]:
    try:
        text = read_bounded_text_preview(path, max_chars=12000)
    except Exception:
        return []
    if not text.strip():
        return []

    edges: List[Dict[str, Any]] = []
    in_relationship_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            title = heading.group(2).strip().lower()
            in_relationship_section = any(token in title for token in ("relationship", "relation", "\u5173\u7cfb"))
            continue
        if not in_relationship_section:
            continue
        edge = _parse_markdown_relationship_line(
            root,
            path,
            source=source,
            line=line,
            node_lookup=node_lookup,
        )
        if edge:
            edges.append(edge)
    return edges


def _read_character_relationship_edges(root: Path, *, node_lookup: Dict[str, str]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in _iter_character_graph_paths(root):
        node = _character_graph_node_from_file(root, path)
        if not node:
            continue
        source = _compact_graph_text(node.get("id"))
        if not source:
            continue

        path_edges: List[Dict[str, Any]] = []
        suffix = path.suffix.lower()
        if suffix == ".json":
            payload = _read_optional_json(path)
            relationships: List[Any] = []
            for key in ("stable_relationships", "stableRelationships", "relationships"):
                value = payload.get(key)
                if isinstance(value, list):
                    relationships.extend(value)
            for item in relationships:
                if isinstance(item, dict):
                    edge = _relationship_from_mapping(
                        root,
                        path,
                        source=source,
                        payload=item,
                        node_lookup=node_lookup,
                    )
                    if edge:
                        path_edges.append(edge)
        elif suffix in {".md", ".txt"}:
            path_edges.extend(
                _relationships_from_markdown_card(
                    root,
                    path,
                    source=source,
                    node_lookup=node_lookup,
                )
            )

        for edge in path_edges:
            key = _relationship_edge_key(edge)
            if not all(key) or key in seen:
                continue
            seen.add(key)
            edges.append(edge)
    return edges


def _read_relationship_graph_snapshot(root: Path, current_dir: Path) -> Dict[str, Any]:
    graph = dict(_read_optional_json(current_dir / "relationship_graph.json"))
    graph.setdefault("version", 1)

    raw_edges = graph.get("edges")
    if not isinstance(raw_edges, list):
        raw_edges = []

    nodes: List[Dict[str, Any]] = []
    seen: set[str] = set()
    character_nodes = _read_character_graph_nodes(root)
    node_lookup = _relationship_node_lookup(character_nodes)

    raw_nodes = graph.get("nodes")
    if isinstance(raw_nodes, list):
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                continue
            node_id = _character_id_for_raw_relationship_node(raw_node, node_lookup)
            if not node_id or node_id in seen:
                continue
            node = dict(raw_node)
            node["id"] = node_id
            node["label"] = _compact_graph_text(node.get("label") or node_id)
            node["kind"] = "character"
            seen.add(node_id)
            nodes.append(node)

    for node in character_nodes:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        nodes.append(node)

    node_lookup = _relationship_node_lookup(nodes)
    character_ids = {str(node.get("id") or "") for node in nodes if str(node.get("id") or "")}
    edges: List[Dict[str, Any]] = []
    existing_edge_keys: set[tuple[str, str, str]] = set()

    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        source = _canonical_relationship_endpoint(raw_edge.get("source"), node_lookup)
        target = _canonical_relationship_endpoint(raw_edge.get("target"), node_lookup)
        if not source or not target or source == target:
            continue
        if source not in character_ids or target not in character_ids:
            continue
        edge = dict(raw_edge)
        edge["source"] = source
        edge["target"] = target
        key = _relationship_edge_key(edge)
        if not all(key) or key in existing_edge_keys:
            continue
        existing_edge_keys.add(key)
        edges.append(edge)

    for edge in _read_character_relationship_edges(root, node_lookup=node_lookup):
        source = _canonical_relationship_endpoint(edge.get("source"), node_lookup)
        target = _canonical_relationship_endpoint(edge.get("target"), node_lookup)
        if source not in character_ids or target not in character_ids:
            continue
        edge["source"] = source
        edge["target"] = target
        key = _relationship_edge_key(edge)
        if key in existing_edge_keys:
            continue
        existing_edge_keys.add(key)
        edges.append(edge)

    graph["nodes"] = nodes
    graph["edges"] = edges
    return graph


@router.get("/story/evolution-snapshot", response_model=ApiEnvelope)
def read_story_evolution_snapshot() -> ApiEnvelope:
    """B 项可视化：把演进系统的 6 份 JSON 一次性返回给前端，供 StoryStatePanel 渲染。"""
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()
    current_dir = story_project_service.storydex_root(root) / "memory" / "current"
    data = {
        "currentDir": current_dir.relative_to(root).as_posix() if current_dir.exists() else "",
        "changeLedger": _read_optional_json(current_dir / "change_ledger.json"),
        "relationshipGraph": _read_relationship_graph_snapshot(root, current_dir),
        "foreshadowLedger": _read_optional_json(current_dir / "foreshadow_ledger.json"),
        "chapterOutline": _read_optional_json(current_dir / "chapter_outline.json"),
        "characterConflicts": _read_optional_json(current_dir / "character_conflicts.json"),
        "timeline": _read_optional_json(current_dir / "timeline.json"),
    }
    audit = [{"action": "read_story_evolution_snapshot"}]
    return success_response(
        data=data,
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/story/character-conflicts/resolve", response_model=ApiEnvelope)
def resolve_character_conflict(payload: Dict[str, Any]) -> ApiEnvelope:
    """B 项可视化：用户从 UI 一键裁定单条冲突：accept_incoming / keep_existing / dismiss。"""
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()
    current_dir = story_project_service.storydex_root(root) / "memory" / "current"
    conflict_path = current_dir / "character_conflicts.json"
    cards_dir = story_project_service.storydex_root(root) / "characters" / "cards"

    target_index = payload.get("entryIndex")
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"accept_incoming", "keep_existing", "dismiss"}:
        return success_response(
            data={"ok": False, "reason": "invalid_decision"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "resolve_character_conflict", "ok": False}],
        )

    ledger = _read_optional_json(conflict_path)
    entries = ledger.get("entries", [])
    if not isinstance(entries, list) or not isinstance(target_index, int) or target_index < 0 or target_index >= len(entries):
        return success_response(
            data={"ok": False, "reason": "entry_not_found"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "resolve_character_conflict", "ok": False}],
        )

    entry = entries[target_index]
    if not isinstance(entry, dict):
        return success_response(
            data={"ok": False, "reason": "entry_invalid"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "resolve_character_conflict", "ok": False}],
        )

    applied = False
    if decision == "accept_incoming":
        cid = str(entry.get("character_id") or "").strip()
        field = str(entry.get("field") or "").strip()
        incoming = entry.get("incoming")
        card_path = cards_dir / f"{cid}.json"
        if cid and field in {"background", "motivation"} and incoming and card_path.exists():
            try:
                card = json.loads(card_path.read_text(encoding="utf-8"))
                if isinstance(card, dict):
                    card[field] = str(incoming).strip()
                    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    applied = True
            except Exception:
                applied = False

    entry["resolution"] = decision
    entry["resolved"] = True
    if applied:
        entry["applied"] = True
    entries[target_index] = entry
    ledger["entries"] = entries
    try:
        conflict_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return success_response(
            data={"ok": False, "reason": "write_failed"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "resolve_character_conflict", "ok": False}],
        )

    return success_response(
        data={"ok": True, "decision": decision, "applied": applied},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "resolve_character_conflict", "ok": True, "decision": decision}],
    )


def _segment_sort_key(rel_path: str) -> tuple:
    """对 chapters/X/segNN.md 这种路径做 chapter+segment 自然序排序。"""
    parts = [p for p in str(rel_path or "").split("/") if p]
    if len(parts) < 2:
        return (rel_path,)
    chapter = parts[1] if parts[0] == "chapters" else parts[0]
    seg_name = parts[-1]
    import re as _re
    nums = _re.findall(r"\d+", seg_name)
    seg_index = int(nums[-1]) if nums else 0
    chap_nums = _re.findall(r"\d+", chapter)
    chap_index = int(chap_nums[0]) if chap_nums else 0
    return (chap_index, chapter, seg_index, seg_name)


def _parse_segment_numeric(value: str) -> int:
    """从 '003' / '第3章' / 'seg-001' 等抽出最后一段数字用于 ledger 截断比较。"""
    import re as _re
    digits = _re.findall(r"\d+", str(value or ""))
    if not digits:
        return -1
    try:
        return int(digits[-1])
    except ValueError:
        return -1


@router.post("/story/rollback", response_model=ApiEnvelope)
def rollback_to_segment(payload: Dict[str, Any]) -> ApiEnvelope:
    """D 项 rollback：把 chapters/ 与演进 ledger 回退到指定 segment 之前。
    保留可 undo 的 safety backup 到 .storydex/rollback_backups/<id>/。"""
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()

    target_segment = str(payload.get("target_segment_relative_path") or "").strip().replace("\\", "/")
    keep_target = bool(payload.get("keep_target", True))
    if not target_segment:
        return success_response(
            data={"ok": False, "reason": "missing_target"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "rollback", "ok": False}],
        )

    target_path = root / target_segment
    if not target_path.exists() or not target_path.is_file():
        return success_response(
            data={"ok": False, "reason": "target_not_found", "target": target_segment},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "rollback", "ok": False}],
        )

    target_key = _segment_sort_key(target_segment)
    target_seg_idx = _parse_segment_numeric(target_path.stem)

    chapters_dir = root / "chapters"
    later_segments: List[Path] = []
    if chapters_dir.exists():
        for path in chapters_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
                continue
            rel = path.relative_to(root).as_posix()
            key = _segment_sort_key(rel)
            if keep_target:
                if key > target_key:
                    later_segments.append(path)
            else:
                if key >= target_key:
                    later_segments.append(path)

    rollback_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8]
    backup_root = story_project_service.agent_root(root) / "rollback_backups" / rollback_id
    backup_root.mkdir(parents=True, exist_ok=True)

    backed_up_segments: List[str] = []
    for path in later_segments:
        rel = path.relative_to(root).as_posix()
        dst = backup_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        backed_up_segments.append(rel)

    storydex_root = story_project_service.storydex_root(root)
    chapters_variables_root = storydex_root / "memory" / "chapters"
    backed_up_variables: List[str] = []
    if chapters_variables_root.exists():
        for var_path in chapters_variables_root.rglob("*.variables.json"):
            var_seg_idx = _parse_segment_numeric(var_path.stem.replace(".variables", ""))
            if var_seg_idx < 0:
                continue
            if (keep_target and var_seg_idx > target_seg_idx) or (not keep_target and var_seg_idx >= target_seg_idx):
                rel = var_path.relative_to(root).as_posix()
                dst = backup_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(var_path, dst)
                backed_up_variables.append(rel)

    current_dir = storydex_root / "memory" / "current"
    ledger_files = [
        "change_ledger.json", "timeline.json", "character_conflicts.json",
        "chapter_outline.json", "relationship_graph.json", "foreshadow_ledger.json",
    ]
    for fname in ledger_files:
        fpath = current_dir / fname
        if fpath.exists():
            dst = backup_root / fpath.relative_to(root).as_posix()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fpath, dst)

    deleted_segments: List[str] = []
    for path in later_segments:
        try:
            path.unlink()
            deleted_segments.append(path.relative_to(root).as_posix())
        except Exception:
            pass

    deleted_variables: List[str] = []
    if chapters_variables_root.exists():
        for var_path in chapters_variables_root.rglob("*.variables.json"):
            var_seg_idx = _parse_segment_numeric(var_path.stem.replace(".variables", ""))
            if var_seg_idx < 0:
                continue
            if (keep_target and var_seg_idx > target_seg_idx) or (not keep_target and var_seg_idx >= target_seg_idx):
                try:
                    var_path.unlink()
                    deleted_variables.append(var_path.relative_to(root).as_posix())
                except Exception:
                    pass

    _truncate_ledger_by_segment(current_dir / "change_ledger.json", "entries", target_seg_idx, keep_target)
    _truncate_ledger_by_segment(current_dir / "timeline.json", "entries", target_seg_idx, keep_target)
    _truncate_ledger_by_segment(current_dir / "character_conflicts.json", "entries", target_seg_idx, keep_target)
    _truncate_relationship_history(current_dir / "relationship_graph.json", target_seg_idx, keep_target)
    _truncate_foreshadow_threads(current_dir / "foreshadow_ledger.json", target_seg_idx, keep_target)
    _truncate_chapter_outline(current_dir / "chapter_outline.json", target_seg_idx, keep_target)

    manifest = {
        "rollbackId": rollback_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "targetSegment": target_segment,
        "keepTarget": keep_target,
        "backedUpSegments": backed_up_segments,
        "backedUpVariables": backed_up_variables,
        "deletedSegments": deleted_segments,
        "deletedVariables": deleted_variables,
        "ledgerSnapshots": [str((backup_root / f"{ledger}").relative_to(root).as_posix()) for ledger in [
            f".storydex/memory/current/{fname}" for fname in ledger_files
        ] if (backup_root / ledger).exists()],
    }
    (backup_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return success_response(
        data={
            "ok": True,
            "rollbackId": rollback_id,
            "deletedSegmentCount": len(deleted_segments),
            "deletedVariableCount": len(deleted_variables),
            "backupPath": backup_root.relative_to(root).as_posix(),
        },
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "rollback", "ok": True, "rollbackId": rollback_id, "deletedSegmentCount": len(deleted_segments)}],
    )


@router.post("/story/rollback/undo", response_model=ApiEnvelope)
def undo_rollback(payload: Dict[str, Any]) -> ApiEnvelope:
    """从 .storydex/rollback_backups/<rollback_id>/ 恢复被回档的内容。"""
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()
    rollback_id = str(payload.get("rollbackId") or "").strip()
    if not rollback_id:
        return success_response(
            data={"ok": False, "reason": "missing_rollback_id"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "rollback_undo", "ok": False}],
        )

    backup_root = story_project_service.agent_root(root) / "rollback_backups" / rollback_id
    manifest_path = backup_root / "manifest.json"
    if not manifest_path.exists():
        return success_response(
            data={"ok": False, "reason": "backup_not_found", "rollbackId": rollback_id},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "rollback_undo", "ok": False}],
        )

    restored: List[str] = []
    for src in backup_root.rglob("*"):
        if not src.is_file() or src.name == "manifest.json":
            continue
        rel = src.relative_to(backup_root).as_posix()
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(rel)

    return success_response(
        data={"ok": True, "rollbackId": rollback_id, "restoredCount": len(restored)},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "rollback_undo", "ok": True, "rollbackId": rollback_id}],
    )


def _truncate_ledger_by_segment(path: Path, list_key: str, target_idx: int, keep_target: bool) -> None:
    if target_idx < 0 or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        entries = data.get(list_key)
        if not isinstance(entries, list):
            return
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            seg_idx = _parse_segment_numeric(str(entry.get("segment_id") or ""))
            if seg_idx < 0:
                kept.append(entry)
                continue
            if keep_target and seg_idx <= target_idx:
                kept.append(entry)
            elif not keep_target and seg_idx < target_idx:
                kept.append(entry)
        data[list_key] = kept
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _truncate_relationship_history(path: Path, target_idx: int, keep_target: bool) -> None:
    if target_idx < 0 or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        edges = data.get("edges")
        if not isinstance(edges, list):
            return
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            history = edge.get("history")
            if not isinstance(history, list):
                continue
            kept_history = []
            for h in history:
                if not isinstance(h, dict):
                    kept_history.append(h)
                    continue
                seg_idx = _parse_segment_numeric(str(h.get("segment_id") or ""))
                if seg_idx < 0 or (keep_target and seg_idx <= target_idx) or (not keep_target and seg_idx < target_idx):
                    kept_history.append(h)
            edge["history"] = kept_history
            level = 0
            for h in kept_history:
                if not isinstance(h, dict):
                    continue
                step = {"minor": 1, "moderate": 2, "major": 3}.get(str(h.get("magnitude") or ""), 1)
                delta = str(h.get("delta") or "")
                if delta in {"increase", "forge"}:
                    level += step
                elif delta in {"decrease", "break"}:
                    level -= step
            edge["current_level"] = max(-10, min(10, level))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _truncate_foreshadow_threads(path: Path, target_idx: int, keep_target: bool) -> None:
    if target_idx < 0 or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        threads = data.get("threads")
        if not isinstance(threads, dict):
            return
        for thread_id, thread in list(threads.items()):
            if not isinstance(thread, dict):
                continue
            planted = thread.get("planted_at") or {}
            planted_idx = _parse_segment_numeric(str(planted.get("segment_id") or "")) if isinstance(planted, dict) else -1
            if planted_idx >= 0 and ((keep_target and planted_idx > target_idx) or (not keep_target and planted_idx >= target_idx)):
                threads.pop(thread_id, None)
                continue
            callbacks = thread.get("callbacks")
            if isinstance(callbacks, list):
                kept_cb = []
                for cb in callbacks:
                    cb_idx = _parse_segment_numeric(str(cb.get("segment_id") or "")) if isinstance(cb, dict) else -1
                    if cb_idx < 0 or (keep_target and cb_idx <= target_idx) or (not keep_target and cb_idx < target_idx):
                        kept_cb.append(cb)
                thread["callbacks"] = kept_cb
            resolved = thread.get("resolved_at")
            if isinstance(resolved, dict):
                r_idx = _parse_segment_numeric(str(resolved.get("segment_id") or ""))
                if r_idx >= 0 and ((keep_target and r_idx > target_idx) or (not keep_target and r_idx >= target_idx)):
                    thread["resolved_at"] = None
                    thread["status"] = "recalled" if thread.get("callbacks") else "open"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _truncate_chapter_outline(path: Path, target_idx: int, keep_target: bool) -> None:
    if target_idx < 0 or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        chapters = data.get("chapters")
        if not isinstance(chapters, dict):
            return
        for chap_id, chap in chapters.items():
            if not isinstance(chap, dict):
                continue
            milestones = chap.get("milestones")
            if not isinstance(milestones, list):
                continue
            kept = []
            for m in milestones:
                if not isinstance(m, dict):
                    kept.append(m)
                    continue
                seg_idx = _parse_segment_numeric(str(m.get("segment_id") or ""))
                if seg_idx < 0 or (keep_target and seg_idx <= target_idx) or (not keep_target and seg_idx < target_idx):
                    kept.append(m)
            chap["milestones"] = kept
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


@router.post("/story/autopilot/start", response_model=ApiEnvelope)
def autopilot_start(payload: Dict[str, Any]) -> ApiEnvelope:
    """C 项 autopilot MVP：把"连续推 N 段" 拆成一份顺序计划，前端轮询 next 触发实际 chat。"""
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()

    prompt_template = str(payload.get("promptTemplate") or "").strip()
    max_segments = int(payload.get("maxSegments") or 0)
    active_file = str(payload.get("activeFile") or "").strip()
    if not prompt_template or max_segments <= 0:
        return success_response(
            data={"ok": False, "reason": "invalid_payload"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "autopilot_start", "ok": False}],
        )
    max_segments = min(max_segments, 20)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8]
    run_dir = story_project_service.agent_root(root) / "autopilot" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "runId": run_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "promptTemplate": prompt_template,
        "maxSegments": max_segments,
        "activeFile": active_file,
        "currentIndex": 0,
        "status": "queued",
        "history": [],
    }
    (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return success_response(
        data={"ok": True, "runId": run_id, "maxSegments": max_segments},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "autopilot_start", "ok": True, "runId": run_id}],
    )


@router.get("/story/autopilot/{run_id}/status", response_model=ApiEnvelope)
def autopilot_status(run_id: str) -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()
    state_path = story_project_service.agent_root(root) / "autopilot" / run_id / "state.json"
    if not state_path.exists():
        return success_response(
            data={"ok": False, "reason": "run_not_found"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "autopilot_status", "ok": False}],
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return success_response(
            data={"ok": False, "reason": "state_corrupted"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "autopilot_status", "ok": False}],
        )
    return success_response(
        data={"ok": True, "state": state},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "autopilot_status", "ok": True}],
    )


@router.post("/story/autopilot/{run_id}/advance", response_model=ApiEnvelope)
def autopilot_advance(run_id: str, payload: Dict[str, Any]) -> ApiEnvelope:
    """前端在每段被人工 commit 后调一次，让 run state 推进到下一段。"""
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()
    state_path = story_project_service.agent_root(root) / "autopilot" / run_id / "state.json"
    if not state_path.exists():
        return success_response(
            data={"ok": False, "reason": "run_not_found"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "autopilot_advance", "ok": False}],
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("invalid state")
    except Exception:
        return success_response(
            data={"ok": False, "reason": "state_corrupted"},
            trace=_build_trace(started=started, trace_id=trace_id),
            audit=[{"action": "autopilot_advance", "ok": False}],
        )

    outcome = str(payload.get("outcome") or "ok").strip().lower()
    note = str(payload.get("note") or "").strip()
    history = list(state.get("history", []))
    history.append({
        "index": int(state.get("currentIndex", 0)),
        "outcome": outcome,
        "note": note,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    state["history"] = history
    state["currentIndex"] = int(state.get("currentIndex", 0)) + 1

    if outcome == "abort":
        state["status"] = "aborted"
    elif state["currentIndex"] >= int(state.get("maxSegments", 0)):
        state["status"] = "done"
    else:
        state["status"] = "running"
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()

    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return success_response(
        data={"ok": True, "state": state},
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=[{"action": "autopilot_advance", "ok": True, "newIndex": state["currentIndex"]}],
    )


@router.get("/story/snapshots/latest", response_model=ApiEnvelope)
def read_story_latest_snapshot() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    data = StoryLatestSnapshotResponse(**story_project_service.find_latest_snapshot(_workspace_root()))
    audit = [{"action": "read_story_latest_snapshot", "relativePath": data.relative_path}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )


@router.post("/story/current-state/sync", response_model=ApiEnvelope)
def sync_story_current_state() -> ApiEnvelope:
    started = perf_counter()
    trace_id = str(uuid4())
    root = _workspace_root()
    latest_snapshot = story_project_service.find_latest_snapshot(root)
    written_paths = story_project_service.sync_current_state_from_latest_snapshot(root)
    data = StorySyncCurrentStateResponse(
        writtenPaths=written_paths,
        latestSnapshotPath=str(latest_snapshot.get("relativePath") or ""),
        currentState=story_project_service.read_current_state(root),
    )
    audit = [{"action": "sync_story_current_state", "writtenCount": len(written_paths)}]
    return success_response(
        data=data.model_dump(by_alias=True),
        trace=_build_trace(started=started, trace_id=trace_id),
        audit=audit,
    )

