from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.story_knowledge_relation_service import (
    KnowledgeRelationError,
    StoryKnowledgeRelationService,
)
from services.story_project_service import StoryProjectService
from services.storydex_agent_tools import StorydexApplyKnowledgeUpdateTool
from services.story_wiki_service import StoryWikiService


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _entity(
    service: StoryKnowledgeRelationService,
    root: Path,
    name: str,
    relative_path: str,
    *,
    kind: str = "setting",
) -> dict:
    path = root / relative_path
    if path.suffix.lower() == ".json":
        _write(path, json.dumps({"name": name}, ensure_ascii=False))
    else:
        _write(path, f"# {name}\n")
    return service.ensure_entity(root, name, source_path=relative_path, kind=kind)


def _stub_projection(monkeypatch: pytest.MonkeyPatch, service: StoryKnowledgeRelationService) -> None:
    def projection(root: Path) -> dict:
        facts = service.load_facts(root).get("facts", [])
        edges = [
            service.graph_edge_from_relation(fact)
            for fact in facts
            if isinstance(fact, dict)
        ]
        return {
            "status": "ready",
            "knowledgeRevision": 1,
            "builtFromRevision": 1,
            "entries": [],
            "graph": {"nodes": [], "edges": edges},
        }

    monkeypatch.setattr(
        service,
        "_rebuild_projection",
        projection,
    )


def test_markdown_parser_accepts_both_colons_and_wikilink_alias() -> None:
    service = StoryKnowledgeRelationService()
    parsed = service.parse_markdown_relations(
        "# Tidebeast\n\n"
        "## 关联对象\n\n"
        "- 栖息于：[[Nightstar|Night Harbor]]\n"
        "- 服务于: [[Voyager Guild]]\n\n"
        "## Notes\n"
        "- ignored: [[Outside]]\n"
    )

    assert [(item["predicate"], item["target"]) for item in parsed] == [
        ("栖息于", "Nightstar"),
        ("服务于", "Voyager Guild"),
    ]
    assert parsed[0]["display_target"] == "Night Harbor"
    assert parsed[0]["line_start"] == parsed[0]["line_end"]


def test_prepare_requires_next_trace_and_apply_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StoryKnowledgeRelationService()
    _stub_projection(monkeypatch, service)
    subject = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    before = (tmp_path / ".storydex/worldbook/Tidebeast.md").read_text(encoding="utf-8")

    prepared = service.prepare_explicit(
        tmp_path,
        [{"subjectId": subject["entityId"], "predicate": "lives_on", "objectId": obj["entityId"]}],
        session_id="session-1",
        trace_id="trace-prepare",
        provider_id="test-provider",
        model="test-model",
    )
    assert (tmp_path / ".storydex/worldbook/Tidebeast.md").read_text(encoding="utf-8") == before
    assert service.load_facts(tmp_path)["facts"] == []

    with pytest.raises(KnowledgeRelationError) as same_trace:
        service.apply_explicit(
            tmp_path,
            prepared["planId"],
            session_id="session-1",
            trace_id="trace-prepare",
            expected_fingerprint=prepared["fingerprint"],
        )
    assert same_trace.value.code == "knowledge_plan_same_trace"

    applied = service.apply_explicit(
        tmp_path,
        prepared["planId"],
        session_id="session-1",
        trace_id="trace-confirm",
        expected_fingerprint=prepared["fingerprint"],
    )
    assert applied["relationCount"] == 1
    markdown = (tmp_path / ".storydex/worldbook/Tidebeast.md").read_text(encoding="utf-8")
    assert markdown.count("- lives_on：[[Nightstar]]") == 1
    facts = service.load_facts(tmp_path)["facts"]
    assert len(facts) == 1
    assert facts[0]["subjectId"] == subject["entityId"]
    assert facts[0]["objectId"] == obj["entityId"]
    assert facts[0]["reviewStatus"] == "confirmed"

    with pytest.raises(KnowledgeRelationError) as applied_twice:
        service.apply_explicit(
            tmp_path,
            prepared["planId"],
            session_id="session-1",
            trace_id="trace-confirm-2",
            expected_fingerprint=prepared["fingerprint"],
        )
    assert applied_twice.value.code == "knowledge_plan_already_applied"


