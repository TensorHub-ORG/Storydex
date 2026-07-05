"""WP-5.2 · 角色 schema 拆静态/动态（07 §5.6）。

旧 ``.storydex/characters/<id>.json`` 把人设卡 + 当前状态混在一起；
v2 拆成两份：
  * ``CharacterCard`` — 静态人设（不常变）：id / name / aliases / role /
    appearance / personality / background / motivation / stable_relationships
  * ``CharacterState`` — 动态状态：current_location / physical_condition /
    emotional_state / current_objective / known_secrets / last_seen_in /
    recent_changes

对应文件：
  * ``.storydex/characters/cards/<id>.json``
  * ``.storydex/characters/states/<id>.json``

旧文件保留；reader 兼容旧 schema 自动拆字段。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])


_EXTRA_ALLOW = ConfigDict(extra="allow", alias_generator=to_camel, populate_by_name=True)


class StableRelationship(BaseModel):
    model_config = _EXTRA_ALLOW
    target_id: str
    relation_type: str = ""
    note: str = ""


class ProseVoice(BaseModel):
    """v1.3 (Sprint #008 边界整改)：人物级口吻指纹的 baseline 字段。

    放在 CharacterCard 里作为 baseline；预设的 character_voices 仅作
    作品级 overlay 覆盖。同一角色在不同作品里可有不同声纹，但默认
    口吻在角色卡里一次定义、跨作品复用。
    """

    model_config = _EXTRA_ALLOW
    tone: str = ""
    signature_actions: List[str] = Field(default_factory=list)
    taboo: List[str] = Field(default_factory=list)


class CharacterCard(BaseModel):
    model_config = _EXTRA_ALLOW
    id: str
    schema_version: str = "2.0"
    name: str
    aliases: List[str] = Field(default_factory=list)
    role: str = ""
    appearance: str = ""
    personality: str = ""
    background: str = ""
    motivation: str = ""
    stable_relationships: List[Dict[str, Any]] = Field(default_factory=list)
    prose_voice: Optional[ProseVoice] = None


class RecentChange(BaseModel):
    model_config = _EXTRA_ALLOW
    chapter_id: str = ""
    description: str = ""
    happened_at: str = ""


class CharacterState(BaseModel):
    model_config = _EXTRA_ALLOW
    id: str
    schema_version: str = "2.0"
    current_location: str = ""
    physical_condition: str = ""
    emotional_state: str = ""
    current_objective: str = ""
    known_secrets: List[str] = Field(default_factory=list)
    last_seen_in: Optional[str] = None
    recent_changes: List[Dict[str, Any]] = Field(default_factory=list)


_STATE_FIELDS = {"current_location", "physical_condition", "emotional_state",
                 "current_objective", "known_secrets", "last_seen_in", "recent_changes"}
_CARD_FIELDS = {"name", "aliases", "role", "appearance", "personality",
                "background", "motivation", "stable_relationships", "prose_voice"}


def split_legacy_character(legacy: Dict[str, Any]) -> Dict[str, Any]:
    """把旧扁平 character.json 拆成 {card: ..., state: ...}。"""
    cid = str(legacy.get("id") or legacy.get("name") or "unknown")
    card_data = {"id": cid, "name": str(legacy.get("name") or cid)}
    state_data = {"id": cid}
    for key, value in legacy.items():
        if key in ("id", "schema_version"):
            continue
        if key in _CARD_FIELDS:
            card_data[key] = value
        elif key in _STATE_FIELDS:
            state_data[key] = value
        # 其它未知字段默认放 card（保持向后兼容）
        else:
            card_data.setdefault(key, value)
    return {"card": CharacterCard(**card_data).model_dump(), "state": CharacterState(**state_data).model_dump()}


_ENCODING_SELFTEST = "CharacterCard / CharacterState 编码自检"
assert "�" not in _ENCODING_SELFTEST
