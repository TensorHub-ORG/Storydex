from __future__ import annotations

import json
import re
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
# 需要移除的 SillyTavern 专有宏（变量系统/注释/其他动态宏）
_SETVAR_PATTERN = re.compile(r"\{\{\s*setvar\s*::[^}]*\}\}", re.IGNORECASE)
_GETVAR_PATTERN = re.compile(r"\{\{\s*getvar\s*::[^}]*\}\}", re.IGNORECASE)
_COMMENT_MACRO_PATTERN = re.compile(r"\{\{//.*?\}\}", re.DOTALL)
_OTHER_MACRO_PATTERN = re.compile(r"\{\{[^}]*\}\}")


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

    if isinstance(data, dict):
        title = _title_from_payload(data, fallback=title)
        candidates = _extract_prompt_candidates(data)
        sampling = _extract_sampling(data)
        # 提取 SPreset 扩展
        regex_modules, display_regexes = _extract_regex_bindings(data)
        chat_squash_meta = _extract_chatsquash_metadata(data)
        if chat_squash_meta:
            import_warnings.append("SPreset ChatSquash 元数据已提取（JavaScript 不执行）")
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
        # 宏清理（格式适配）
        cleaned_content, macro_warnings = _extract_silly_tavern_macros(candidate.content)
        import_warnings.extend(macro_warnings)

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
                "content": cleaned_content,
                "tags": [tag for tag in ["sillytavern", candidate.role, candidate.source] if tag],
                "sourceFormat": "sillytavern",
                "sourceIdentifier": candidate.identifier,
                "sourceName": candidate.name,
            }
        )

    # 构造 meta（含导入元数据）
    meta_dict: Dict[str, Any] = {
        "name": title,
        "description": f"Imported from SillyTavern preset {filename}. Review modules before activation.",
        "compatibleProviders": [],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceFormat": "sillytavern",
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
            "moduleProfiles": [{"id": "sillytavern", "label": "SillyTavern"}],
            "riskPolicy": {"filteredOnImport": False},
            "runtimeDefaults": {"sourceFormat": "sillytavern"},
        }
    )
    markdown = _render_markdown(title=title, filename=filename, modules=modules)
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
    return _dedupe_candidates(prompts)


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
        role = _first_text(raw, ("role",)) or "system"
        order_info = order.get(identifier)
        is_marker = raw.get("marker") is True
        injection_position = raw.get("injection_position")
        if isinstance(injection_position, bool):
            injection_position = None
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


def _extract_prompt_order(raw_order: Any) -> Dict[str, Tuple[int, bool]]:
    if not isinstance(raw_order, list):
        return {}
    selected: List[Any] = []
    for entry in raw_order:
        if not isinstance(entry, dict):
            continue
        order = entry.get("order")
        if isinstance(order, list) and len(order) > len(selected):
            selected = order
    result: Dict[str, Tuple[int, bool]] = {}
    for index, entry in enumerate(selected):
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            continue
        result[identifier] = (index, entry.get("enabled") is not False)
    return result


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