def test_prepare_dedupes_exact_relations_but_preserves_direction(tmp_path: Path) -> None:
    service = StoryKnowledgeRelationService()
    tidebeast = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    nightstar = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")

    forward = {
        "subjectId": tidebeast["entityId"],
        "predicate": "linked_to",
        "objectId": nightstar["entityId"],
    }
    reverse = {
        "subjectId": nightstar["entityId"],
        "predicate": "linked_to",
        "objectId": tidebeast["entityId"],
    }
    prepared = service.prepare_explicit(
        tmp_path,
        [forward, dict(forward), reverse],
        session_id="directional-dedupe",
        trace_id="directional-dedupe-prepare",
    )

    assert prepared["relationCount"] == 2
    assert {
        (relation["subjectId"], relation["objectId"])
        for relation in prepared["relations"]
    } == {
        (tidebeast["entityId"], nightstar["entityId"]),
        (nightstar["entityId"], tidebeast["entityId"]),
    }
    assert len({relation["relationKey"] for relation in prepared["relations"]}) == 2


def test_explicit_plan_rejects_wrong_session_expiry_and_tampering(tmp_path: Path) -> None:
    service = StoryKnowledgeRelationService()
    subject = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    relation = {
        "subjectId": subject["entityId"],
        "predicate": "lives_on",
        "objectId": obj["entityId"],
    }

    wrong_session = service.prepare_explicit(
        tmp_path,
        [relation],
        session_id="session-owner",
        trace_id="trace-owner",
    )
    with pytest.raises(KnowledgeRelationError) as mismatch:
        service.apply_explicit(
            tmp_path,
            wrong_session["planId"],
            session_id="session-other",
            trace_id="trace-confirm",
            expected_fingerprint=wrong_session["fingerprint"],
        )
    assert mismatch.value.code == "knowledge_plan_session_mismatch"

    expired = service.prepare_explicit(
        tmp_path,
        [relation],
        session_id="session-expired",
        trace_id="trace-expired",
    )
    expired_path = service.plans_root(tmp_path) / f"{expired['planId']}.json"
    expired_plan = json.loads(expired_path.read_text(encoding="utf-8"))
    expired_plan["expiresAt"] = "2000-01-01T00:00:00+00:00"
    expired_plan["fingerprint"] = service._plan_fingerprint(expired_plan)
    expired_path.write_text(json.dumps(expired_plan), encoding="utf-8")
    with pytest.raises(KnowledgeRelationError) as expired_error:
        service.apply_explicit(
            tmp_path,
            expired["planId"],
            session_id="session-expired",
            trace_id="trace-confirm",
            expected_fingerprint=expired_plan["fingerprint"],
        )
    assert expired_error.value.code == "knowledge_plan_expired"

    tampered = service.prepare_explicit(
        tmp_path,
        [relation],
        session_id="session-tampered",
        trace_id="trace-tampered",
    )
    tampered_path = service.plans_root(tmp_path) / f"{tampered['planId']}.json"
    tampered_plan = json.loads(tampered_path.read_text(encoding="utf-8"))
    tampered_plan["relations"][0]["predicate"] = "serves"
    tampered_path.write_text(json.dumps(tampered_plan), encoding="utf-8")
    with pytest.raises(KnowledgeRelationError) as tampered_error:
        service.apply_explicit(
            tmp_path,
            tampered["planId"],
            session_id="session-tampered",
            trace_id="trace-confirm",
            expected_fingerprint=tampered["fingerprint"],
        )
    assert tampered_error.value.code == "knowledge_plan_stale"


