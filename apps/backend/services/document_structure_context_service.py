from __future__ import annotations

import concurrent.futures
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Sequence

from core.bounded_text_io import read_text_limited
from services.content_catalog_service import ContentCatalogSnapshot
from services.source_contract import source_revision_id


_SCAN_CHAR_LIMIT = 400_000
_MAX_CANDIDATES = 256
_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
_JSON_KEY_RE = re.compile(r'^\s*"(?P<key>[^"\\]+)"\s*:', re.MULTILINE)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}")
_ACTION_TERMS = frozenset(
    {
        "请修改",
        "修改",
        "更新",
        "调整",
        "读取",
        "查看",
        "检查",
        "分析",
        "文件",
        "内容",
        "字段",
        "目标",
        "保留",
        "其他",
        "不要",
        "创建",
        "并行",
    }
)
_FIELD_ALIASES: Dict[str, tuple[str, ...]] = {
    "核心动机": ("motivation", "core_motivation", "核心动机", "动机"),
    "动机": ("motivation", "core_motivation", "动机"),
    "背景": ("background", "背景", "经历"),
    "性格": ("personality", "性格"),
    "身份": ("role", "identity", "身份", "角色"),
    "关系": ("relationships", "relations", "关系", "关联"),
    "行为边界": ("boundaries", "taboo", "行为边界", "边界", "禁忌"),
    "边界": ("boundaries", "taboo", "边界", "禁忌"),
    "规则": ("rules", "constraints", "规则", "约束"),
    "地理": ("geography", "location", "地理", "地点"),
    "历史": ("history", "历史", "沿革"),
}
_FIELD_QUERY_TERMS = frozenset(
    str(alias).casefold()
    for key, aliases in _FIELD_ALIASES.items()
    for alias in (key, *aliases)
)
_WRITE_DISCIPLINE = (
    "Read-before-write: this map/excerpt is incomplete. Before editing, call read_file for the "
    "target path and relevant span at this revision; continue while hasMore=true."
)
_SHORT_WRITE_DISCIPLINE = (
    "Incomplete excerpt: call read_file before writing and continue while hasMore=true."
)


@dataclass(frozen=True)
class _Section:
    title: str
    level: int
    start_line: int
    end_line: int
    content: str


@dataclass(frozen=True)
class _Document:
    path: Path
    relative_path: str
    text: str
    total_chars: int
    total_lines: int
    truncated_scan: bool
    headings: tuple[_Section, ...]
    json_payload: Dict[str, Any] | None
    identity_text: str


@lru_cache(maxsize=512)
def _cached_scan(
    path_value: str, mtime_ns: int, size_bytes: int
) -> tuple[str, int, bool]:
    del mtime_ns, size_bytes
    read = read_text_limited(Path(path_value), _SCAN_CHAR_LIMIT)
    return read.text, read.total_chars, read.truncated


@lru_cache(maxsize=512)
def _parse_document(
    path_value: str,
    relative_path: str,
    mtime_ns: int,
    size_bytes: int,
) -> _Document | None:
    """Parse one immutable revision once, not once per turn.

    The catalog/context pipeline already invalidates revisions through mtime and
    size.  Keeping the parsed headings/JSON payload in the same bounded cache
    removes the repeated split/parse work that otherwise dominates warm Windows
    turns with many character cards.
    """

    path = Path(path_value)
    try:
        text, total_chars, truncated = _cached_scan(
            path_value,
            mtime_ns,
            size_bytes,
        )
    except OSError:
        return None
    lines = text.splitlines()
    headings: list[_Section] = []
    heading_rows: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line)
        if match is not None:
            heading_rows.append(
                (index, len(match.group("marks")), match.group("title").strip())
            )
    for index, (start_line, level, title) in enumerate(heading_rows):
        end_line = (
            heading_rows[index + 1][0] - 1
            if index + 1 < len(heading_rows)
            else max(start_line, len(lines))
        )
        content = "\n".join(lines[start_line - 1 : end_line]).strip()
        headings.append(_Section(title, level, start_line, end_line, content))

    json_payload: Dict[str, Any] | None = None
    if path.suffix.lower() == ".json" and not truncated:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                json_payload = parsed
        except (TypeError, ValueError):
            json_payload = None

    identity_parts = [path.stem]
    if headings:
        identity_parts.extend(section.title for section in headings[:12])
    if json_payload is not None:
        identity_parts.append(str(json_payload.get("name") or ""))
        aliases = json_payload.get("aliases")
        if isinstance(aliases, list):
            identity_parts.extend(str(item) for item in aliases[:12])
    return _Document(
        path=path,
        relative_path=relative_path,
        text=text,
        total_chars=total_chars,
        total_lines=max(len(lines), 1),
        truncated_scan=truncated,
        headings=tuple(headings),
        json_payload=json_payload,
        identity_text=" ".join(identity_parts),
    )