def _extract_silly_tavern_macros(content: str) -> Tuple[str, List[str]]:
    """清理 SillyTavern 专有宏（格式适配，非内容过滤）。

    保留 {{user}} {{char}}，移除 setvar/getvar/注释/其他动态宏。
    返回 (清理后内容, 警告列表)。
    """
    if not content:
        return content, []
    warnings: List[str] = []
    cleaned = content

    # 检测并移除 setvar
    matches = _SETVAR_PATTERN.findall(cleaned)
    for m in matches:
        warnings.append(f"移除 SillyTavern 宏: {m.strip()}")
    cleaned = _SETVAR_PATTERN.sub("", cleaned)

    # 检测并移除 getvar
    matches = _GETVAR_PATTERN.findall(cleaned)
    for m in matches:
        warnings.append(f"移除 SillyTavern 宏: {m.strip()}")
    cleaned = _GETVAR_PATTERN.sub("", cleaned)

    # 检测并移除注释宏 {{//...}}
    matches = _COMMENT_MACRO_PATTERN.findall(cleaned)
    for m in matches[:3]:  # 只警告前3个，避免刷屏
        warnings.append(f"移除注释宏: {m.strip()[:60]}")
    cleaned = _COMMENT_MACRO_PATTERN.sub("", cleaned)

    # 移除其他 {{xxx}} 宏（保留 {{user}} {{char}}）
    # 先保护 user/char
    placeholder_map: Dict[str, str] = {}

    def _protect(match: re.Match) -> str:
        key = f"__STORYDEX_MACRO_PLACEHOLDER_{len(placeholder_map)}__"
        placeholder_map[key] = match.group(0)
        return key

    cleaned = _PRESERVED_MACROS.sub(_protect, cleaned)
    # 移除其他宏
    other_matches = _OTHER_MACRO_PATTERN.findall(cleaned)
    for m in other_matches[:5]:
        warnings.append(f"移除未识别宏: {m.strip()[:60]}")
    cleaned = _OTHER_MACRO_PATTERN.sub("", cleaned)
    # 恢复 user/char
    for key, original in placeholder_map.items():
        cleaned = cleaned.replace(key, original)

    # 清理多余空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, warnings


def _extract_regex_bindings(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """提取 SPreset RegexBinding 扩展。

    返回 (prompt_rule_modules, display_rule_metadata)。
    - promptOnly: true → 转为 negative_rules 槽位模块
    - markdownOnly: true → 存入 display_rules 元数据
    - disabled: true → 跳过（作者主动禁用）
    """
    prompt_modules: List[Dict[str, Any]] = []
    display_rules: List[Dict[str, Any]] = []

    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        return prompt_modules, display_rules

    spreset = extensions.get("SPreset")
    if not isinstance(spreset, dict):
        return prompt_modules, display_rules

    regex_binding = spreset.get("RegexBinding")
    if not isinstance(regex_binding, dict):
        return prompt_modules, display_rules

    regexes = regex_binding.get("regexes")
    if not isinstance(regexes, list):
        return prompt_modules, display_rules

    for index, regex_entry in enumerate(regexes):
        if not isinstance(regex_entry, dict):
            continue
        if regex_entry.get("disabled") is True:
            continue

        script_name = regex_entry.get("scriptName") or f"regex_{index + 1}"
        find_regex = regex_entry.get("findRegex") or ""
        replace_string = regex_entry.get("replaceString") or ""
        is_prompt_only = regex_entry.get("promptOnly") is True
        is_markdown_only = regex_entry.get("markdownOnly") is True

        # 每个正则同时可能有多种用途，按规则处理
        if is_prompt_only:
            # promptOnly 正则作为输入清洗规则，放入 negative_rules
            module_id = f"st_regex_{_ID_CLEANUP.sub('_', script_name).strip('_').lower()[:48]}"
            content = f"[SillyTavern 正则规则: {script_name}]\n查找: {find_regex}\n替换: {replace_string}"
            prompt_modules.append(
                {
                    "id": module_id,
                    "title": f"正则: {script_name}",
                    "slot": "negative_rules",
                    "enabledByDefault": True,
                    "priority": 50,
                    "scope": "global",
                    "content": content,
                    "tags": ["sillytavern", "regex", "prompt_only"],
                    "sourceFormat": "sillytavern",
                    "sourceIdentifier": regex_entry.get("id") or module_id,
                    "sourceName": script_name,
                }
            )

        if is_markdown_only:
            # markdownOnly 正则仅存为元数据
            display_rules.append(
                {
                    "scriptName": script_name,
                    "findRegex": find_regex,
                    "replaceString": replace_string,
                    "markdownOnly": True,
                    "id": regex_entry.get("id"),
                }
            )

    return prompt_modules, display_rules


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
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Source: SillyTavern preset `{filename}`",
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