def test_prepare_normalizes_human_names_in_id_fields_and_object_path_hint(tmp_path: Path) -> None:
    service = StoryKnowledgeRelationService()
    subject_path = ".storydex/worldbook/Tidebeast.md"
    object_path = ".storydex/worldbook/Nightstar.md"
    _write(tmp_path / subject_path, "# Tidebeast\n")
    _write(tmp_path / object_path, "# Nightstar\n")

    prepared = service.prepare_explicit(
        tmp_path,
        [
            {
                # This mirrors a model that redundantly copies display names
                # into the optional ID fields and uses targetSourcePath for
                # the object's source file.
                "subject": "Tidebeast",
                "subjectId": "Tidebeast",
                "subjectSourcePath": subject_path,
                "predicate": "lives_on",
                "object": "Nightstar",
                "objectId": "Nightstar",
                "targetSourcePath": object_path,
            }
        ],
        session_id="normalize",
        trace_id="normalize-prepare",
    )

    relation = prepared["relations"][0]
    assert relation["subject"] == "Tidebeast"
    assert relation["object"] == "Nightstar"
    assert relation["targetSourcePath"] == subject_path
    assert len(service.load_entities(tmp_path)["entities"]) == 2


def test_prepare_keeps_distinct_unknown_ids_fail_closed(tmp_path: Path) -> None:
    service = StoryKnowledgeRelationService()
    _write(tmp_path / ".storydex/worldbook/Tidebeast.md", "# Tidebeast\n")
    _write(tmp_path / ".storydex/worldbook/Nightstar.md", "# Nightstar\n")

    with pytest.raises(KnowledgeRelationError) as exc:
        service.prepare_explicit(
            tmp_path,
            [
                {
                    "subject": "Tidebeast",
                    "subjectId": "invented-stable-id",
                    "subjectSourcePath": ".storydex/worldbook/Tidebeast.md",
                    "predicate": "lives_on",
                    "object": "Nightstar",
                    "objectSourcePath": ".storydex/worldbook/Nightstar.md",
                }
            ],
            session_id="strict",
            trace_id="strict-prepare",
        )
    assert exc.value.code == "knowledge_entity_not_found"


def test_agent_tool_enforces_turn_operation_and_server_identity(tmp_path: Path) -> None:
    service = StoryKnowledgeRelationService()
    subject = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    first_contract = {
        "sessionId": "session-knowledge",
        "traceId": "trace-prepare",
        "providerId": "test-provider",
        "model": "test-model",
        "knowledgeWritePolicy": {
            "mode": "explicit_binding",
            "confirmationRequired": True,
            "confirmed": False,
        },
    }
    first_tool = StorydexApplyKnowledgeUpdateTool(
        workspace_root=tmp_path,
        turn_contract=first_contract,
    )

    forged = first_tool.run(
        {
            "operation": "prepare_explicit",
            "providerId": "forged-provider",
            "relations": [
                {
                    "subjectId": subject["entityId"],
                    "predicate": "栖息于",
                    "objectId": obj["entityId"],
                }
            ],
        }
    )
    assert forged.success is False
    assert json.loads(forged.output)["code"] == "knowledge_contract_mismatch"

    prepared_result = first_tool.run(
        {
            "operation": "prepare_explicit",
            "relations": [
                {
                    "subjectId": subject["entityId"],
                    "predicate": "栖息于",
                    "objectId": obj["entityId"],
                }
            ],
        }
    )
    assert prepared_result.success is True
    prepared = json.loads(prepared_result.output)
    plan = json.loads(
        (service.plans_root(tmp_path) / f"{prepared['planId']}.json").read_text(encoding="utf-8")
    )
    assert plan["sessionId"] == "session-knowledge"
    assert plan["traceId"] == "trace-prepare"
    assert plan["providerId"] == "test-provider"
    assert plan["model"] == "test-model"

    early_apply = first_tool.run(
        {
            "operation": "apply_explicit",
            "planId": prepared["planId"],
            "expectedFingerprint": prepared["fingerprint"],
        }
    )
    assert early_apply.success is False
    assert json.loads(early_apply.output)["code"] == "knowledge_operation_not_allowed"

    confirmed_tool = StorydexApplyKnowledgeUpdateTool(
        workspace_root=tmp_path,
        turn_contract={
            **first_contract,
            "traceId": "trace-confirm",
            "knowledgeWritePolicy": {
                "mode": "explicit_binding",
                "confirmationRequired": True,
                "confirmed": True,
            },
        },
    )
    applied_result = confirmed_tool.run(
        {
            "operation": "apply_explicit",
            "planId": prepared["planId"],
            "expectedFingerprint": prepared["fingerprint"],
        }
    )
    assert applied_result.success is True
    fact = service.load_facts(tmp_path)["facts"][0]
    assert fact["provenance"]["providerId"] == "test-provider"
    assert fact["provenance"]["model"] == "test-model"
    assert fact["traceId"] == "trace-confirm"