def _read_document(path: Path, relative_path: str) -> _Document | None:
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return None
    return _parse_document(
        str(resolved),
        str(relative_path),
        stat.st_mtime_ns,
        stat.st_size,
    )


def _query_terms(prompt: str, active_entities: Sequence[str]) -> list[str]:
    terms: list[str] = []
    for value in [*active_entities, *_TOKEN_RE.findall(str(prompt or ""))]:
        normalized = str(value or "").strip()
        if not normalized or normalized in _ACTION_TERMS:
            continue
        if normalized not in terms:
            terms.append(normalized)
        for key, aliases in _FIELD_ALIASES.items():
            if key in normalized or normalized in aliases:
                for alias in aliases:
                    if alias not in terms:
                        terms.append(alias)
    return terms[:24]


def _candidate_paths(
    root: Path,
    *,
    kind: str,
    catalog_snapshot: ContentCatalogSnapshot | None,
) -> list[tuple[str, Path]]:
    prefix = ".storydex/characters" if kind == "character" else ".storydex/worldbook"
    suffixes = {".md", ".txt", ".json"}
    candidates: list[tuple[str, Path]] = []
    if catalog_snapshot is not None:
        for entry in catalog_snapshot.files(prefix=prefix, suffixes=tuple(suffixes)):
            relative = entry.path.replace("\\", "/")
            if Path(relative).name.lower() == "readme.md":
                continue
            candidates.append((relative, root / relative))
    else:
        directory = root / prefix
        if directory.is_dir():
            for path in sorted(
                directory.rglob("*"), key=lambda item: item.as_posix().lower()
            ):
                if (
                    path.is_file()
                    and path.suffix.lower() in suffixes
                    and path.name.lower() != "readme.md"
                ):
                    candidates.append((path.relative_to(root).as_posix(), path))
    return candidates[:_MAX_CANDIDATES]


def _document_score(
    document: _Document,
    *,
    terms: Sequence[str],
    active_file: str,
) -> int:
    score = 1000 if document.relative_path == active_file else 0
    identity = document.identity_text.casefold()
    body = document.text.casefold()
    for term in terms:
        needle = term.casefold()
        if not needle:
            continue
        if needle in identity:
            score += 40
        elif needle in body:
            score += 4
    return score


def _selected_headings(
    document: _Document, terms: Sequence[str], limit: int = 36
) -> list[_Section]:
    headings = list(document.headings)
    if len(headings) <= limit:
        return headings
    matched = sorted(
        (
            (_section_score(section, terms), section)
            for section in headings
            if _section_score(section, terms) > 0
        ),
        key=lambda item: (-item[0], item[1].start_line),
    )
    selected = [
        *headings[:10],
        *(section for _, section in matched[:16]),
        *headings[-10:],
    ]
    unique = {section.start_line: section for section in selected}
    return [unique[line] for line in sorted(unique)][:limit]


def _section_score(section: _Section, terms: Sequence[str]) -> int:
    title = section.title.casefold()
    content = section.content.casefold()
    score = 0
    for term in terms:
        needle = term.casefold()
        if not needle:
            continue
        if needle == title:
            score += 120 if needle in _FIELD_QUERY_TERMS else 80
        elif needle in title:
            score += 60 if needle in _FIELD_QUERY_TERMS else 40
        elif needle in content:
            score += 4
    return score


