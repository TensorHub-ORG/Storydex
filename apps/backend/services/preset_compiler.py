"""Preset module compiler and static risk checker.

This service is intentionally independent from the prompt renderer so the UI
preview and Stage-1 injection can share the same compiled source.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set

from pydantic import BaseModel, ConfigDict, Field

from services.preset_schema import PresetDocument


_EXTRA_ALLOW = ConfigDict(extra="allow", populate_by_name=True)

RiskLevel = Literal["error", "warning", "info"]


class PresetModule(BaseModel):
    model_config = _EXTRA_ALLOW

    id: str
    title: str = ""
    slot: str = "advanced"
    enabled_by_default: bool = Field(default=True, alias="enabledByDefault")
    priority: int = 50
    scope: str = "global"
    placement: str = "turn_plan"
    purpose: List[str] = Field(default_factory=list)
    content: str = ""
    tags: List[str] = Field(default_factory=list)
    virtual: bool = False
    source_format: str = Field(default="", alias="sourceFormat")
    source_identifier: str = Field(default="", alias="sourceIdentifier")
    source_name: str = Field(default="", alias="sourceName")
    source_order: Optional[int] = Field(default=None, alias="sourceOrder")
    source_role: str = Field(default="", alias="sourceRole")
    source_system_prompt: Optional[bool] = Field(default=None, alias="sourceSystemPrompt")
    forbid_overrides: Optional[bool] = Field(default=None, alias="forbidOverrides")
    injection_position: Optional[int] = Field(default=None, alias="injectionPosition")
    injection_depth: Optional[int] = Field(default=None, alias="injectionDepth")
    injection_order: Optional[int] = Field(default=None, alias="injectionOrder")
    injection_trigger: List[str] = Field(default_factory=list, alias="injectionTrigger")


class PresetRuntimeOverrides(BaseModel):
    model_config = _EXTRA_ALLOW

    enabled_module_ids: List[str] = Field(default_factory=list, alias="enabledModuleIds")
    disabled_module_ids: List[str] = Field(default_factory=list, alias="disabledModuleIds")
    temporary_rules: List[str] = Field(default_factory=list, alias="temporaryRules")


class PresetCompiledSection(BaseModel):
    model_config = _EXTRA_ALLOW

    id: str
    title: str
    slot: str
    source_module_id: str = Field(alias="sourceModuleId")
    priority: int
    enabled: bool
    scope: str
    placement: str = "turn_plan"
    purpose: List[str] = Field(default_factory=list)
    text: str
    virtual: bool = False
    source_order: Optional[int] = Field(default=None, alias="sourceOrder")
    source_role: str = Field(default="", alias="sourceRole")
    injection_position: Optional[int] = Field(default=None, alias="injectionPosition")
    injection_depth: Optional[int] = Field(default=None, alias="injectionDepth")
    injection_order: Optional[int] = Field(default=None, alias="injectionOrder")


class PresetCompiledInjection(BaseModel):
    model_config = _EXTRA_ALLOW

    depth: int
    order: int
    role: str
    text: str
    source_module_ids: List[str] = Field(default_factory=list, alias="sourceModuleIds")


class PresetRisk(BaseModel):
    model_config = _EXTRA_ALLOW

    level: RiskLevel
    code: str
    message: str
    source_module_id: str = Field(default="", alias="sourceModuleId")
    line: Optional[int] = None


class PresetCompileResult(BaseModel):
    model_config = _EXTRA_ALLOW

    compiled_text: str = Field(alias="compiledText")
    sections: List[PresetCompiledSection] = Field(default_factory=list)
    injections: List[PresetCompiledInjection] = Field(default_factory=list)
    risks: List[PresetRisk] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


_SLOT_ORDER: Dict[str, int] = {
    "boundary": 0,
    "author_reference": 10,
    "language_mechanics": 20,
    "scene_module": 30,
    "negative_rules": 40,
    "self_check": 50,
    "advanced": 60,
}

# 外部导入的预设：按导入顺序编译、不加 Storydex
# 段头、运行时展开外部宏。Storydex 原生预设仍走槽位排序 + 段头渲染。
_IMPORTED_SOURCE_FORMATS = {"sillytavern", "generic"}


def compile_preset(
    doc: PresetDocument,
    *,
    overrides: Optional[PresetRuntimeOverrides | Dict[str, Any]] = None,
    runtime_context: Optional[Dict[str, Any]] = None,
) -> PresetCompileResult:
    runtime_overrides = _coerce_overrides(overrides)
    modules = _modules_from_document(doc)
    warnings = _validate_modules(modules)
    source_format = _document_source_format(doc)
    imported_format = source_format in _IMPORTED_SOURCE_FORMATS
    macro_runtime = None
    if imported_format:
        from services.silly_tavern_macro_runtime import create_silly_tavern_macro_runtime

        macro_runtime = create_silly_tavern_macro_runtime(runtime_context)
    generation_type = _runtime_generation_type(runtime_context)

    disabled: Set[str] = set(runtime_overrides.disabled_module_ids)
    enabled_extra: Set[str] = set(runtime_overrides.enabled_module_ids)

    sections: List[PresetCompiledSection] = []
    for module in sorted(modules, key=lambda item: _module_sort_key(item, source_format=source_format)):
        enabled = (module.enabled_by_default or module.id in enabled_extra) and module.id not in disabled
        if not enabled:
            continue
        if imported_format and not _silly_tavern_trigger_allows(module, generation_type):
            continue
        text = module.content.strip()
        if macro_runtime is not None:
            text = macro_runtime.expand(text).strip()
        if not text:
            continue
        sections.append(
            PresetCompiledSection(
                id=f"{module.slot}:{module.id}",
                title=module.title or module.id,
                slot=module.slot,
                sourceModuleId=module.id,
                priority=module.priority,
                enabled=True,
                scope=module.scope,
                placement=module.placement,
                purpose=module.purpose,
                text=text,
                virtual=module.virtual,
                sourceOrder=module.source_order,
                sourceRole=module.source_role,
                injectionPosition=module.injection_position,
                injectionDepth=module.injection_depth,
                injectionOrder=module.injection_order,
            )
        )

    if runtime_overrides.temporary_rules:
        text = "\n".join(rule.strip() for rule in runtime_overrides.temporary_rules if rule.strip())
        if text:
            sections.append(
                PresetCompiledSection(
                    id="runtime:temporary_rules",
                    title="本轮临时规则",
                    slot="boundary",
                    sourceModuleId="runtime_temporary_rules",
                    priority=110,
                    enabled=True,
                    scope="turn",
                    placement="turn_plan",
                    purpose=[],
                    text=text,
                    virtual=True,
                )
            )

    sections.sort(key=lambda item: _section_sort_key(item, source_format=source_format))
    compiled_text = _render_sections(sections, source_format=source_format)
    injections = _silly_tavern_absolute_injections(sections) if imported_format else []
    risks = check_preset_risks(modules, sections)
    return PresetCompileResult(
        compiledText=compiled_text,
        sections=sections,
        injections=injections,
        risks=risks,
        warnings=warnings,
    )


def check_preset_risks(
    modules: Sequence[PresetModule],
    sections: Optional[Sequence[PresetCompiledSection]] = None,
) -> List[PresetRisk]:
    risks: List[PresetRisk] = []
    injected_ids = {section.source_module_id for section in sections or []}
    for module in modules:
        text = module.content or ""
        risks.extend(_scan_module_text(module, text))
        if module.enabled_by_default and module.content.strip() and sections is not None and module.id not in injected_ids:
            risks.append(
                PresetRisk(
                    level="warning",
                    code="not_injected",
                    message="模块默认启用但未进入最终注入文本。",
                    sourceModuleId=module.id,
                )
            )
        if len(text) > 2400:
            risks.append(
                PresetRisk(
                    level="info",
                    code="overlong_module",
                    message="模块内容超过 2400 字，建议拆分为更小的模块。",
                    sourceModuleId=module.id,
                )
            )
    risks.extend(_scan_conflicts(modules))
    return risks


def modules_from_document(doc: PresetDocument) -> List[PresetModule]:
    """Public helper for API/UI summaries."""
    return _modules_from_document(doc)


def _coerce_overrides(overrides: Optional[PresetRuntimeOverrides | Dict[str, Any]]) -> PresetRuntimeOverrides:
    if overrides is None:
        return PresetRuntimeOverrides()
    if isinstance(overrides, PresetRuntimeOverrides):
        return overrides
    return PresetRuntimeOverrides.model_validate(overrides)


def _modules_from_document(doc: PresetDocument) -> List[PresetModule]:
    raw_modules = getattr(doc, "modules", None)
    if isinstance(raw_modules, list) and raw_modules:
        modules: List[PresetModule] = []
        for index, raw in enumerate(raw_modules):
            if not isinstance(raw, dict):
                continue
            try:
                module = PresetModule.model_validate(raw)
            except Exception:
                continue
            if not module.id:
                module.id = f"module_{index + 1}"
            modules.append(module)
        if modules:
            return modules
    return _virtual_modules_from_v1(doc)


def _document_source_format(doc: PresetDocument) -> str:
    source = str(getattr(doc.meta, "source_format", "") or "").strip().lower()
    if source:
        return source
    runtime_defaults = getattr(doc, "__pydantic_extra__", {}) or {}
    if isinstance(runtime_defaults, dict):
        raw_runtime = runtime_defaults.get("runtimeDefaults") or runtime_defaults.get("runtime_defaults")
        if isinstance(raw_runtime, dict):
            return str(raw_runtime.get("sourceFormat") or raw_runtime.get("source_format") or "").strip().lower()
    return ""


def _runtime_generation_type(runtime_context: Optional[Dict[str, Any]]) -> str:
    if not isinstance(runtime_context, dict):
        return "normal"
    value = runtime_context.get("generationType") or runtime_context.get("lastGenerationType") or "normal"
    return str(value or "normal").strip().lower() or "normal"


def _silly_tavern_trigger_allows(module: PresetModule, generation_type: str) -> bool:
    triggers = [str(item).strip().lower() for item in module.injection_trigger if str(item).strip()]
    if not triggers:
        return True
    return generation_type in triggers


_ST_ROLE_ALIASES = {"model": "assistant", "bot": "assistant", "ai": "assistant", "human": "user"}


def _silly_tavern_absolute_injections(sections: Sequence[PresetCompiledSection]) -> List[PresetCompiledInjection]:
    grouped: Dict[tuple[int, int, str], Dict[str, Any]] = {}
    for section in sections:
        if section.injection_position != 1:
            continue
        role = str(section.source_role or "system").strip().lower()
        role = _ST_ROLE_ALIASES.get(role, role)
        if role not in {"system", "user", "assistant"}:
            role = "system"
        depth = int(section.injection_depth if section.injection_depth is not None else 4)
        order = int(section.injection_order if section.injection_order is not None else 100)
        key = (depth, order, role)
        entry = grouped.setdefault(key, {"texts": [], "source_ids": []})
        entry["texts"].append(section.text.strip())
        entry["source_ids"].append(section.source_module_id)

    role_rank = {"system": 0, "user": 1, "assistant": 2}
    injections: List[PresetCompiledInjection] = []
    for depth, order, role in sorted(grouped, key=lambda item: (item[0], -item[1], role_rank[item[2]])):
        entry = grouped[(depth, order, role)]
        text = "\n".join(item for item in entry["texts"] if item).strip()
        if not text:
            continue
        injections.append(
            PresetCompiledInjection(
                depth=depth,
                order=order,
                role=role,
                text=text,
                sourceModuleIds=entry["source_ids"],
            )
        )
    return injections


def _virtual_modules_from_v1(doc: PresetDocument) -> List[PresetModule]:
    modules: List[PresetModule] = []
    if doc.style.free_text_slot_pre.strip():
        modules.append(
            PresetModule(
                id="v1_free_text_slot_pre",
                title="硬边界",
                slot="boundary",
                priority=100,
                content=doc.style.free_text_slot_pre,
                tags=["v1", "hard"],
                virtual=True,
            )
        )
    ar = doc.style.author_reference
    if ar and (ar.primary or ar.borrow or ar.do_not_borrow or ar.secondary or ar.notes):
        parts = []
        if ar.primary:
            parts.append(f"主参考: {ar.primary}")
        if ar.borrow:
            parts.append("借鉴: " + " / ".join(ar.borrow))
        if ar.do_not_borrow:
            parts.append("剔除: " + " / ".join(ar.do_not_borrow))
        if ar.secondary:
            parts.append("辅参考: " + " / ".join(ar.secondary))
        if ar.notes:
            parts.append(f"备注: {ar.notes}")
        modules.append(
            PresetModule(
                id="v1_author_reference",
                title="参考作家",
                slot="author_reference",
                priority=90,
                content="\n".join(parts),
                tags=["v1", "author"],
                virtual=True,
            )
        )
    mechanics = []
    if doc.style.prose_register.strip():
        mechanics.append(f"整体定位: {doc.style.prose_register.strip()}")
    mechanics.extend(rule.strip() for rule in doc.style.style_rules if rule.strip())
    if mechanics:
        modules.append(
            PresetModule(
                id="v1_language_mechanics",
                title="语言机制",
                slot="language_mechanics",
                priority=80,
                content="\n".join(mechanics),
                tags=["v1", "style"],
                virtual=True,
            )
        )
    negative = []
    if doc.style.forbidden_words:
        negative.append("禁词: " + " / ".join(doc.style.forbidden_words))
    if doc.style.forbidden_patterns:
        negative.append("禁式: " + " / ".join(doc.style.forbidden_patterns))
    if negative:
        modules.append(
            PresetModule(
                id="v1_negative_rules",
                title="禁用规则",
                slot="negative_rules",
                priority=80,
                content="\n".join(negative),
                tags=["v1", "negative"],
                virtual=True,
            )
        )
    standalone = []
    if doc.style.pov:
        narrator = f"（视角主角 {doc.style.narrator}）" if doc.style.narrator else ""
        standalone.append(f"视角: {doc.style.pov} {narrator}".strip())
    if standalone:
        modules.append(
            PresetModule(
                id="v1_pov",
                title="视角约束",
                slot="language_mechanics",
                priority=75,
                content="\n".join(standalone),
                tags=["v1", "pov"],
                virtual=True,
            )
        )
    narrator_id = doc.style.narrator
    if narrator_id and narrator_id in doc.character_voices:
        voice = doc.character_voices[narrator_id]
        voice_lines = []
        if voice.tone:
            voice_lines.append(f"视角主角口吻: {voice.tone}")
        if voice.signature_actions:
            voice_lines.append("标志动作: " + " / ".join(voice.signature_actions))
        if voice.taboo:
            voice_lines.append("禁忌: " + " / ".join(voice.taboo))
        if voice_lines:
            modules.append(
                PresetModule(
                    id="v1_character_voice",
                    title="视角主角口吻",
                    slot="language_mechanics",
                    priority=70,
                    content="\n".join(voice_lines),
                    tags=["v1", "voice"],
                    virtual=True,
                )
            )
    length_contract = doc.length_contract
    if length_contract.body_min_chars and length_contract.body_max_chars:
        modules.append(
            PresetModule(
                id="v1_length_contract",
                title="长度合同",
                slot="boundary",
                priority=60,
                content=(
                    f"正文不少于 {length_contract.body_min_chars} 字，目标 {length_contract.body_target_chars} 字，"
                    f"上限 {length_contract.body_max_chars} 字；段落 {length_contract.paragraph_min}-"
                    f"{length_contract.paragraph_max} 段。不足下限视为未完成。"
                ),
                tags=["v1", "length"],
                virtual=True,
            )
        )
    if doc.terms.term_replace_map:
        repls = " / ".join(f"{key}→{value}" for key, value in doc.terms.term_replace_map.items())
        modules.append(
            PresetModule(
                id="v1_terms",
                title="术语硬替换",
                slot="negative_rules",
                priority=65,
                content=f"术语硬替换: {repls}",
                tags=["v1", "terms"],
                virtual=True,
            )
        )
    if doc.thinking.enabled and doc.thinking.stages:
        modules.append(
            PresetModule(
                id="v1_self_check",
                title="落笔前检查",
                slot="self_check",
                priority=70,
                content="\n".join(f"{index + 1}. {stage}" for index, stage in enumerate(doc.thinking.stages)),
                tags=["v1", "self_check"],
                virtual=True,
            )
        )
    if doc.style.free_text_slot_post.strip():
        modules.append(
            PresetModule(
                id="v1_free_text_slot_post",
                title="底置自由规则",
                slot="advanced",
                priority=10,
                content=doc.style.free_text_slot_post,
                tags=["v1", "advanced"],
                virtual=True,
            )
        )
    return modules


def _validate_modules(modules: Sequence[PresetModule]) -> List[str]:
    warnings: List[str] = []
    seen: Set[str] = set()
    for module in modules:
        if module.id in seen:
            warnings.append(f"duplicate module id ignored by UI risk: {module.id}")
        seen.add(module.id)
        if module.slot not in _SLOT_ORDER:
            warnings.append(f"unknown module slot '{module.slot}' on {module.id}; compiled after fixed slots")
    return warnings


def _module_sort_key(module: PresetModule, *, source_format: str = "") -> tuple[int, int, str]:
    if source_format in _IMPORTED_SOURCE_FORMATS or module.source_format in _IMPORTED_SOURCE_FORMATS:
        order = module.source_order if module.source_order is not None else 1_000_000
        return (0, int(order), module.id)
    return (_SLOT_ORDER.get(module.slot, 999), -module.priority, module.id)


def _section_sort_key(section: PresetCompiledSection, *, source_format: str = "") -> tuple[int, int, str]:
    if source_format in _IMPORTED_SOURCE_FORMATS:
        order = section.source_order if section.source_order is not None else 1_000_000
        return (0, int(order), section.source_module_id)
    return (_SLOT_ORDER.get(section.slot, 999), -section.priority, section.source_module_id)


def _render_sections(sections: Sequence[PresetCompiledSection], *, source_format: str = "") -> str:
    if source_format in _IMPORTED_SOURCE_FORMATS:
        # 绝对注入（injection_position == 1）走 injections 结构化输出，
        # 不再混入相对顺序文本，避免同一段内容出现两份。
        return "\n\n".join(
            section.text.strip()
            for section in sections
            if section.text.strip() and section.injection_position != 1
        ).strip()

    chunks: List[str] = []
    for section in sections:
        chunks.append(
            f"[{section.slot}/{section.source_module_id} | priority {section.priority} | {section.scope}]\n{section.text}"
        )
    return "\n\n".join(chunks).strip()


def _scan_module_text(module: PresetModule, text: str) -> List[PresetRisk]:
    patterns: List[tuple[str, RiskLevel, str, str]] = [
        ("visible_cot", "error", r"(<thinking>|</thinking>|思维链|chain[- ]?of[- ]?thought|展示推理|输出推理过程)", "包含显式思维链或要求展示推理的文本。"),
        ("forced_hook", "warning", r"(末句留.*钩子|未结清的悬念|强行.*承接下节|cliffhanger|悬念.*必须)", "包含强制 hook 或强制悬念规则。"),
        ("auto_darkline", "warning", r"(自动.*暗线|推进.*暗线|异象.*升级|超自然.*铺垫|地底水纹|伞里有东西|有什么东西.*浮上来)", "包含自动暗线、异象升级或谜题化物件风险。"),
    ]
    risks: List[PresetRisk] = []
    for code, level, pattern, message in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            risks.append(
                PresetRisk(
                    level=level,
                    code=code,
                    message=message,
                    sourceModuleId=module.id,
                    line=_line_for_offset(text, match.start()),
                )
            )
    return risks


def _scan_conflicts(modules: Sequence[PresetModule]) -> List[PresetRisk]:
    risks: List[PresetRisk] = []
    for module in modules:
        text = module.content
        if re.search(r"(日常场景.*零心理|少写心理|不要.*心理)", text) and re.search(r"(每[两2三3].*句.*心理|大量.*心理|高频.*心理)", text):
            risks.append(
                PresetRisk(
                    level="warning",
                    code="conflict_rules",
                    message="同一模块同时要求少写心理和高频心理，规则冲突。",
                    sourceModuleId=module.id,
                )
            )
        if re.search(r"(不要|禁止).*暗线", text) and re.search(r"(必须|每章|总要).*暗线", text):
            risks.append(
                PresetRisk(
                    level="warning",
                    code="conflict_rules",
                    message="同一模块同时禁止暗线又强制暗线，规则冲突。",
                    sourceModuleId=module.id,
                )
            )
    return risks


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
