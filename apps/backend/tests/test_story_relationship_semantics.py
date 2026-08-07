from services.story_relationship_semantics import (
    classify_relationship,
    parse_relationship_markdown,
)


def test_real_chy_relationship_statement_keeps_stable_target_and_semantics():
    statement = parse_relationship_markdown(
        "- **苏晚**（char:suwan）：互相信任的盟友。"
    )

    assert statement is not None
    assert statement.display_target == "苏晚"
    assert statement.stable_target == "char:suwan"
    assert statement.detail == "互相信任的盟友。"
    assert statement.semantics.relation_type in {"trust", "alliance"}
    assert statement.semantics.polarity == "positive"
    assert statement.semantics.status == "asserted"


def test_relationship_semantics_keep_professional_and_unknown_distinct():
    professional = classify_relationship("两人保持专业合作与同事关系")
    unresolved = classify_relationship("两人之间存在难以说明的联系")

    assert professional.relation_type == "professional_collaboration"
    assert professional.polarity == "neutral"
    assert professional.strength == 0.65
    assert unresolved.relation_type == "unknown"
    assert unresolved.polarity == "unknown"
    assert unresolved.strength is None
    assert unresolved.status == "unresolved"


def test_uncertain_relationship_claim_is_not_promoted_even_with_known_keyword():
    uncertain = classify_relationship("据说两人可能是朋友，尚未确认")
    romantic = classify_relationship("她一直暗恋对方")

    assert uncertain.relation_type == "unknown"
    assert uncertain.status == "unresolved"
    assert romantic.relation_type == "intimacy"
    assert romantic.status == "asserted"


def test_negated_or_ambiguous_relationship_text_is_not_promoted():
    for description in (
        "两人不是朋友，只是互不认识的陌生人",
        "她并非他的敌人",
        "他们不再是同事",
        "双方没有合作，也不存在信任",
        "二人无怨无仇",
        "两人曾经是朋友",
        "他们计划成为合作伙伴",
    ):
        semantics = classify_relationship(description)
        assert semantics.relation_type == "unknown"
        assert semantics.status == "unresolved"

    assert classify_relationship("共同练习功夫").relation_type == "unknown"


def test_relationship_polarity_is_scoped_and_prefers_the_current_contrast_clause():
    semantics = classify_relationship("两人没有血缘关系，但始终彼此信任，是可靠的伙伴")

    assert semantics.relation_type == "trust"
    assert semantics.status == "asserted"

    former = classify_relationship("两人曾经是朋友，但后来成为宿敌")
    assert former.relation_type == "hostility"
    assert former.status == "asserted"

    corrected = classify_relationship("两人并非敌人，而是彼此信任的盟友")
    assert corrected.relation_type == "trust"
    assert corrected.status == "asserted"
