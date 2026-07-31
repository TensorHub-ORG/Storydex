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
    (
        "hostility",
        ("hostility", "enemy", "hostile", "敌对", "敌人", "仇敌", "宿敌", "对立", "欺负", "羞辱", "厌恶", "憎恨", "仇怨", "结怨"),
    ),
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
            "同僚",
            "合伙人",
        ),
    ),
    ("alliance", ("alliance", "ally", "partner", "同盟", "盟友", "合作", "结盟", "联手", "伙伴")),
    ("trust", ("trust", "trusted", "信任", "信赖", "托付")),
    ("loyalty", ("loyalty", "loyal", "忠诚", "效忠", "追随")),
    (
        "intimacy",
        ("intimacy", "friend", "亲密", "朋友", "挚友", "友人", "故交", "亲近", "爱人", "恋人", "暗恋", "爱慕", "伴侣"),
    ),
    (
        "family",
        (
            "family", "家人", "亲属", "血亲", "亲生", "家族",
            "父亲", "母亲", "爸爸", "妈妈", "哥哥", "弟弟", "姐姐", "妹妹",
            "兄长", "胞兄", "胞弟", "胞姐", "胞妹", "姐弟", "兄妹",
            "妻子", "丈夫", "夫妻", "叔叔", "姑姑", "舅舅", "姨妈", "姨母",
        ),
    ),
)

_UNCERTAINTY_TOKENS = (
    "可能", "或许", "疑似", "据说", "看似", "待定", "未知", "不确定", "尚不明确", "尚未确认",
    "如果", "假如", "若是", "计划成为", "希望成为", "打算成为",
    "maybe", "possibly", "uncertain", "unconfirmed", "rumored",
)

_NEGATION_TOKENS = (
    "不是", "并非", "没有", "不存在", "从未", "未曾", "未形成", "不构成", "不再是",
    "互不", "否认", "无怨无仇", "not ", "no relationship", "never ", "unrelated", "stranger",
)

_NON_CURRENT_TOKENS = (
    "曾经是", "曾是", "过去是", "此前是", "原同事", "前同事", "前任同事", "已分手", "已经分手",
    "former ", "used to be", "ex-",
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
    dimension_tokens = (
        token
        for _dimension, tokens in _DIMENSION_TOKENS
        for token in tokens
    )
    if any(token in normalized for token in (*_UNCERTAINTY_TOKENS, *_NON_CURRENT_TOKENS)):
        return semantics_for_dimension("unknown")
    if any(token in normalized for token in _NEGATION_TOKENS) and any(
        token in normalized for token in dimension_tokens
    ):
        return semantics_for_dimension("unknown")
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