def test_json_character_uses_relation_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StoryKnowledgeRelationService()
    _stub_projection(monkeypatch, service)
    subject = _entity(
        service,
        tmp_path,
        "Alice",
        ".storydex/characters/Alice.json",
        kind="character",
    )
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    prepared = service.prepare_explicit(
        tmp_path,
        [{"subjectId": subject["entityId"], "predicate": "serves", "objectId": obj["entityId"]}],
        session_id="sidecar",
        trace_id="prepare-sidecar",
    )
    assert prepared["relations"][0]["usesSidecar"] is True
    assert prepared["relations"][0]["targetSourcePath"].endswith("Alice.relations.md")

    service.apply_explicit(
        tmp_path,
        prepared["planId"],
        session_id="sidecar",
        trace_id="confirm-sidecar",
        expected_fingerprint=prepared["fingerprint"],
    )
    sidecar = tmp_path / ".storydex/characters/Alice.relations.md"
    content = sidecar.read_text(encoding="utf-8")
    assert f"entityId: {subject['entityId']}" in content
    assert "- serves：[[Nightstar]]" in content
    assert len(service.scan_formal_markdown_relations(tmp_path)) == 1


def test_candidate_submission_is_review_only_and_filters_unsupported_evidence(
    tmp_path: Path,
) -> None:
    service = StoryKnowledgeRelationService()
    subject = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    rumor_obj = _entity(service, tmp_path, "Farstar", ".storydex/worldbook/Farstar.md")
    chapter_path = "chapters/001.md"
    explicit = "Tidebeast lives on Nightstar coast."
    negated = "Tidebeast does not live on Nightstar."
    hypothetical = "If Tidebeast moved to Nightstar, it could survive."
    rumor = "Rumor says Tidebeast came from Farstar."
    _write(tmp_path / chapter_path, "\n".join((explicit, negated, hypothetical, rumor)) + "\n")

    def candidate(object_id: str, predicate: str, quote: str) -> dict:
        return {
            "subjectId": subject["entityId"],
            "predicate": predicate,
            "objectId": object_id,
            "sourceRefs": [{"path": chapter_path, "quote": quote, "role": "chapter"}],
        }

    result = service.submit_candidates(
        tmp_path,
        [
            candidate(obj["entityId"], "lives_on", explicit),
            candidate(obj["entityId"], "avoids", negated),
            candidate(obj["entityId"], "might_live_on", hypothetical),
            candidate(rumor_obj["entityId"], "came_from", rumor),
            {
                "subject": "Unknown creature",
                "predicate": "lives_on",
                "objectId": obj["entityId"],
                "sourceRefs": [{"path": chapter_path, "quote": explicit}],
            },
        ],
        trace_id="trace-extract",
        provider_id="test-provider",
        model="test-model",
        extractor_version="test-extractor-v1",
    )

    assert result["acceptedCount"] == 2
    assert {item["reason"] for item in result["skipped"]} >= {
        "negated_evidence",
        "hypothetical_evidence",
        "knowledge_entity_not_found",
    }
    assert service.load_facts(tmp_path)["facts"] == []
    assert "## 关联对象" not in (tmp_path / ".storydex/worldbook/Tidebeast.md").read_text(encoding="utf-8")
    accepted = result["candidates"]
    assert accepted[0]["reviewStatus"] == "review_required"
    assert accepted[0]["provenance"]["providerId"] == "test-provider"
    assert next(item for item in accepted if item["predicate"] == "came_from")["knowledgeStatus"] == "inferred"

    repeated = service.submit_candidates(
        tmp_path,
        [candidate(obj["entityId"], "lives_on", explicit)],
        trace_id="another-trace",
        provider_id="another-provider",
        model="another-model",
    )
    assert repeated["acceptedCount"] == 0
    assert repeated["skipped"][0]["reason"] == "unchanged_candidate"


