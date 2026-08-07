import json

from services.evidence_ledger_service import (
    EvidenceLedgerService,
    merge_evidence_spans,
)
from services.coomi_agent_service import _CoomiEventTranslator
from services.source_contract import source_revision_id


def _revision(value: str) -> str:
    return source_revision_id(value.encode("utf-8"))


def _span(revision: str, start: int, end: int) -> dict:
    return {
        "startChar": start,
        "endChar": end,
        "startByte": start,
        "endByte": end,
        "startLine": 1,
        "endLine": 1,
        "revision": revision,
    }


def test_merge_evidence_spans_merges_overlap_and_keeps_gaps() -> None:
    revision = _revision("same")
    merged = merge_evidence_spans([_span(revision, 0, 10), _span(revision, 10, 20), _span(revision, 30, 40)])

    assert [(item["startChar"], item["endChar"]) for item in merged] == [(0, 20), (30, 40)]


def test_ledger_persists_cross_turn_observations_and_merges_spans(tmp_path) -> None:
    service = EvidenceLedgerService(tmp_path, "session-a")
    revision = _revision("alpha")
    first = service.record(
        path="chapters/001.md",
        revision=revision,
        span=_span(revision, 0, 10),
        source_tool="read_file",
        turn_id="turn-1",
    )
    second = service.record(
        path="chapters/001.md",
        revision=revision,
        span=_span(revision, 10, 20),
        source_tool="StorydexProjectSearch",
        turn_id="turn-2",
    )

    assert first["recorded"] is True
    assert second["recorded"] is True
    reloaded = EvidenceLedgerService(tmp_path, "session-a").snapshot()
    assert len(reloaded["entries"]) == 1
    entry = reloaded["entries"][0]
    assert entry["firstObservedTurn"] == "turn-1"
    assert entry["lastObservedTurn"] == "turn-2"
    assert set(entry["sourceTools"]) == {"read_file", "StorydexProjectSearch"}
    assert [(item["startChar"], item["endChar"]) for item in entry["spans"]] == [(0, 20)]


def test_revision_change_invalidates_only_changed_path(tmp_path) -> None:
    service = EvidenceLedgerService(tmp_path, "session-a")
    old = _revision("old")
    new = _revision("new")
    other = _revision("other")
    service.record(path="chapters/001.md", revision=old, span=_span(old, 0, 5), source_tool="read_file")
    service.record(path="chapters/002.md", revision=other, span=_span(other, 0, 5), source_tool="read_file")
    result = service.record(path="chapters/001.md", revision=new, span=_span(new, 0, 5), source_tool="read_file")

    snapshot = service.snapshot()
    assert result["invalidated"] == [{"path": "chapters/001.md", "revision": old}]
    assert {item["path"] for item in snapshot["entries"]} == {"chapters/001.md", "chapters/002.md"}
    assert {item["revision"] for item in snapshot["entries"] if item["path"] == "chapters/001.md"} == {new}
    assert snapshot["invalidations"][0]["newRevision"] == new


def test_record_tool_result_extracts_read_and_search_spans_and_coverage_gate(tmp_path) -> None:
    service = EvidenceLedgerService(tmp_path, "session-a")
    revision = _revision("chapter text")
    output = {
        "status": "ok",
        "resultState": "hits",
        "query": "marker",
        "candidatePaths": ["chapters/001.md", "chapters/002.md"],
        "results": [
            {"path": "chapters/001.md", "revision": revision, "snippetSpan": _span(revision, 4, 12)},
        ],
    }
    result = service.record_tool_result(
        tool_name="StorydexProjectSearch",
        arguments={"query": "marker"},
        raw_output="success: " + json.dumps(output, ensure_ascii=False),
        turn_id="turn-3",
    )

    assert result["recorded"] is True
    assert result["observationCount"] == 1
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["missingPaths"] == ["chapters/002.md"]
    assert service.coverage_gate(["chapters/001.md"], revisions={"chapters/001.md": revision})["complete"] is True


def test_coomi_tool_trace_records_ledger_without_changing_tool_output(tmp_path) -> None:
    revision = _revision("read result")
    translator = _CoomiEventTranslator(
        session_id="session-a",
        trace_id="turn-4",
        workspace_root=tmp_path,
    )
    translator.translate(
        {"type": "tool_started", "data": {"call": {"id": "call-1", "name": "read_file", "arguments": {}}}}
    )
    event = translator.translate(
        {
            "type": "tool_finished",
            "data": {
                "call": {"id": "call-1", "name": "read_file", "arguments": {}},
                "result": {
                    "success": True,
                    "output": json.dumps({"path": "chapters/001.md", "revision": revision, "span": _span(revision, 0, 12), "content": "read result"}),
                },
            },
        }
    )

    assert event is not None
    assert event[0] == "ToolDone"
    assert "content" not in event[1]
    assert event[1]["evidenceLedger"]["recorded"] is True
    assert EvidenceLedgerService(tmp_path, "session-a").snapshot()["entries"][0]["path"] == "chapters/001.md"
