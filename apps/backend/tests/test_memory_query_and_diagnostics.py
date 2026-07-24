from __future__ import annotations

import json
import subprocess
import types
from datetime import datetime, timedelta, timezone

import pytest

from core.exceptions import StorydexError
from services import diagnostics_service, fact_memory_store, relationship_memory_store, request_auth_service, storydex_coomi_runtime_tools, storydex_retrieval


def test_memory_catalog_diagnostic_governance_branches(tmp_path):
    catalog = tmp_path / "catalog.json"
    stale = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    catalog.write_text(
        json.dumps(
            {
                "modules": [
                    None,
                    {"id": "partial", "path": "", "updatedAt": "not-a-time"},
                    {
                        "id": "canon",
                        "path": "canon",
                        "purpose": "facts",
                        "schemaVersion": 1,
                        "consumers": ["agent"],
                        "updatedAt": stale,
                    },
                    {
                        "id": "canon",
                        "path": "canon",
                        "purpose": "duplicate",
                        "schemaVersion": 1,
                        "updatedAt": stale,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    codes = [item["code"] for item in diagnostics_service.DiagnosticsService._memory_catalog_diagnostics(catalog)]
    assert "story.memory.module_incomplete" in codes
    assert "story.memory.module_duplicate" in codes
    assert "story.memory.module_unused" in codes
    assert "story.memory.module_stale" in codes

    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"characters": []}', encoding="utf-8")
    assert diagnostics_service.DiagnosticsService._legacy_memory_diagnostic(
        path=legacy,
        relative_path=".storydex/memory/legacy.json",
    )["severity"] == "info"
    legacy.write_text("[]", encoding="utf-8")
    assert diagnostics_service.DiagnosticsService._legacy_memory_diagnostic(
        path=legacy,
        relative_path=".storydex/memory/legacy.json",
    ) is None


def test_fact_store_loading_relevance_context_inference_dedup_and_paths(tmp_path):
    store = fact_memory_store.FactMemoryStore(tmp_path)
    assert store.load_facts_payload()["facts"] == []
    store.facts_path.parent.mkdir(parents=True)
    store.facts_path.write_text("[]", encoding="utf-8")
    assert store.load_facts_payload()["facts"] == []
    store.facts_path.write_text("{broken", encoding="utf-8")
    assert store.load_facts_payload()["facts"] == []
    facts = [
        None,
        {"subject": "Alice", "predicate": "lives", "object": "City", "confidence": "canon", "evidence": "chapter text", "establishedIn": "1"},
        {"subject": "Alice", "predicate": "lives", "object": "City", "confidence": "tentative"},
        {"subject": "Alice", "predicate": "knows", "object": "Bob", "confidence": "confirmed"},
        {"subject": "Bob", "predicate": "", "object": "x"},
    ]
    store.facts_path.write_text(json.dumps({"facts": facts}), encoding="utf-8")
    relevant = store.relevant_facts(["Alice"], max_facts=10)
    assert len(relevant) == 2 and relevant[0].context_line().startswith("- Alice")
    assert store.relevant_facts([], max_facts=10) == []
    assert store.relevant_facts(["Alice"], max_facts=0) == []
    with_tentative = store.relevant_facts(["Alice"], include_tentative=True, max_facts=10)
    assert len(with_tentative) == 2  # duplicate lower-confidence fact is removed
    assert "Project Fact Context" in store.project_context(prompt="", active_file="", active_entities=["Alice"])
    chapter = tmp_path / "chapters/1.md"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("Alice enters the city", encoding="utf-8")
    assert "Alice" in store.project_context(prompt="", active_file="chapters/1.md", active_entities=[])
    assert store.project_context(prompt="none", active_file="", active_entities=[]) == ""
    assert store._subject_names() == ("Alice", "Bob")
    assert store._safe_workspace_file("") is None and store._safe_workspace_file("../bad") is None
    assert store._safe_workspace_file("chapters/1.md") == chapter.resolve()
    assert fact_memory_store.ProjectFact.from_payload({}) is None
    long = fact_memory_store.ProjectFact("A", "p", "o", evidence="x" * 200)
    assert "truncated" in long.context_line(evidence_chars=30)


def test_relationship_store_edges_neighborhood_depth_dimensions_context_and_paths(tmp_path):
    store = relationship_memory_store.RelationshipMemoryStore(tmp_path)
    assert store.load_graph()["edges"] == []
    store.graph_path.parent.mkdir(parents=True)
    edges = [
        None,
        {"source": "Alice", "target": "Bob", "dimension": "trust", "current_level": 20, "last_updated_in": "5", "history": [None, {"delta": "+", "magnitude": "large", "detail": "saved", "evidence": "chapter"}]},
        {"source": "Bob", "target": "Carol", "dimension": "hostility", "current_level": -4, "history": []},
        {"source": "Alice", "target": "Alice"},
        {"source": "", "target": "Bob"},
    ]
    store.graph_path.write_text(json.dumps({"edges": edges}), encoding="utf-8")
    neighborhood = store.neighborhood(["Alice"], depth=2, max_edges=10)
    assert len(neighborhood.edges) == 2 and neighborhood.edges[0].current_level <= 10
    assert store.neighborhood([], depth=0).edges == []
    assert store.neighborhood(["Alice"], dimensions=["trust"]).edges[0].dimension == "trust"
    assert store.neighborhood(["Alice"], dimensions=["family"]).edges == []
    assert store.neighborhood(["Alice"], max_edges=0).edges == []
    assert "Relationship Context" in store.project_context(prompt="", active_file="", active_entities=["Alice"])
    chapter = tmp_path / "chapters/1.md"
    chapter.parent.mkdir(parents=True)
    chapter.write_text("Alice and Bob", encoding="utf-8")
    assert "Alice" in store.project_context(prompt="", active_file="chapters/1.md", active_entities=[])
    assert store.project_context(prompt="Nobody", active_file="", active_entities=[]) == ""
    edge = relationship_memory_store.RelationshipEdge.from_payload(edges[1])
    assert edge.touches_any(["Alice"]) is True and edge.other_entities(["Alice"]) == ("Bob",)
    assert "evidence" in edge.context_line()
    assert relationship_memory_store.RelationshipEdge.from_payload({"source": "A", "target": "A"}) is None
    assert store._safe_workspace_file("../bad") is None
    assert relationship_memory_store._safe_int("bad", 3) == 3


def test_retrieval_tokenization_bm25_hybrid_errors_snippets_and_external_path(monkeypatch, tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    empty = tmp_path / "empty.md"
    a.write_text("Alice watches the magic city", encoding="utf-8")
    b.write_text("Bob visits another city", encoding="utf-8")
    empty.write_text("", encoding="utf-8")
    assert "魔法" in storydex_retrieval.tokenize("魔法城市")
    assert storydex_retrieval.tokenize("") == []
    assert storydex_retrieval.bm25_search("", [a], tmp_path) == []
    results = storydex_retrieval.bm25_search("Alice city", [a, b, empty, tmp_path / "missing"], tmp_path, limit=1)
    assert len(results) == 1 and results[0]["doc_id"] == "a.md" and results[0]["metadata"]["snippet"]
    hybrid = storydex_retrieval.hybrid_search("Bob", [a, b], tmp_path)
    assert hybrid[0]["engine"] == "storydex_bm25"
    assert storydex_retrieval._build_snippet("plain text", ["missing"]) == ""
    assert "Alice" in storydex_retrieval._build_snippet("x" * 150 + "Alice" + "y" * 300, ["alice"])
    monkeypatch.setattr(storydex_retrieval, "read_text_limited", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    assert storydex_retrieval.bm25_search("Alice", [a], tmp_path) == []


def test_diagnostics_files_operations_story_large_and_helpers(tmp_path):
    service = diagnostics_service.DiagnosticsService()
    service.project_service = types.SimpleNamespace(workspace_root=tmp_path.resolve())
    service.story_project_service = types.SimpleNamespace(collect_story_diagnostics=lambda root: {
        "story.md": [None, {"message": "story warning"}]
    })
    good_py = tmp_path / "good.py"
    bad_py = tmp_path / "bad.py"
    bad_json = tmp_path / "bad.json"
    bom_json = tmp_path / "bom.json"
    story = tmp_path / "story.md"
    good_py.write_text("x = 1\n", encoding="utf-8")
    bad_py.write_text("def broken(:\n", encoding="utf-8")
    bad_json.write_text("{broken", encoding="utf-8")
    bom_json.write_bytes(b"\xef\xbb\xbf{\"ok\": true}\n")
    story.write_text("story", encoding="utf-8")
    diagnostics = service.diagnose_paths(["", "../outside.py", "good.py", "bad.py", "bad.json", "story.md", "missing.py"])
    assert any(item["source"] == "python.ast" for item in diagnostics)
    assert any(item["source"] == "json" for item in diagnostics)
    assert any(item["message"] == "story warning" for item in diagnostics)
    assert service._diagnose_python_content(content="x=1", relative_path="a.py") == []
    assert service._diagnose_json_content(content="{}", relative_path="a.json") == []
    assert service._diagnose_text(content="bad", relative_path="a.txt") == []
    bom_diagnostics = service.diagnose_paths(["bom.json"])
    assert [item["code"] for item in bom_diagnostics] == ["text.utf8_bom"]
    assert service.apply_fix(relative_path="bom.json", fix_id="remove_utf8_bom")["changed"] is True
    assert not bom_json.read_bytes().startswith(b"\xef\xbb\xbf")

    operation_diagnostics = service.diagnose_workspace_operations([
        None,
        {"op": "write", "relativePath": "new.py", "content": "def bad(:"},
        {"op": "append", "relativePath": "good.py", "content": "def bad(:"},
        {"op": "edit", "relativePath": "good.py", "oldString": "x = 1", "newString": "x = 2"},
        {"op": "edit", "relativePath": "good.py", "oldString": "missing", "newString": "x"},
        {"op": "multi_edit", "relativePath": "good.py", "edits": [{"oldString": "x = 1", "newString": "x = 3"}]},
        {"op": "multi_edit", "relativePath": "good.py", "edits": [None]},
        {"op": "delete", "relativePath": "good.py"},
        {"op": "write", "relativePath": "../outside.py", "content": "bad"},
    ])
    assert operation_diagnostics
    assert service._append_text("a", "b") == "a\nb"
    assert service._append_text("", "b") == "b"
    assert service._append_text("a", "") == "a"
    with pytest.raises(ValueError):
        service._apply_text_edit("a", old_string="", new_string="b", replace_all=False)
    with pytest.raises(ValueError):
        service._apply_text_edit("a", old_string="x", new_string="b", replace_all=False)
    with pytest.raises(ValueError):
        service._apply_text_edit("xx", old_string="x", new_string="b", replace_all=False)
    assert service._apply_text_edit("xx", old_string="x", new_string="b", replace_all=True) == "bb"
    assert service._normalize_relative_path("./a\\b") == "a/b"

    large = tmp_path / "large.json"
    large.write_bytes(b" " * (diagnostics_service.MAX_DIAGNOSTIC_FILE_BYTES + 1))
    assert service._diagnose_path(path=large, relative_path="large.json")[0]["source"] == "diagnostics.size_limit"


def test_request_auth_required_optional_and_authentication(monkeypatch):
    with pytest.raises(StorydexError) as invalid:
        request_auth_service.require_bearer_token(None)
    assert invalid.value.code == "auth_header_invalid"
    with pytest.raises(StorydexError) as missing:
        request_auth_service.require_bearer_token("Bearer ")
    assert missing.value.code == "auth_header_invalid"
    assert request_auth_service.require_bearer_token("bearer token") == "token"
    assert request_auth_service.resolve_request_user_optional(None) is None
    assert request_auth_service.resolve_request_bearer_token_optional(None) == ""
    fake = types.SimpleNamespace(authenticate_token=lambda token: {"token": token})
    monkeypatch.setattr(request_auth_service, "get_auth_service", lambda: fake)
    assert request_auth_service.resolve_request_user_optional("Bearer abc") == {"token": "abc"}
    assert request_auth_service.resolve_request_bearer_token_optional("Bearer abc") == "abc"


def test_workspace_bound_runtime_tools_normalization_shell_success_failures(monkeypatch, tmp_path):
    tools = storydex_coomi_runtime_tools.create_workspace_bound_tool_overrides(tmp_path)
    assert len(tools) == 7
    read = tools[0]
    normalized = read._normalized_arguments({"file_path": "a.md", "path": str((tmp_path / "abs").resolve()), "directory": ""})
    assert normalized["file_path"] == (tmp_path / "a.md").as_posix()
    read.set_workspace_root(tmp_path / "next")
    assert read.workspace_root == (tmp_path / "next").resolve()
    glob = tools[3]
    grep = tools[4]
    assert glob._normalized_arguments({})["path"] == tmp_path.resolve().as_posix()
    assert grep._normalized_arguments({})["path"] == tmp_path.resolve().as_posix()

    bash = tools[5]
    monkeypatch.setattr(storydex_coomi_runtime_tools.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    assert bash.run({"command": "echo ok"}).success is True
    monkeypatch.setattr(storydex_coomi_runtime_tools.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=2, stdout="", stderr="bad"))
    failed = bash.run({"command": "bad"})
    assert failed.success is False and "code 2" in failed.error
    monkeypatch.setattr(storydex_coomi_runtime_tools.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1)))
    assert "timed out" in bash.run({"command": "slow"}).error
    monkeypatch.setattr(storydex_coomi_runtime_tools.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "RuntimeError" in bash.run({"command": "bad"}).error

    powershell = tools[6]
    monkeypatch.setattr(storydex_coomi_runtime_tools.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout="ok", stderr="warn"))
    assert powershell.run({"command": "ok"}).success is True
    monkeypatch.setattr(storydex_coomi_runtime_tools.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr="bad"))
    assert powershell.run({"command": "bad"}).success is False
    monkeypatch.setattr(storydex_coomi_runtime_tools.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    assert "PowerShell not found" in powershell.run({"command": "x"}).error
