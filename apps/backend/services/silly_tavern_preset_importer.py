from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.preset_schema import PresetDocument


_TEXT_FIELDS = ("content", "text", "prompt", "value")
_TITLE_FIELDS = ("name", "title", "label", "identifier", "id")
_FILENAME_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_ID_CLEANUP = re.compile(r"[^0-9A-Za-z_]+")

# 保留的 SillyTavern 宏（Storydex 上下文装配器可解析）
_PRESERVED_MACROS = re.compile(r"\{\{\s*(user|char)\s*\}\}", re.IGNORECASE)
# SillyTavern 专有宏保留原文导入，运行时兼容层后续再展开。
_OTHER_MACRO_PATTERN = re.compile(r"\{\{[^}]*\}\}")
_MACRO_NAME_PATTERN = re.compile(r"^\{\{\s*(//|[!/#]?[A-Za-z][A-Za-z0-9_]*)")

# SillyTavern 聊天补全导出的 prompt_order 里，100001 是所有角色共用的默认档，
# 100000 是内置 fallback；优先取 100001 才与 SillyTavern 实际生效的开关一致。
_ST_DUMMY_CHARACTER_IDS = (100001, 100000)

# ST 导出的 role 存在 Gemini/Claude 风格别名，归一化成 OpenAI 三角色。
_ROLE_ALIASES = {
    "model": "assistant",
    "bot": "assistant",
    "ai": "assistant",
    "human": "user",
}
# 不在 prompt_order 中的 prompt 排到已排序模块之后，避免与 order 序号混排。
_UNORDERED_SOURCE_ORDER_BASE = 100000


@dataclass
class FilteredPresetBlock:
    name: str
    identifier: str
    reason: str


@dataclass
class SillyTavernPresetImportResult:
    title: str
    document: PresetDocument
    markdown: str
    module_count: int
    filtered_count: int
    filtered_blocks: List[FilteredPresetBlock] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    display_regexes: List[Dict[str, Any]] = field(default_factory=list)
    chat_squash_meta: Dict[str, Any] = field(default_factory=dict)
    import_warnings: List[str] = field(default_factory=list)


@dataclass
class _PromptCandidate:
    identifier: str
    name: str
    content: str
    enabled: bool
    role: str
    index: int
    source: str
    order_index: Optional[int] = None
    order_enabled: Optional[bool] = None
    marker: bool = False
    injection_position: Optional[int] = None
    injection_depth: Optional[int] = None
    injection_order: Optional[int] = None
    injection_trigger: List[str] = field(default_factory=list)
    system_prompt: Optional[bool] = None
    forbid_overrides: Optional[bool] = None