def test_changed_evidence_supersedes_the_active_candidate(tmp_path: Path) -> None:
    service = StoryKnowledgeRelationService()
    subject = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    chapter_path = "chapters/001.md"
    first_quote = "Tidebeast lives on Nightstar coast."
    second_quote = "Tidebeast permanently lives on Nightstar coast."
    _write(tmp_path / chapter_path, first_quote + "\n")

    def submit(quote: str) -> dict:
        return service.submit_candidates(
            tmp_path,
            [
                {
                    "subjectId": subject["entityId"],
                    "predicate": "lives_on",
                    "objectId": obj["entityId"],
                    "sourceRefs": [{"path": chapter_path, "quote": quote}],
                }
            ],
        )

    first = submit(first_quote)["candidates"][0]
    _write(tmp_path / chapter_path, second_quote + "\n")
    second = submit(second_quote)["candidates"][0]

    ledger = service.load_review_ledger(tmp_path)["relations"]
    superseded = next(item for item in ledger if item["id"] == first["id"])
    current = next(item for item in ledger if item["id"] == second["id"])
    assert superseded["reviewStatus"] == "superseded"
    assert superseded["supersededBy"] == current["id"]
    assert current["reviewStatus"] == "review_required"


def test_candidate_confirm_reject_and_stale_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StoryKnowledgeRelationService()
    _stub_projection(monkeypatch, service)
    subject = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    other = _entity(service, tmp_path, "Farstar", ".storydex/worldbook/Farstar.md")
    chapter_path = "chapters/001.md"
    first_quote = "Tidebeast lives on Nightstar coast."
    second_quote = "Tidebeast once visited Farstar."
    _write(tmp_path / chapter_path, f"{first_quote}\n{second_quote}\n")
    submitted = service.submit_candidates(
        tmp_path,
        [
            {
                "subjectId": subject["entityId"],
                "predicate": "lives_on",
                "objectId": obj["entityId"],
                "sourceRefs": [{"path": chapter_path, "quote": first_quote}],
            },
            {
                "subjectId": subject["entityId"],
                "predicate": "visited",
                "objectId": other["entityId"],
                "sourceRefs": [{"path": chapter_path, "quote": second_quote}],
            },
        ],
    )
    first, second = submitted["candidates"]

    with pytest.raises(KnowledgeRelationError) as stale:
        service.confirm_candidate(
            tmp_path,
            first["id"],
            expected_fingerprint="stale",
            trace_id="confirm-stale",
        )
    assert stale.value.code == "knowledge_candidate_stale"
    assert stale.value.status_code == 409

    confirmed = service.confirm_candidate(
        tmp_path,
        first["id"],
        expected_fingerprint=first["fingerprint"],
        trace_id="confirm-current",
    )
    assert confirmed["candidate"]["reviewStatus"] == "confirmed"
    assert confirmed["candidate"]["publishedFactId"]
    fact = service.load_facts(tmp_path)["facts"][0]
    assert any(ref["path"] == chapter_path for ref in fact["sourceRefs"])
    assert any(ref["role"] == "formal_relation" for ref in fact["sourceRefs"])

    rejected = service.reject_candidate(
        tmp_path,
        second["id"],
        expected_fingerprint=second["fingerprint"],
        reason="not_canon",
        note="draft-only",
    )
    assert rejected["candidate"]["reviewStatus"] == "rejected"
    assert rejected["candidate"]["rejectionReason"] == "not_canon"
    assert service.list_review_relations(tmp_path, status="review_required")["total"] == 0

    repeated_rejection = service.submit_candidates(
        tmp_path,
        [
            {
                "subjectId": subject["entityId"],
                "predicate": "visited",
                "objectId": other["entityId"],
                "sourceRefs": [{"path": chapter_path, "quote": second_quote}],
            }
        ],
    )
    assert repeated_rejection["acceptedCount"] == 0
    assert repeated_rejection["skipped"][0]["reason"] == "unchanged_candidate"
    assert repeated_rejection["skipped"][0]["reviewStatus"] == "rejected"