def _bounded_span(content: str, terms: Sequence[str], max_chars: int = 900) -> str:
    text = str(content or "").strip()
    if len(text) <= max_chars:
        return text
    folded = text.casefold()
    matches = []
    for term in terms:
        needle = str(term or "").casefold()
        position = folded.find(needle) if needle else -1
        if position < 0:
            continue
        matches.append(
            (
                1 if needle in _FIELD_QUERY_TERMS else 0,
                len(needle),
                -position,
                position,
            )
        )
    center = max(matches)[3] if matches else 0
    start = max(0, center - max_chars // 3)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "...\n" if start else ""
    suffix = "\n..." if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def _json_key_lines(text: str) -> Dict[str, int]:
    return {
        str(match.group("key")): text.count("\n", 0, match.start()) + 1
        for match in _JSON_KEY_RE.finditer(text)
    }


def _clip_text(value: str, max_chars: int, *, marker: str = "...") -> str:
    text = str(value or "").strip()
    safe_limit = max(0, int(max_chars or 0))
    if len(text) <= safe_limit:
        return text
    if safe_limit <= len(marker):
        return text[:safe_limit]
    return text[: safe_limit - len(marker)].rstrip() + marker


def _render_structure_map(
    title: str,
    rows: Sequence[tuple[int, int, str]],
    *,
    max_chars: int,
) -> str:
    safe_limit = max(0, int(max_chars or 0))
    if safe_limit <= 0:
        return ""
    heading = _clip_text(title, safe_limit)
    if len(heading) >= safe_limit or not rows:
        return heading

    available = safe_limit - len(heading) - 1
    selected: list[tuple[int, str]] = []
    for index, _priority, row in sorted(rows, key=lambda item: (-item[1], item[0])):
        needed = len(row) + (1 if selected else 0)
        if needed > available:
            if not selected and available > 0:
                clipped = _clip_text(row, available)
                if clipped:
                    selected.append((index, clipped))
                    available -= len(clipped)
            continue
        selected.append((index, row))
        available -= needed
    selected.sort(key=lambda item: item[0])

    rendered_rows = [row for _, row in selected]
    omitted = max(0, len(rows) - len(rendered_rows))
    if omitted:
        marker = (
            f"- ... {omitted} map entries omitted; use read_file for the full structure"
        )
        needed = len(marker) + (1 if rendered_rows else 0)
        if needed <= available:
            rendered_rows.append(marker)
    return "\n".join([heading, *rendered_rows]).strip()


def _render_matched_evidence(
    entries: Sequence[tuple[str, str]],
    *,
    terms: Sequence[str],
    max_chars: int,
) -> tuple[str, int]:
    safe_limit = max(0, int(max_chars or 0))
    if safe_limit < 80 or not entries:
        return "", 0
    rendered: list[str] = []
    remaining = safe_limit
    for index, (label, content) in enumerate(entries[:3]):
        separator_chars = 2 if rendered else 0
        label_text = str(label or "").strip()
        minimum_needed = separator_chars + len(label_text) + 2 + 24
        if minimum_needed > remaining:
            continue
        slots_left = max(1, min(3, len(entries)) - index)
        content_budget = max(
            24,
            min(520, (remaining - separator_chars - len(label_text) - 1) // slots_left),
        )
        span = _bounded_span(content, terms, max_chars=content_budget)
        section = f"{label_text}\n{span}".strip()
        if len(section) + separator_chars > remaining:
            section = _clip_text(section, remaining - separator_chars)
        if not section:
            continue
        rendered.append(section)
        remaining -= len(section) + separator_chars
    return "\n\n".join(rendered).strip(), len(rendered)


def _render_document(
    document: _Document,
    *,
    revision: str,
    terms: Sequence[str],
    max_chars: int,
) -> tuple[str, int]:
    prefix = "\n".join(
        [
            f"### {document.relative_path}",
            (
                f"revision={revision}; totalLines={document.total_lines}; totalChars={document.total_chars}; "
                f"scanComplete={str(not document.truncated_scan).lower()}"
            ),
        ]
    )
    map_title = ""
    map_rows: list[tuple[int, int, str]] = []
    evidence_entries: list[tuple[str, str]] = []
    if document.json_payload is not None:
        key_lines = _json_key_lines(document.text)
        keys = list(document.json_payload)
        map_title = "Structure map (JSON top-level keys):"
        scored_keys = []
        for index, key in enumerate(keys):
            value_text = json.dumps(document.json_payload.get(key), ensure_ascii=False)
            score = sum(
                (
                    80
                    if term.casefold() == key.casefold()
                    and term.casefold() in _FIELD_QUERY_TERMS
                    else 40
                    if term.casefold() in key.casefold()
                    else 4
                    if term.casefold() in value_text.casefold()
                    else 0
                )
                for term in terms
                if term
            )
            if key in {"name", "aliases"}:
                score += 5
            scored_keys.append((score, key, value_text))
            map_rows.append(
                (
                    index,
                    score + (10 if index in {0, len(keys) - 1} else 0),
                    f"- L{key_lines.get(key, 1)} key={key}",
                )
            )
        for score, key, value_text in sorted(
            scored_keys, key=lambda item: (-item[0], item[1])
        )[:3]:
            if score <= 0:
                continue
            line = key_lines.get(key, 1)
            evidence_entries.append(
                (
                    f"Matched evidence span: L{line} key={key}",
                    f"{key}: {value_text}",
                )
            )
    elif document.headings:
        map_title = "Structure map (Markdown headings):"
        selected_headings = _selected_headings(document, terms)
        for index, section in enumerate(selected_headings):
            score = _section_score(section, terms)
            map_rows.append(
                (
                    index,
                    score + (10 if index in {0, len(selected_headings) - 1} else 0),
                    f"- L{section.start_line}-{section.end_line} H{section.level} {section.title}",
                )
            )
        scored = sorted(
            (
                (_section_score(section, terms), section)
                for section in document.headings
            ),
            key=lambda item: (-item[0], item[1].start_line),
        )
        for score, section in scored[:3]:
            if score <= 0:
                continue
            evidence_entries.append(
                (
                    f"Matched evidence span: L{section.start_line}-{section.end_line} heading={section.title}",
                    section.content,
                )
            )
    else:
        map_title = (
            "Structure map: plain-text document (no Markdown headings detected)."
        )
        folded = document.text.casefold()
        matches = []
        for term in terms:
            needle = str(term or "").casefold()
            position = folded.find(needle) if needle else -1
            if position >= 0:
                matches.append(
                    (
                        1 if needle in _FIELD_QUERY_TERMS else 0,
                        len(needle),
                        -position,
                        position,
                    )
                )
        if matches:
            position = max(matches)[3]
            start_line = document.text.count("\n", 0, position) + 1
            evidence_entries.append(
                (
                    f"Matched evidence span near L{start_line}",
                    document.text,
                )
            )

    safe_limit = max(1, int(max_chars or 1))
    separator = "\n\n"
    if len(prefix) >= safe_limit:
        return _clip_text(prefix, safe_limit), 0
    footer = _WRITE_DISCIPLINE
    minimum_map_chars = min(len(map_title), 80)
    if len(prefix) + len(footer) + minimum_map_chars + len(separator) * 2 > safe_limit:
        footer = _SHORT_WRITE_DISCIPLINE
    if len(prefix) + len(footer) + len(separator) > safe_limit:
        footer = _clip_text(footer, safe_limit - len(prefix) - len(separator))

    variable_with_evidence = max(
        0,
        safe_limit - len(prefix) - len(footer) - len(separator) * 3,
    )
    max_evidence_chars = max(0, variable_with_evidence - minimum_map_chars)
    evidence_budget = (
        min(620, max_evidence_chars, max(160, variable_with_evidence // 2))
        if evidence_entries and max_evidence_chars >= 80
        else 0
    )
    evidence_text, matched_count = _render_matched_evidence(
        evidence_entries,
        terms=terms,
        max_chars=evidence_budget,
    )
    separator_count = 3 if evidence_text else 2
    map_budget = max(
        0,
        safe_limit
        - len(prefix)
        - len(footer)
        - len(evidence_text)
        - len(separator) * separator_count,
    )
    map_text = _render_structure_map(
        map_title,
        map_rows,
        max_chars=map_budget,
    )
    rendered = separator.join(
        part for part in (prefix, map_text, evidence_text, footer) if part
    ).strip()
    return rendered, matched_count


class DocumentStructureContextService:
    def build_context(
        self,
        workspace_root: Path,
        *,
        kind: str,
        prompt: str,
        active_file: str,
        active_entities: Sequence[str],
        catalog_snapshot: ContentCatalogSnapshot | None,
        max_files: int,
        max_chars_per_file: int,
        total_chars: int,
        allow_unmatched_fallback: bool = True,
    ) -> tuple[str, list[str], Dict[str, Any]]:
        root = Path(workspace_root).resolve()
        if kind not in {"character", "worldbook"}:
            raise ValueError(f"unsupported structure context kind: {kind}")
        normalized_active = str(active_file or "").strip().replace("\\", "/")
        terms = _query_terms(prompt, active_entities)
        candidates = _candidate_paths(
            root,
            kind=kind,
            catalog_snapshot=catalog_snapshot,
        )
        if not candidates:
            return (
                "",
                [],
                {
                    "structureMapCount": 0,
                    "matchedSpanCount": 0,
                    "queryTerms": terms,
                    "requiresFullReadBeforeWrite": True,
                    "strategy": "structure_map_matched_spans_jit_read",
                    "relevanceMatched": False,
                    "unmatchedFallbackUsed": False,
                },
            )

        max_workers = min(8, len(candidates))
        if max_workers <= 1:
            inspected = [
                _read_document(path, relative) for relative, path in candidates
            ]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                inspected = list(
                    pool.map(
                        lambda item: _read_document(item[1], item[0]),
                        candidates,
                    )
                )
        documents = [item for item in inspected if item is not None]
        ranked = sorted(
            (
                (
                    _document_score(item, terms=terms, active_file=normalized_active),
                    item,
                )
                for item in documents
            ),
            key=lambda item: (-item[0], item[1].relative_path.casefold()),
        )
        relevance_matched = any(score > 0 for score, _ in ranked)
        if relevance_matched:
            ranked = [(score, item) for score, item in ranked if score > 0]
        elif not allow_unmatched_fallback:
            return (
                "",
                [],
                {
                    "structureMapCount": 0,
                    "matchedSpanCount": 0,
                    "queryTerms": terms,
                    "requiresFullReadBeforeWrite": True,
                    "strategy": "structure_map_matched_spans_jit_read",
                    "relevanceMatched": False,
                    "unmatchedFallbackUsed": False,
                },
            )
        selected = [item for _, item in ranked[: max(1, int(max_files or 1))]]

        header = "\n".join(
            [
                f"[Project {kind.title()} Structure Maps and Matched Evidence]",
                "These are compact location maps and exact matched spans, not complete hard constraints. Read the target revision on demand before relying on omitted content or making a write.",
            ]
        )
        rendered_parts: list[str] = [header]
        selected_paths: list[str] = []
        matched_span_count = 0
        safe_total = max(320, int(total_chars or 320))
        remaining = safe_total - len(header)
        for document in selected:
            separator_chars = 2
            if remaining - separator_chars < 320:
                break
            entry = (
                catalog_snapshot.get(document.relative_path)
                if catalog_snapshot is not None
                else None
            )
            if entry is not None:
                revision = entry.revision
            else:
                try:
                    revision = source_revision_id(document.path.read_bytes())
                except OSError:
                    revision = "unavailable"
            per_file_limit = min(
                max(320, int(max_chars_per_file or 320)),
                remaining - separator_chars,
            )
            rendered, matched_count = _render_document(
                document,
                revision=revision,
                terms=terms,
                max_chars=per_file_limit,
            )
            if not rendered:
                continue
            rendered_parts.append(rendered)
            selected_paths.append(document.relative_path)
            matched_span_count += matched_count
            remaining -= len(rendered) + separator_chars
        block = "\n\n".join(rendered_parts).strip()
        return (
            block,
            selected_paths,
            {
                "structureMapCount": len(selected_paths),
                "matchedSpanCount": matched_span_count,
                "queryTerms": terms,
                "requiresFullReadBeforeWrite": True,
                "strategy": "structure_map_matched_spans_jit_read",
                "relevanceMatched": relevance_matched,
                "unmatchedFallbackUsed": bool(selected_paths) and not relevance_matched,
            },
        )


_SERVICE = DocumentStructureContextService()


def get_document_structure_context_service() -> DocumentStructureContextService:
    return _SERVICE