def convert_silly_tavern_preset(content: bytes, *, filename: str) -> SillyTavernPresetImportResult:
    text = _decode_text(content)
    title = Path(filename or "imported-preset").stem
    warnings: List[str] = []
    import_warnings: List[str] = []

    data: Any = None
    if _looks_like_json(filename, text):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"JSON parse failed; imported as plain text: {exc}")

    display_regexes: List[Dict[str, Any]] = []
    chat_squash_meta: Dict[str, Any] = {}
    source_format = "generic"

    if isinstance(data, dict):
        title = _title_from_payload(data, fallback=title)
        candidates = _extract_prompt_candidates(data)
        sampling = _extract_sampling(data)
        if _is_silly_tavern_payload(data):
            source_format = "sillytavern"
        # 提取 SPreset 扩展
        regex_modules, display_regexes = _extract_regex_bindings(data)
        chat_squash_meta = _extract_chatsquash_metadata(data)
        if chat_squash_meta:
            import_warnings.append("SPreset ChatSquash 元数据已提取（JavaScript 不执行）")
    elif isinstance(data, list):
        candidates = _extract_list_prompts(data)
        sampling = {}
        regex_modules = []
    else:
        candidates = [
            _PromptCandidate(
                identifier="plain_text",
                name=title,
                content=text,
                enabled=True,
                role="system",
                index=0,
                source="text",
            )
        ]
        sampling = {}
        regex_modules = []

    modules: List[Dict[str, Any]] = []
    used_ids: set[str] = set()
    macro_counter: Counter[str] = Counter()

    # 先添加正则绑定生成的模块（promptOnly 类）
    for regex_mod in regex_modules:
        module_id = regex_mod["id"]
        # 确保 id 唯一
        base = module_id
        suffix = 2
        while module_id in used_ids:
            module_id = f"{base}_{suffix}"
            suffix += 1
        regex_mod["id"] = module_id
        used_ids.add(module_id)
        modules.append(regex_mod)

    for candidate in candidates:
        # marker 跳过（结构解析，非内容过滤）：marker 是 SillyTavern 的占位符
        if candidate.marker:
            continue
        # 空内容跳过
        if not _normalize_prompt_text(candidate.content):
            continue
        # 宏兼容提示（导入层保留原文，按宏名聚合后统一输出）
        macro_counter.update(_collect_silly_tavern_macro_names(candidate.content))

        module_id = _unique_module_id(candidate, used_ids)
        used_ids.add(module_id)
        order_index = candidate.order_index if candidate.order_index is not None else candidate.index
        modules.append(
            {
                "id": module_id,
                "title": candidate.name or candidate.identifier or module_id,
                "slot": _infer_slot(candidate),
                "enabledByDefault": _candidate_enabled(candidate),
                "priority": max(1, 1000 - int(order_index or 0)),
                "scope": "global",
                "content": candidate.content,
                "tags": [tag for tag in [source_format, candidate.role, candidate.source] if tag],
                "sourceFormat": source_format,
                "sourceIdentifier": candidate.identifier,
                "sourceName": candidate.name,
                "sourceOrder": int(order_index or 0),
                "sourceRole": candidate.role,
                "sourceSystemPrompt": candidate.system_prompt,
                "forbidOverrides": candidate.forbid_overrides,
                "injectionPosition": candidate.injection_position,
                "injectionDepth": candidate.injection_depth,
                "injectionOrder": candidate.injection_order,
                "injectionTrigger": candidate.injection_trigger,
            }
        )

    import_warnings.extend(_summarize_macro_hints(macro_counter))

    # 兜底：只要文件非空，导入永远不产出空预设（开发阶段要求全部可导入）。
    if not modules and text.strip():
        modules.append(
            {
                "id": "imported_raw_content",
                "title": title,
                "slot": "advanced",
                "enabledByDefault": True,
                "priority": 500,
                "scope": "global",
                "content": text,
                "tags": ["imported", "raw"],
                "sourceFormat": source_format,
                "sourceIdentifier": "raw_content",
                "sourceName": title,
                "sourceOrder": 0,
                "sourceRole": "system",
            }
        )
        import_warnings.append("未识别出结构化模块，已将文件原文导入为单个模块。")

    format_label = "SillyTavern" if source_format == "sillytavern" else "imported"
    # 构造 meta（含导入元数据）
    meta_dict: Dict[str, Any] = {
        "name": title,
        "description": f"Imported from {format_label} preset {filename}. Review modules before activation.",
        "compatibleProviders": [],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceFormat": source_format,
    }
    if display_regexes:
        meta_dict["displayRegexes"] = display_regexes
    if chat_squash_meta:
        meta_dict["chatSquashMeta"] = chat_squash_meta
    if import_warnings:
        meta_dict["importWarnings"] = import_warnings

    document = PresetDocument.model_validate(
        {
            "version": 1,
            "meta": meta_dict,
            "sampling": {"default": sampling, "perPurpose": {}},
            "modules": modules,
            "moduleProfiles": [{"id": source_format, "label": format_label}],
            "riskPolicy": {"filteredOnImport": False},
            "runtimeDefaults": {"sourceFormat": source_format},
            "sillyTavern": {
                "sourceFilename": filename,
                "sourcePreset": data if isinstance(data, (dict, list)) else None,
                "selectedPromptOrder": _selected_prompt_order(data.get("prompt_order")) if isinstance(data, dict) else [],
            },
        }
    )
    markdown = _render_markdown(title=title, filename=filename, modules=modules, format_label=format_label)
    return SillyTavernPresetImportResult(
        title=title,
        document=document,
        markdown=markdown,
        module_count=len(modules),
        filtered_count=0,  # 开发阶段不过滤
        filtered_blocks=[],  # 开发阶段不过滤
        warnings=warnings,
        display_regexes=display_regexes,
        chat_squash_meta=chat_squash_meta,
        import_warnings=import_warnings,
    )