def test_story_increment_delegates_relation_updates_to_review_ledger(tmp_path: Path) -> None:
    relation_service = StoryKnowledgeRelationService()
    project_service = StoryProjectService()
    project_service.ensure_project_structure(tmp_path)
    subject = _entity(relation_service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(relation_service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    quote = "Tidebeast lives on Nightstar coast."

    result = project_service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/001.md",
            "segmentText": quote,
            "applyVariables": True,
            "factUpdates": [
                {
                    "subjectId": subject["entityId"],
                    "subject": "Tidebeast",
                    "predicate": "lives_on",
                    "objectId": obj["entityId"],
                    "object": "Nightstar",
                    "evidence": quote,
                }
            ],
        },
        generation_contract={
            "traceId": "story-trace",
            "sessionId": "story-session",
            "providerId": "test-provider",
            "model": "test-model",
            "knowledgeWritePolicy": {"mode": "candidate_extraction"},
        },
    )

    submission = result["knowledgeReview"]["relationSubmission"]
    assert submission["acceptedCount"] == 1
    assert relation_service.load_facts(tmp_path)["facts"] == []
    candidate = relation_service.list_review_relations(tmp_path)["relations"][0]
    assert candidate["sourceRefs"] == [
        {"path": "chapters/001.md", "quote": quote, "role": "story_generation"}
    ]
    assert candidate["traceId"] == "story-trace"
    assert candidate["provenance"]["model"] == "test-model"


def test_story_increment_keeps_dynamic_character_relationships_in_relationship_graph(
    tmp_path: Path,
) -> None:
    relation_service = StoryKnowledgeRelationService()
    project_service = StoryProjectService()
    project_service.ensure_project_structure(tmp_path)
    alice = _entity(
        relation_service,
        tmp_path,
        "Alice",
        ".storydex/characters/Alice.md",
        kind="character",
    )
    bob = _entity(
        relation_service,
        tmp_path,
        "Bob",
        ".storydex/characters/Bob.md",
        kind="character",
    )
    quote = "Alice now trusts Bob."

    result = project_service.apply_story_generation_increment(
        tmp_path,
        {
            "segmentPath": "chapters/001.md",
            "segmentText": quote,
            "applyVariables": True,
            "relationshipUpdates": [
                {
                    "source": "Alice",
                    "target": "Bob",
                    "dimension": "trust",
                    "currentLevel": 3,
                    "evidence": quote,
                    "lastUpdatedIn": "chapters/001.md",
                }
            ],
        },
        generation_contract={
            "traceId": "story-relationship-trace",
            "sessionId": "story-relationship-session",
            "providerId": "test-provider",
            "model": "test-model",
            "knowledgeWritePolicy": {"mode": "standard"},
        },
    )

    graph_path = tmp_path / ".storydex/memory/current/relationship_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge["sourceId"] == alice["entityId"]
    assert edge["targetId"] == bob["entityId"]
    assert edge["sourceRefs"] == [
        {"path": "chapters/001.md", "quote": quote, "role": "story_generation"}
    ]
    assert edge["reviewStatus"] == "confirmed"
    assert edge["knowledgeStatus"] == "observed"
    assert edge["traceId"] == "story-relationship-trace"
    assert edge["provenance"]["providerId"] == "test-provider"
    assert edge["provenance"]["model"] == "test-model"
    assert relation_service.load_review_ledger(tmp_path)["relations"] == []
    assert ".storydex/memory/current/relationship_graph.json" in result["writtenPaths"]


