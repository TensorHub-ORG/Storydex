from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import routes_wiki
from core.exceptions import StorydexError
from services.story_knowledge_relation_service import StoryKnowledgeRelationService


def _prepare_candidate(tmp_path: Path) -> tuple[StoryKnowledgeRelationService, dict]:
    service = StoryKnowledgeRelationService()
    current = tmp_path / ".storydex/memory/current"
    current.mkdir(parents=True)
    worldbook = tmp_path / ".storydex/worldbook"
    worldbook.mkdir(parents=True)
    (worldbook / "Alice.md").write_text("# Alice\n", encoding="utf-8")
    (worldbook / "Nightstar.md").write_text("# Nightstar\n", encoding="utf-8")
    (current / "entities.json").write_text(
        json.dumps(
            {
                "version": 2,
                "schemaVersion": 2,
                "entities": [
                    {
                        "entityId": "entity:alice",
                        "canonical_name": "Alice",
                        "kind": "character",
                        "sourcePaths": [".storydex/worldbook/Alice.md"],
                    },
                    {
                        "entityId": "entity:night",
                        "canonical_name": "Nightstar",
                        "kind": "setting",
                        "sourcePaths": [".storydex/worldbook/Nightstar.md"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "chapters/001.md"
    source.parent.mkdir(parents=True)
    quote = "Alice serves Nightstar."
    source.write_text(quote + "\n", encoding="utf-8")
    submitted = service.submit_candidates(
        tmp_path,
        [
            {
                "subjectId": "entity:alice",
                "predicate": "serves",
                "objectId": "entity:night",
                "sourceRefs": [{"path": "chapters/001.md", "quote": quote}],
            }
        ],
        provider_id="test-provider",
        model="test-model",
        trace_id="extract-trace",
    )
    return service, submitted["candidates"][0]


@pytest.fixture()
def isolated_review_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, candidate = _prepare_candidate(tmp_path)
    monkeypatch.setattr(routes_wiki, "project_service", SimpleNamespace(workspace_root=tmp_path))
    monkeypatch.setattr(routes_wiki, "story_knowledge_relation_service", service)
    return service, candidate


def test_review_route_lists_and_rejects_candidate(isolated_review_routes) -> None:
    service, candidate = isolated_review_routes
    queue = routes_wiki.read_relation_review_queue(status="review_required", offset=0, limit=100)
    assert queue.data["total"] == 1
    assert queue.data["relations"][0]["id"] == candidate["id"]

    response = routes_wiki.reject_relation_candidate(
        candidate["id"],
        {"expectedFingerprint": candidate["fingerprint"], "reason": "incorrect", "note": "not supported"},
    )
    assert response.data["candidate"]["reviewStatus"] == "rejected"
    assert service.list_review_relations(routes_wiki.project_service.workspace_root)["total"] == 0


def test_review_route_confirms_candidate_with_corrections(isolated_review_routes) -> None:
    service, candidate = isolated_review_routes
    root = routes_wiki.project_service.workspace_root

    response = routes_wiki.confirm_relation_candidate(
        candidate["id"],
        {
            "expectedFingerprint": candidate["fingerprint"],
            "subjectId": "entity:alice",
            "predicate": "inhabits",
            "objectId": "entity:night",
            "targetSourcePath": ".storydex/worldbook/Alice.md",
        },
    )

    confirmed = response.data["candidate"]
    assert confirmed["reviewStatus"] == "confirmed"
    assert confirmed["predicate"] == "inhabits"
    assert confirmed["publishedFactId"].startswith("fact:")
    assert response.data["apply"]["operation"] == "apply_explicit"
    assert service.list_review_relations(root)["total"] == 0
    assert any(
        fact.get("id") == confirmed["publishedFactId"]
        and fact.get("reviewStatus") == "confirmed"
        and fact.get("predicate") == "inhabits"
        for fact in service.load_facts(root)["facts"]
    )
    formal_text = (root / ".storydex/worldbook/Alice.md").read_text(encoding="utf-8")
    assert "inhabits" in formal_text
    assert "[[Nightstar]]" in formal_text


def test_review_route_maps_stale_fingerprint_to_conflict(isolated_review_routes) -> None:
    _service, candidate = isolated_review_routes
    with pytest.raises(StorydexError) as exc_info:
        routes_wiki.confirm_relation_candidate(
            candidate["id"],
            {
                "expectedFingerprint": "stale",
                "subjectId": "entity:alice",
                "predicate": "serves",
                "objectId": "entity:night",
            },
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "knowledge_candidate_stale"