def safe_preset_filename_stem(title: str, *, fallback: str = "imported-preset") -> str:
    stem = _FILENAME_FORBIDDEN.sub("_", str(title or "").strip()).strip(" .")
    stem = re.sub(r"\s+", " ", stem)
    if not stem:
        stem = fallback
    return stem[:80].strip() or fallback


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _looks_like_json(filename: str, text: str) -> bool:
    if str(filename or "").lower().endswith(".json"):
        return True
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _title_from_payload(data: Dict[str, Any], *, fallback: str) -> str:
    for key in ("name", "title", "display_name", "identifier"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = data.get("meta")
    if isinstance(meta, dict):
        for key in ("name", "title"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    root = data.get("root")
    if isinstance(root, dict):
        for key in ("title", "name", "identifier"):
            value = root.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _extract_sampling(data: Dict[str, Any]) -> Dict[str, Any]:
    mapping = {
        "temperature": "temperature",
        "top_p": "topP",
        "topP": "topP",
        "top_k": "topK",
        "topK": "topK",
        "frequency_penalty": "frequencyPenalty",
        "frequencyPenalty": "frequencyPenalty",
        "presence_penalty": "presencePenalty",
        "presencePenalty": "presencePenalty",
        "seed": "seed",
    }
    sampling: Dict[str, Any] = {}
    for source_key, target_key in mapping.items():
        value = data.get(source_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            sampling[target_key] = value
    return sampling


def _extract_prompt_candidates(data: Dict[str, Any]) -> List[_PromptCandidate]:
    prompts: List[_PromptCandidate] = []
    prompts.extend(_extract_silly_tavern_prompts(data))
    if not prompts:
        root = data.get("root")
        if isinstance(root, dict):
            prompts.extend(_extract_tree_prompts(root))
    if not prompts and isinstance(data.get("children"), list):
        prompts.extend(_extract_tree_prompts(data))
    if not prompts:
        prompts.extend(_extract_generic_field_prompts(data))
    return _dedupe_candidates(prompts)


def _is_silly_tavern_payload(data: Dict[str, Any]) -> bool:
    return isinstance(data.get("prompts"), list) or isinstance(data.get("prompt_order"), list)


def _extract_list_prompts(items: List[Any]) -> List[_PromptCandidate]:
    """顶层 JSON 数组：字符串或 prompt 形态的对象，逐条导入。"""
    prompts: List[_PromptCandidate] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            if not item.strip():
                continue
            prompts.append(
                _PromptCandidate(
                    identifier=f"item_{index + 1}",
                    name=f"item_{index + 1}",
                    content=item,
                    enabled=True,
                    role="system",
                    index=index,
                    source="list",
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        content = _first_text(item, _TEXT_FIELDS)
        if not content.strip():
            continue
        identifier = _first_text(item, ("identifier", "id", "name", "title")) or f"item_{index + 1}"
        prompts.append(
            _PromptCandidate(
                identifier=identifier,
                name=_first_text(item, _TITLE_FIELDS) or identifier,
                content=content,
                enabled=item.get("enabled") is not False,
                role=_normalize_role(_first_text(item, ("role",))),
                index=index,
                source="list",
            )
        )
    return prompts


# 通用 JSON 回退提取时跳过的元信息键（这些不是 prompt 内容）。
_GENERIC_META_KEYS = {
    "name", "title", "label", "identifier", "id", "version", "char_version",
    "creator", "author", "create_date", "created", "updated", "updatedat",
    "tags", "spec", "spec_version", "avatar", "extensions", "type", "kind",
}


def _extract_generic_field_prompts(data: Dict[str, Any], *, prefix: str = "", depth: int = 0) -> List[_PromptCandidate]:
    """普通 JSON（无 prompts/root/children 结构）的回退提取。

    把顶层（以及 chara card 风格的 `data` 一层）里有实际内容的字符串字段、
    字符串列表、prompt 形态的对象列表都收进来，保证普通预设也能完整导入。
    """
    prompts: List[_PromptCandidate] = []
    for key, value in data.items():
        key_text = str(key)
        lowered = key_text.lower()
        if lowered in _GENERIC_META_KEYS:
            continue
        identifier = f"{prefix}{key_text}" if prefix else key_text
        if isinstance(value, str):
            if len(value.strip()) < 8:
                continue
            prompts.append(
                _PromptCandidate(
                    identifier=identifier,
                    name=identifier,
                    content=value,
                    enabled=True,
                    role="system",
                    index=len(prompts),
                    source="generic",
                )
            )
        elif isinstance(value, list):
            prompts.extend(
                _reindex_candidates(_extract_list_prompts(value), base=len(prompts), prefix=f"{identifier}_")
            )
        elif isinstance(value, dict) and depth == 0 and lowered in {"data", "config", "preset", "settings"}:
            prompts.extend(
                _reindex_candidates(
                    _extract_generic_field_prompts(value, prefix=f"{identifier}.", depth=depth + 1),
                    base=len(prompts),
                )
            )
    return prompts


def _reindex_candidates(candidates: List[_PromptCandidate], *, base: int, prefix: str = "") -> List[_PromptCandidate]:
    for offset, candidate in enumerate(candidates):
        candidate.index = base + offset
        if prefix and not candidate.identifier.startswith(prefix):
            candidate.identifier = f"{prefix}{candidate.identifier}"
            candidate.name = candidate.name or candidate.identifier
    return candidates


def _extract_silly_tavern_prompts(data: Dict[str, Any]) -> List[_PromptCandidate]:
    raw_prompts = data.get("prompts")
    if not isinstance(raw_prompts, list):
        return []
    order = _extract_prompt_order(data.get("prompt_order"))
    prompts: List[_PromptCandidate] = []
    for index, raw in enumerate(raw_prompts):
        if not isinstance(raw, dict):
            continue
        content = _first_text(raw, _TEXT_FIELDS)
        identifier = _first_text(raw, ("identifier", "id", "name")) or f"prompt_{index + 1}"
        name = _first_text(raw, ("name", "title", "identifier")) or identifier
        role = _normalize_role(_first_text(raw, ("role",)))
        order_info = order.get(identifier)
        if order and order_info is None:
            # ST 语义：prompt 的启用状态完全由 prompt_order 决定；不在 order 中
            # 的 prompt 不参与生成。仍然全部导入，只是默认开关为关，排序后置。
            order_info = (_UNORDERED_SOURCE_ORDER_BASE + index, False)
        is_marker = raw.get("marker") is True
        injection_position = raw.get("injection_position")
        if isinstance(injection_position, bool):
            injection_position = None
        injection_depth = raw.get("injection_depth")
        if isinstance(injection_depth, bool):
            injection_depth = None
        injection_order = raw.get("injection_order")
        if isinstance(injection_order, bool):
            injection_order = None
        raw_trigger = raw.get("injection_trigger")
        injection_trigger = [str(item).strip().lower() for item in raw_trigger if str(item).strip()] if isinstance(raw_trigger, list) else []
        prompts.append(
            _PromptCandidate(
                identifier=identifier,
                name=name,
                content=content,
                enabled=raw.get("enabled") is not False,
                role=role,
                index=index,
                source="prompts",
                order_index=order_info[0] if order_info else None,
                order_enabled=order_info[1] if order_info else None,
                marker=is_marker,
                injection_position=injection_position if isinstance(injection_position, int) else None,
                injection_depth=injection_depth if isinstance(injection_depth, int) else None,
                injection_order=injection_order if isinstance(injection_order, int) else None,
                injection_trigger=injection_trigger,
                system_prompt=raw.get("system_prompt") if isinstance(raw.get("system_prompt"), bool) else None,
                forbid_overrides=raw.get("forbid_overrides") if isinstance(raw.get("forbid_overrides"), bool) else None,
            )
        )
    prompts.sort(
        key=lambda item: (
            item.order_index is None,
            item.order_index if item.order_index is not None else item.index,
            item.index,
        )
    )
    return prompts


def _normalize_role(raw_role: str) -> str:
    role = str(raw_role or "").strip().lower()
    if not role:
        return "system"
    return _ROLE_ALIASES.get(role, role)


def _extract_prompt_order(raw_order: Any) -> Dict[str, Tuple[int, bool]]:
    selected = _selected_prompt_order(raw_order)
    result: Dict[str, Tuple[int, bool]] = {}
    for index, entry in enumerate(selected):
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            continue
        result[identifier] = (index, entry.get("enabled") is not False)
    return result


def _selected_prompt_order(raw_order: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_order, list):
        return []
    direct_entries = [entry for entry in raw_order if isinstance(entry, dict) and isinstance(entry.get("identifier"), str)]
    if direct_entries:
        return direct_entries
    by_character: Dict[Any, List[Any]] = {}
    for entry in raw_order:
        if not isinstance(entry, dict):
            continue
        order = entry.get("order")
        if isinstance(order, list):
            by_character[entry.get("character_id")] = order
    selected: List[Any] = []
    # SillyTavern 聊天补全预设默认读取 dummy character 档（100001）。
    for character_id in _ST_DUMMY_CHARACTER_IDS:
        if by_character.get(character_id):
            selected = by_character[character_id]
            break
    if not selected:
        for order in by_character.values():
            if len(order) > len(selected):
                selected = order
    return [entry for entry in selected if isinstance(entry, dict)]


def _extract_tree_prompts(root: Dict[str, Any]) -> List[_PromptCandidate]:
    prompts: List[_PromptCandidate] = []

    def walk(node: Dict[str, Any], inherited_enabled: bool, path: Tuple[int, ...]) -> None:
        enabled = inherited_enabled and node.get("enabled") is not False
        content = _first_text(node, _TEXT_FIELDS)
        children = node.get("children")
        is_marker = node.get("marker") is True or node.get("kind") == "marker"
        if content.strip():
            identifier = _first_text(node, ("identifier", "id", "name", "title")) or "node_" + "_".join(map(str, path))
            name = _first_text(node, _TITLE_FIELDS) or identifier
            prompts.append(
                _PromptCandidate(
                    identifier=identifier,
                    name=name,
                    content=content,
                    enabled=enabled,
                    role=_first_text(node, ("role",)) or "system",
                    index=len(prompts),
                    source="tree",
                    marker=is_marker,
                )
            )
        if isinstance(children, list):
            for child_index, child in enumerate(children):
                if isinstance(child, dict):
                    walk(child, enabled, (*path, child_index))

    walk(root, True, (0,))
    return prompts


def _dedupe_candidates(candidates: Iterable[_PromptCandidate]) -> List[_PromptCandidate]:
    seen: set[Tuple[str, str]] = set()
    result: List[_PromptCandidate] = []
    for candidate in candidates:
        key = (candidate.identifier, candidate.content.strip())
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _first_text(data: Dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _candidate_enabled(candidate: _PromptCandidate) -> bool:
    if candidate.order_enabled is not None:
        return candidate.order_enabled
    return candidate.enabled


def _unique_module_id(candidate: _PromptCandidate, used_ids: set[str]) -> str:
    raw = candidate.identifier or candidate.name or f"module_{candidate.index + 1}"
    slug = _ID_CLEANUP.sub("_", raw).strip("_").lower()
    if not slug:
        slug = f"module_{candidate.index + 1}"
    base = "st_" + slug[:54].strip("_")
    module_id = base
    suffix = 2
    while module_id in used_ids:
        module_id = f"{base}_{suffix}"
        suffix += 1
    return module_id


def _normalize_prompt_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _collect_silly_tavern_macro_names(content: str) -> Counter[str]:
    """按宏名统计模块内容中的 SillyTavern 宏（{{user}}/{{char}} 除外）。

    Storydex 导入阶段不执行 SillyTavern 宏，也不删除宏。这样外部预设可以
    保真进入项目，后续运行时兼容层再决定如何展开变量、随机、时间等语义。
    """
    counter: Counter[str] = Counter()
    if not content:
        return counter
    for match in _OTHER_MACRO_PATTERN.finditer(content):
        macro = match.group(0).strip()
        if not macro or _PRESERVED_MACROS.fullmatch(macro):
            continue
        name_match = _MACRO_NAME_PATTERN.match(macro)
        name = name_match.group(1) if name_match else macro[2:22].strip() or "?"
        counter[name] += 1
    return counter


def _summarize_macro_hints(counter: Counter[str], *, max_lines: int = 40) -> List[str]:
    """把宏计数聚合成少量提示行，避免大预设产生成百上千条提示。"""
    if not counter:
        return []
    hints: List[str] = []
    for name, count in counter.most_common(max_lines):
        hints.append(f"保留 SillyTavern 宏 {{{{{name}}}}} ×{count}（导入阶段不执行，运行时兼容层会尽量展开）")
    remaining = len(counter) - max_lines
    if remaining > 0:
        hints.append(f"另有 {remaining} 种宏同样保留原文，详见模块内容。")
    return hints


def _extract_regex_bindings(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """提取 SPreset RegexBinding 扩展。

    返回 (prompt_rule_modules, display_rule_metadata)。
    导入阶段不执行、不注入正则，避免把社区预设的输出处理规则变成
    Storydex 运行时过滤。这里只保留元数据，供后续专门的 ST 兼容层使用。
    """
    display_rules: List[Dict[str, Any]] = []
    seen: set[str] = set()

    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        return [], display_rules

    spreset = extensions.get("SPreset")
    regex_sources: List[Tuple[str, Any]] = []
    if isinstance(spreset, dict):
        regex_binding = spreset.get("RegexBinding")
        if isinstance(regex_binding, dict):
            regex_sources.append(("SPreset.RegexBinding", regex_binding.get("regexes")))
    regex_sources.append(("extensions.regex_scripts", extensions.get("regex_scripts")))

    for source, regexes in regex_sources:
        if not isinstance(regexes, list):
            continue
        for index, regex_entry in enumerate(regexes):
            if not isinstance(regex_entry, dict):
                continue
            script_name = str(regex_entry.get("scriptName") or f"regex_{index + 1}")
            find_regex = str(regex_entry.get("findRegex") or "")
            key = str(regex_entry.get("id") or f"{script_name}\n{find_regex}")
            if key in seen:
                continue
            seen.add(key)
            display_rules.append(
                {
                    "source": source,
                    "scriptName": script_name,
                    "findRegex": find_regex,
                    "replaceString": regex_entry.get("replaceString") or "",
                    "promptOnly": regex_entry.get("promptOnly") is True,
                    "markdownOnly": regex_entry.get("markdownOnly") is True,
                    "disabled": regex_entry.get("disabled") is True,
                    "runOnEdit": regex_entry.get("runOnEdit") is True,
                    "substituteRegex": regex_entry.get("substituteRegex"),
                    "id": regex_entry.get("id"),
                }
            )

    return [], display_rules


def _extract_chatsquash_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    """提取 SPreset ChatSquash 元数据（不执行 JavaScript）。

    squashed_post_script 作为文本存入元数据供开发参考。
    """
    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        return {}

    spreset = extensions.get("SPreset")
    if not isinstance(spreset, dict):
        return {}

    chat_squash = spreset.get("ChatSquash")
    if not isinstance(chat_squash, dict):
        return {}

    meta: Dict[str, Any] = {}
    for key in ("user_prefix", "char_prefix", "prefix_system", "suffix_system", "user_role_system", "stop_string"):
        value = chat_squash.get(key)
        if isinstance(value, str) and value.strip():
            meta[key] = value
    # squashed_post_script 是 JavaScript，作为文本保留供开发参考（不执行）
    post_script = chat_squash.get("squashed_post_script")
    if isinstance(post_script, str) and post_script.strip():
        meta["squashed_post_script"] = post_script
    meta["enabled"] = chat_squash.get("enabled")
    return meta


def _infer_slot(candidate: _PromptCandidate) -> str:
    """改进的槽位推断：综合 role/marker/injection_position/关键词。"""
    label = f"{candidate.name} {candidate.identifier}".lower()

    # injection_position 判断：1 = 作者注释（Author's Note 位置）
    if candidate.injection_position == 1:
        return "author_reference"

    # role + name 权重
    if candidate.role == "system":
        if re.search(r"(main|主提示|主任务|初始化|职责|边界|安全|长度|字数|输出|格式|content|task|reset)", label):
            return "boundary"

    # 关键词推断（fallback）
    if re.search(r"(main|system|init|初始化|职责|边界|安全|长度|字数|输出|格式|content|task|reset)", label):
        return "boundary"
    if re.search(r"(author|reference|作家|参考|借鉴|作者注释)", label):
        return "author_reference"
    if re.search(r"(style|prose|writing|dialogue|文风|语言|写法|对白|反模板|节奏|漫改|吐槽)", label):
        return "language_mechanics"
    if re.search(r"(world|scenario|scene|character|persona|npc|角色|场景|世界|设定|剧情|性格|锚定)", label):
        return "scene_module"
    if re.search(r"(forbid|avoid|negative|ban|禁|不要|防|杀|去八股|润色)", label):
        return "negative_rules"
    if re.search(r"(check|summary|summar|总结|检查|自检|摘要)", label):
        return "self_check"
    return "advanced"


def _render_markdown(
    *,
    title: str,
    filename: str,
    modules: List[Dict[str, Any]],
    format_label: str = "SillyTavern",
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Source: {format_label} preset `{filename}`",
        f"Imported modules: {len(modules)}",
        "",
        "Review the sidecar module switches before activation.",
    ]
    if modules:
        lines.extend(["", "## Modules"])
        for module in modules:
            state = "on" if module.get("enabledByDefault") is not False else "off"
            lines.append(f"- [{state}] {module.get('title') or module.get('id')} ({module.get('slot')})")
    return "\n".join(lines).rstrip() + "\n"