def test_dynamic_relationship_graph_reads_v1_and_writes_audit_fields(tmp_path: Path) -> None:
    relation_service = StoryKnowledgeRelationService()
    project_service = StoryProjectService()
    alice = _entity(
        relation_service,
        tmp_path,
        "Alice",
        ".storydex/characters/Alice.md",
        kind="character",
    )
    bob = _entity(
        relation_service,
        tmp_path,
        "Bob",
        ".storydex/characters/Bob.md",
        kind="character",
    )
    evidence_path = "chapters/001.md"
    quote = "Alice now trusts Bob."
    _write(tmp_path / evidence_path, quote + "\n")
    graph_path = tmp_path / ".storydex/memory/current/relationship_graph.json"
    _write(
        graph_path,
        json.dumps(
            {
                "version": 1,
                "edges": [
                    {
                        "source": "Alice",
                        "target": "Bob",
                        "dimension": "trust",
                        "current_level": 1,
                        "history": [],
                    }
                ],
            }
        ),
    )

    normalized = project_service._normalize_relationship_updates(
        [
            {
                "source": "Alice",
                "target": "Bob",
                "dimension": "trust",
                "currentLevel": 3,
                "evidence": quote,
                "lastUpdatedIn": evidence_path,
                "reviewStatus": "confirmed",
                "knowledgeStatus": "observed",
                "traceId": "trace-dynamic-relation",
            }
        ]
    )
    project_service._apply_relationship_updates(
        tmp_path,
        normalized,
        updated_at="2026-08-07T00:00:00+00:00",
    )

    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    edge = payload["edges"][0]
    assert edge["sourceId"] == alice["entityId"]
    assert edge["targetId"] == bob["entityId"]
    assert edge["sourceRefs"] == [
        {"path": evidence_path, "quote": quote, "role": "story_generation"}
    ]
    assert edge["reviewStatus"] == "confirmed"
    assert edge["knowledgeStatus"] == "observed"
    assert edge["traceId"] == "trace-dynamic-relation"


