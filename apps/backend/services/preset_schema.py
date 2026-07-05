"""Storydex 结构化预设 schema · WP-阶段 B + v1.3 边界整改。

把"自由 markdown 预设"升级为"sidecar 双文件预设"：

* 主文件 ``<stem>.md`` — 人读 + 模型阅读
* 副文件 ``<stem>.preset.json`` — 结构化参数

只有 ``.md`` 时退化为 legacy free-markdown；同 stem 时 JSON 字段优先。

PresetDocument 承载：
  * sampling — 采样参数（全局默认 + per-purpose 覆盖）
  * length_contract — 长度合同（min/target/max chars、required_tags）
  * thinking — system 端阶段化清单（只允许 mode="stage_list"，
    visible_in_output 强制 False）
  * style — POV、禁词表、禁式正则、风格规则、prose_register、
    author_reference、free_text_slot_pre/post
  * memory — 摘要协议
  * terms — 名归一化 + 术语硬替换（system 提示，不做后处理正则）
  * character_voices — 角色口吻指纹（运行时作为角色卡 overlay）

v1.3 整改 (Sprint #008)：
  * 所有 BaseModel 改用 ``extra="allow"``，让预设可以加任意扩展字段
    而不被 Pydantic 静默吞，模型可见 (字段级 fallback render)。
  * StyleSection 新增 prose_register / author_reference /
    free_text_slot_pre / free_text_slot_post 四个结构化槽位。
  * 新增 AuthorReference BaseModel 承载参考作家。

校验策略：fail-soft。单字段越界丢弃 + warning，整 doc 不失败。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


def to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])


_EXTRA_ALLOW = ConfigDict(extra="allow", alias_generator=to_camel, populate_by_name=True)


# -- 采样参数 ---------------------------------------------------------------


class SamplingParams(BaseModel):
    """单次 LLM 调用的采样参数。全部 Optional，缺省由 resolver 回落。"""

    model_config = _EXTRA_ALLOW

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    seed: Optional[int] = None
    stop: Optional[List[str]] = None

    @field_validator("temperature")
    @classmethod
    def _check_temperature(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if v < 0.0 or v > 2.0:
            return None
        return v

    @field_validator("top_p")
    @classmethod
    def _check_top_p(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if v < 0.0 or v > 1.0:
            return None
        return v

    @field_validator("top_k")
    @classmethod
    def _check_top_k(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if v < 1 or v > 1024:
            return None
        return v

    @field_validator("frequency_penalty", "presence_penalty")
    @classmethod
    def _check_penalty(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if v < -2.0 or v > 2.0:
            return None
        return v


class SamplingSection(BaseModel):
    model_config = _EXTRA_ALLOW
    default: SamplingParams = Field(default_factory=SamplingParams)
    per_purpose: Dict[str, SamplingParams] = Field(default_factory=dict)


# -- 长度合同 ---------------------------------------------------------------


class LengthContract(BaseModel):
    model_config = _EXTRA_ALLOW
    body_min_chars: int = 1200
    body_target_chars: int = 2400
    body_max_chars: int = 3600
    paragraph_min: int = 6
    paragraph_max: int = 24
    required_tags: List[str] = Field(default_factory=list)
    forbidden_tags: List[str] = Field(default_factory=list)


# -- 思维链阶段化清单 ------------------------------------------------------


class ThinkingSection(BaseModel):
    model_config = _EXTRA_ALLOW
    enabled: bool = False
    mode: Literal["stage_list"] = "stage_list"
    stages: List[str] = Field(default_factory=list)
    inject_position: Literal["system_suffix", "user_suffix"] = "system_suffix"
    visible_in_output: bool = False

    @field_validator("visible_in_output")
    @classmethod
    def _force_invisible(cls, v: bool) -> bool:
        return False


# -- 参考作家（v1.3 新增） --------------------------------------------------


class AuthorReference(BaseModel):
    """参考作家信息——告诉模型借鉴谁、剔除谁、辅参考谁。"""

    model_config = _EXTRA_ALLOW
    primary: str = ""
    borrow: List[str] = Field(default_factory=list)
    do_not_borrow: List[str] = Field(default_factory=list)
    secondary: List[str] = Field(default_factory=list)
    notes: str = ""


# -- 文风 ------------------------------------------------------------------


class StyleSection(BaseModel):
    """文风规则集合。

    v1.3 新增四槽位：
      - prose_register: 一句话整体定位（如"江南雨巷古风修仙，节奏耐心铺陈"）
      - author_reference: 参考作家结构化字段
      - free_text_slot_pre: 自由文本顶置槽 - 插入到硬约束块顶部
      - free_text_slot_post: 自由文本底置槽 - 插入到硬约束块底部
    """

    model_config = _EXTRA_ALLOW
    pov: str = ""
    narrator: str = ""
    forbidden_words: List[str] = Field(default_factory=list)
    forbidden_patterns: List[str] = Field(default_factory=list)
    style_rules: List[str] = Field(default_factory=list)
    max_consecutive_repeat: int = 2
    prose_register: str = ""
    author_reference: Optional[AuthorReference] = None
    free_text_slot_pre: str = ""
    free_text_slot_post: str = ""


# -- 记忆 ------------------------------------------------------------------


class MemorySection(BaseModel):
    model_config = _EXTRA_ALLOW
    summary_format: str = "scene_outline"
    summary_min_chars: int = 240
    summary_max_chars: int = 600
    big_summary_trigger_chapters: int = 8


# -- 术语 ------------------------------------------------------------------


class TermsSection(BaseModel):
    model_config = _EXTRA_ALLOW
    name_alias_map: Dict[str, str] = Field(default_factory=dict)
    term_replace_map: Dict[str, str] = Field(default_factory=dict)
    enforce_at_generation: bool = True


# -- 角色口吻（运行时作为 character card overlay） ------------------------


class CharacterVoice(BaseModel):
    """角色口吻指纹。

    v1.3 边界整改：character card 是 baseline；预设里的 character_voices
    仅作为"作品级 overlay"——同一角色在不同作品里可有不同声纹。运行时
    overlay 字段覆盖 baseline，未设置的字段沿用 baseline。
    """

    model_config = _EXTRA_ALLOW
    tone: str = ""
    signature_actions: List[str] = Field(default_factory=list)
    taboo: List[str] = Field(default_factory=list)


# -- 元信息 ----------------------------------------------------------------


class PresetMeta(BaseModel):
    model_config = _EXTRA_ALLOW
    name: str = ""
    description: str = ""
    compatible_providers: List[str] = Field(default_factory=list)
    updated_at: str = ""
    # 导入元数据（SillyTavern 适配）
    source_format: Optional[str] = None  # "sillytavern" | "storydex" | "generic"
    display_regexes: List[Dict[str, Any]] = Field(default_factory=list)  # markdownOnly 正则元数据
    chat_squash_meta: Dict[str, Any] = Field(default_factory=dict)  # SPreset ChatSquash 元数据
    import_warnings: List[str] = Field(default_factory=list)  # 宏清理等格式适配警告


# -- 顶层 Document ---------------------------------------------------------


class PresetDocument(BaseModel):
    model_config = _EXTRA_ALLOW
    version: int = 1
    meta: PresetMeta = Field(default_factory=PresetMeta)
    sampling: SamplingSection = Field(default_factory=SamplingSection)
    length_contract: LengthContract = Field(default_factory=LengthContract)
    thinking: ThinkingSection = Field(default_factory=ThinkingSection)
    style: StyleSection = Field(default_factory=StyleSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    terms: TermsSection = Field(default_factory=TermsSection)
    character_voices: Dict[str, CharacterVoice] = Field(default_factory=dict)


# -- 加载器 ----------------------------------------------------------------


def load_preset_sidecar(path: Path) -> Tuple[PresetDocument, List[str]]:
    """从 ``<stem>.preset.json`` 加载并校验。

    fail-soft：解析失败返回 (空 PresetDocument, warnings)；
    单字段越界已在 field_validator 里丢成 None，整 doc 仍有效。
    """
    warnings: List[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PresetDocument(), [f"preset sidecar not found: {path}"]
    except OSError as exc:
        return PresetDocument(), [f"preset sidecar read error: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"preset sidecar JSON parse error: {exc}")
        return PresetDocument(), warnings

    if not isinstance(data, dict):
        warnings.append("preset sidecar must be a JSON object")
        return PresetDocument(), warnings

    try:
        doc = PresetDocument.model_validate(data)
    except ValidationError as exc:
        warnings.append(f"preset sidecar validation error: {exc.errors()}")
        return PresetDocument(), warnings

    if data.get("thinking", {}).get("visible_in_output") is True:
        warnings.append("thinking.visible_in_output forced to False")

    return doc, warnings


def find_sidecar_path(md_path: Path) -> Path:
    """对应同 stem 的 ``.preset.json`` 路径（不要求存在）。"""
    return md_path.with_name(md_path.stem + ".preset.json")


def write_preset_sidecar(md_path: Path, doc: PresetDocument) -> None:
    """原子写 sidecar：先写 ``.tmp`` 再 rename。"""
    sidecar = find_sidecar_path(md_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    payload = doc.model_dump(mode="json")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(sidecar)


def summarize_preset_sidecar(doc: PresetDocument, max_chars: int = 900) -> str:
    """把 sidecar 压缩成短文本，用于 _build_project_preset_skill 注入。"""
    lines: List[str] = []
    if doc.meta.name:
        lines.append(f"Preset: {doc.meta.name}")
    if doc.style.prose_register:
        lines.append(f"定位: {doc.style.prose_register[:200]}")
    if doc.style.pov:
        lines.append(f"POV: {doc.style.pov}（{doc.style.narrator}）" if doc.style.narrator else f"POV: {doc.style.pov}")
    lc = doc.length_contract
    lines.append(
        f"长度合同: {lc.body_min_chars}-{lc.body_max_chars} 字 / 段落 {lc.paragraph_min}-{lc.paragraph_max}"
    )
    if doc.style.forbidden_words:
        head = "、".join(doc.style.forbidden_words[:8])
        lines.append(f"禁词: {head}" + ("…" if len(doc.style.forbidden_words) > 8 else ""))
    if doc.style.style_rules:
        lines.append("风格规则:")
        for rule in doc.style.style_rules[:6]:
            lines.append(f"  - {rule}")
    if doc.terms.term_replace_map:
        repls = "; ".join(f"{k}→{v}" for k, v in list(doc.terms.term_replace_map.items())[:6])
        lines.append(f"术语硬替换: {repls}")
    if doc.terms.name_alias_map:
        aliases = "; ".join(f"{k}→{v}" for k, v in list(doc.terms.name_alias_map.items())[:6])
        lines.append(f"名字归一化: {aliases}")
    if doc.thinking.enabled and doc.thinking.stages:
        lines.append("阶段化清单:")
        for i, stage in enumerate(doc.thinking.stages, 1):
            lines.append(f"  {i}. {stage}")
    try:
        from services.preset_compiler import modules_from_document

        modules = modules_from_document(doc)
    except Exception:
        modules = []
    if modules:
        lines.append("模块:")
        for module in modules[:8]:
            if not module.enabled_by_default:
                continue
            content = " ".join((module.content or "").split())
            if len(content) > 180:
                content = content[:179] + "…"
            lines.append(f"  - {module.title or module.id} [{module.slot}]: {content}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def render_thinking_block(doc: PresetDocument) -> str:
    """渲染阶段化清单作为 system_suffix 注入。"""
    if not doc.thinking.enabled or not doc.thinking.stages:
        return ""
    lines = ["[Stage-list before writing (do not echo this list in the output)]"]
    for i, stage in enumerate(doc.thinking.stages, 1):
        lines.append(f"  {i}. {stage}")
    return "\n".join(lines)
