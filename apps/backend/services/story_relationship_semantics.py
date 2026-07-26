from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RelationshipSemantics:
    dimension: str
    relation_type: str
    polarity: str
    strength: float | None
    status: str
    current_level: int


@dataclass(frozen=True)
class RelationshipStatement:
    display_target: str
    stable_target: str
    detail: str
    semantics: RelationshipSemantics


_DIMENSION_TOKENS = (
    ("hostility", ("hostility", "enemy", "hostile", "敌对", "仇", "怨")),
    ("rivalry", ("rivalry", "rival", "竞争", "对手", "较量", "冲突")),
    # 专业关系必须先于泛化的 alliance/“合作”命中。
    (
        "professional",
        (
            "professional",
            "collaboration",
            "mentor",
            "student",
            "colleague",
            "专业",
            "协作",
            "工作关系",
            "师徒",
            "师父",
            "师门",
            "掌柜",
            "上司",
            "下属",
            "同事",
        ),
    ),
    ("alliance", ("alliance", "ally", "partner", "同盟", "盟友", "合作", "结盟", "联手", "伙伴")),
    ("trust", ("trust", "trusted", "信任", "信赖", "不会轻易害人", "托付")),
    ("loyalty", ("loyalty", "loyal", "忠诚", "效忠", "追随")),
    ("intimacy", ("intimacy", "friend", "亲密", "朋友", "友人", "故交", "亲近")),
    (
        "family",
        ("family", "家人", "亲属", "父亲", "母亲", "兄", "弟", "姐", "妹", "妻", "夫", "叔", "姑", "舅", "姨"),
    ),
)


def semantics_for_dimension(dimension: str) -> RelationshipSemantics:
    normalized = str(dimension or "").strip().lower()
    if normalized in {"hostility", "rivalry"}:
        return RelationshipSemantics(normalized, normalized, "negative", 0.8, "asserted", -2)
    if normalized in {"trust", "intimacy", "loyalty", "alliance"}:
        return RelationshipSemantics(normalized, normalized, "positive", 0.8, "asserted", 2)
    if normalized == "professional":
        return RelationshipSemantics(
            normalized,
            "professional_collaboration",
            "neutral",
            0.65,
            "asserted",
            0,
        )
    if normalized == "family":
        return RelationshipSemantics(normalized, "family", "unknown", 0.65, "asserted", 0)
    return RelationshipSemantics("unknown", "unknown", "unknown", None, "unresolved", 0)


def classify_relationship(description: str) -> RelationshipSemantics:
    normalized = _compact_text(description).lower()
    for dimension, tokens in _DIMENSION_TOKENS:
        if any(token in normalized for token in tokens):
            return semantics_for_dimension(dimension)
    return semantics_for_dimension("unknown")


def parse_relationship_markdown(line: str) -> Optional[RelationshipStatement]:
    text = _compact_text(re.sub(r"^[-*+•]\s*", "", str(line or "").strip()))
    text = re.sub(r"^\d+[.)]\s*", "", text).strip("*_ ")
    if not text or text.lower() in {"暂无", "无", "none", "n/a"}:
        return None

    stable_match = re.match(
        r"^(?P<label>.{1,80}?)\s*[（(]\s*(?P<stable>[^（）()]{1,160})\s*[）)]\s*[：:]\s*(?P<detail>.+)$",
        text,
    )
    if stable_match:
        display_target = _clean_target(stable_match.group("label"))
        stable_target = _clean_target(stable_match.group("stable"))
        detail = _compact_text(stable_match.group("detail"))
    else:
        legacy_match = re.match(r"^(?P<label>.{1,80}?)[：:]\s*(?P<detail>.+)$", text)
        if not legacy_match:
            return None
        display_target = _clean_target(legacy_match.group("label"))
        stable_target = ""
        detail = _compact_text(legacy_match.group("detail"))

    if not display_target or not detail:
        return None
    return RelationshipStatement(
        display_target=display_target,
        stable_target=stable_target,
        detail=detail,
        semantics=classify_relationship(detail),
    )


def _clean_target(value: str) -> str:
    target = _compact_text(value).strip("*_ `")
    target = re.sub(r"^(?:与|和|对)\s*", "", target)
    target = re.sub(r"(?:的)?关系$", "", target)
    return target.strip()


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