def test_v1_migration_creates_backup_and_is_idempotent(tmp_path: Path) -> None:
    service = StoryKnowledgeRelationService()
    current = tmp_path / ".storydex/memory/current"
    current.mkdir(parents=True)
    worldbook_path = tmp_path / ".storydex/worldbook/Alice.md"
    _write(worldbook_path, "# Alice\n\nLegacy prose must remain unchanged.\n")
    original_worldbook = worldbook_path.read_bytes()
    evidence_path = "chapters/001.md"
    quote = "Alice serves Nightstar."
    _write(tmp_path / evidence_path, quote + "\n")
    (current / "entities.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entities": [
                    {"id": "alice", "canonical_name": "Alice", "kind": "character"},
                    {"id": "nightstar", "canonical_name": "Nightstar", "kind": "setting"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (current / "facts.json").write_text(
        json.dumps(
            {
                "version": 1,
                "facts": [
                    {
                        "subject": "Alice",
                        "predicate": "serves",
                        "object": "Nightstar",
                        "confidence": "canon",
                        "established_in": evidence_path,
                        "evidence": quote,
                    },
                    {
                        "subject": "Alice",
                        "predicate": "suspects",
                        "object": "Nightstar",
                        "confidence": "uncertain",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (current / "relationship_graph.json").write_text(
        json.dumps({"version": 1, "edges": []}),
        encoding="utf-8",
    )
    legacy_wiki = tmp_path / ".storydex/wiki/knowledge_graph.json"
    _write(legacy_wiki, json.dumps({"version": 1, "graph": {"nodes": [], "edges": []}}))

    migrated = service.migrate_v1(tmp_path)
    assert migrated["migrated"] is True
    backup = tmp_path / migrated["backupPath"]
    assert (backup / "entities.json").is_file()
    assert (backup / "facts.json").is_file()
    assert (backup / "relationship_graph.json").is_file()
    assert (backup / "wiki/knowledge_graph.json").is_file()
    assert service.load_entities(tmp_path)["version"] == 2
    assert len(service.load_facts(tmp_path)["facts"]) == 1
    review = service.load_review_ledger(tmp_path)["relations"]
    assert len(review) == 1
    assert review[0]["reviewStatus"] == "review_required"
    assert review[0]["reviewReason"] == "v1_migration_insufficient_evidence"
    assert worldbook_path.read_bytes() == original_worldbook
    second = service.migrate_v1(tmp_path)
    assert second == {"ok": True, "migrated": False, "reason": "already_v2", "backupPath": ""}


def test_transactional_replace_restores_originals_on_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StoryKnowledgeRelationService()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    _write(first, "old-first")
    _write(second, "old-second")
    original_replace = os.replace
    calls = 0

    def flaky_replace(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated commit failure")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="simulated commit failure"):
        service._transactional_replace({first: "new-first", second: "new-second"})

    assert first.read_text(encoding="utf-8") == "old-first"
    assert second.read_text(encoding="utf-8") == "old-second"


def test_apply_restores_relation_files_when_projection_omits_confirmed_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StoryKnowledgeRelationService()
    subject = _entity(service, tmp_path, "Tidebeast", ".storydex/worldbook/Tidebeast.md")
    obj = _entity(service, tmp_path, "Nightstar", ".storydex/worldbook/Nightstar.md")
    prepared = service.prepare_explicit(
        tmp_path,
        [{"subjectId": subject["entityId"], "predicate": "lives_on", "objectId": obj["entityId"]}],
        session_id="projection-rollback",
        trace_id="projection-prepare",
    )
    tracked_paths = [
        tmp_path / ".storydex/worldbook/Tidebeast.md",
        service.entities_path(tmp_path),
        service.facts_path(tmp_path),
    ]
    before = {
        path: path.read_bytes() if path.exists() else None
        for path in tracked_paths
    }

    monkeypatch.setattr(
        service,
        "_rebuild_projection",
        lambda _root: {
            "status": "ready",
            "knowledgeRevision": 1,
            "graph": {"nodes": [], "edges": []},
        },
    )
    with pytest.raises(KnowledgeRelationError) as exc_info:
        service.apply_explicit(
            tmp_path,
            prepared["planId"],
            session_id="projection-rollback",
            trace_id="projection-confirm",
            expected_fingerprint=prepared["fingerprint"],
        )

    assert exc_info.value.code == "knowledge_projection_relation_missing"
    assert {
        path: path.read_bytes() if path.exists() else None
        for path in tracked_paths
    } == before
    plan = json.loads(
        (service.plans_root(tmp_path) / f"{prepared['planId']}.json").read_text(encoding="utf-8")
    )
    assert plan.get("status") != "applied"


def test_projection_reports_62_nodes_52_edges_and_no_isolated_nodes(tmp_path: Path) -> None:
    relation_service = StoryKnowledgeRelationService()
    planets = [
        _entity(relation_service, tmp_path, f"Planet-{index:02d}", f".storydex/worldbook/Planet-{index:02d}.md")
        for index in range(10)
    ]
    creatures = [
        _entity(relation_service, tmp_path, f"Creature-{index:02d}", f".storydex/worldbook/Creature-{index:02d}.md")
        for index in range(52)
    ]
    prepared = relation_service.prepare_explicit(
        tmp_path,
        [
            {
                "subjectId": creature["entityId"],
                "predicate": "lives_on",
                "objectId": planets[index % len(planets)]["entityId"],
            }
            for index, creature in enumerate(creatures)
        ],
        session_id="bulk",
        trace_id="bulk-prepare",
    )
    relation_service.apply_explicit(
        tmp_path,
        prepared["planId"],
        session_id="bulk",
        trace_id="bulk-confirm",
        expected_fingerprint=prepared["fingerprint"],
    )

    wiki_service = StoryWikiService()
    first_page = wiki_service.query_graph(tmp_path, category="setting", limit=60, offset=0)
    second_page = wiki_service.query_graph(tmp_path, category="setting", limit=60, offset=60)
    assert first_page["total"]["nodeCount"] == 62
    assert first_page["total"]["edgeCount"] == 52
    assert first_page["total"]["confirmedEdgeCount"] == 52
    assert first_page["total"]["isolatedNodeCount"] == 0
    assert first_page["returnedNodeCount"] == 60
    assert first_page["hasMore"] is True
    assert second_page["returnedNodeCount"] == 2
    assert second_page["hasMore"] is False
