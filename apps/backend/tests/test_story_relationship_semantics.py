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
