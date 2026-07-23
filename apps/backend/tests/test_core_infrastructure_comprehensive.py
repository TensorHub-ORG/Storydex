from __future__ import annotations

import asyncio
import json
import sqlite3
import types
from pathlib import Path

import pytest

from core import bounded_text_io, feature_flags
from services import execution_log_service, global_config_service, job_queue, secure_storage_service


def test_bounded_text_reads_small_large_head_tail_and_missing_stat(monkeypatch, tmp_path):
    small = tmp_path / "small.txt"
    small.write_text("  hello world  ", encoding="utf-8")
    read = bounded_text_io.read_text_limited(small, 100)
    assert read.text == "  hello world  " and read.truncated is False
    limited = bounded_text_io.read_text_limited(small, 5)
    assert limited.text == "  hel" and limited.truncated is True
    assert bounded_text_io.read_text_preview(small, max_chars=100) == "hello world"
    assert bounded_text_io.read_text_tail(small, max_chars=100) == "hello world"

    medium = tmp_path / "medium.txt"
    medium.write_text("A" * 200 + "END", encoding="utf-8")
    preserved = bounded_text_io.read_text_limited(medium, 120, preserve_tail=True, middle_marker="<cut>")
    assert preserved.truncated is True and preserved.text.startswith("A") and preserved.text.endswith("END")
    assert bounded_text_io._head_tail_from_text("x" * 100, 20, marker="marker") is None
    assert bounded_text_io.read_text_tail(medium, max_chars=30).startswith(bounded_text_io.TAIL_ANCHOR_MARKER)

    large = tmp_path / "large.txt"
    large.write_text("头" * 40000 + "TAIL", encoding="utf-8")
    head = bounded_text_io.read_text_limited(large, 100)
    assert head.truncated is True and len(head.text) == 100
    both = bounded_text_io.read_text_limited(large, 200, preserve_tail=True, middle_marker="<middle>")
    assert "<middle>" in both.text and both.text.endswith("TAIL")
    tail = bounded_text_io.read_text_tail(large, max_chars=80)
    assert tail.endswith("TAIL")
    assert bounded_text_io._full_read_limit(1) >= bounded_text_io.MAX_FULL_READ_BYTES
    assert bounded_text_io._read_tail_chars(large, 4, size_bytes=0) == "TAIL"


def test_feature_flags_project_environment_defaults_snapshot_and_cache(monkeypatch, tmp_path):
    missing = feature_flags.FeatureFlags(tmp_path, {"A": False, "N": 3, "RAW": "x"})
    assert missing.project_root == tmp_path and missing.get_bool("A") is False
    flags_path = tmp_path / ".storydex/config/feature-flags.json"
    flags_path.parent.mkdir(parents=True)
    flags_path.write_text(json.dumps({"A": "yes", "B": "off", "N": "9", "BAD": "x", "RAW": "project"}), encoding="utf-8")
    flags = feature_flags.FeatureFlags(tmp_path, {"A": False, "B": True, "N": 3, "BAD": 4, "RAW": "default"})
    assert flags.get_bool("A") is True and flags.get_bool("B") is False
    assert flags.get_int("N") == 9 and flags.get_int("BAD", fallback=7) == 4
    monkeypatch.setenv("ENV_TRUE", "on")
    monkeypatch.setenv("ENV_FALSE", "0")
    monkeypatch.setenv("ENV_BAD", "unknown")
    monkeypatch.setenv("INT", "12")
    monkeypatch.setenv("INT_BAD", "x")
    env = feature_flags.FeatureFlags(None, {"ENV_TRUE": False, "ENV_FALSE": True, "ENV_BAD": True, "INT": 1, "INT_BAD": 8, "RAW": "fallback"})
    assert env.get_bool("ENV_TRUE") is True and env.get_bool("ENV_FALSE") is False and env.get_bool("ENV_BAD") is True
    assert env.get_int("INT") == 12 and env.get_int("INT_BAD") == 8
    assert env.snapshot()["RAW"] == "fallback"
    flags_path.write_text("[]", encoding="utf-8")
    assert feature_flags.FeatureFlags(tmp_path, {}).snapshot() == {}
    flags_path.write_text("{broken", encoding="utf-8")
    assert feature_flags.FeatureFlags(tmp_path, {}).snapshot() == {}
    feature_flags.reset_cache()


def test_execution_log_sanitization_session_context_and_unique_paths(monkeypatch, tmp_path):
    assert execution_log_service.sanitize_execution_log_payload(None) is None
    assert execution_log_service.sanitize_execution_log_payload("x" * 130000).endswith("...")
    deep = value = {}
    for _ in range(10):
        value["x"] = {}
        value = value["x"]
    assert "truncated" in str(execution_log_service.sanitize_execution_log_payload(deep))
    many = execution_log_service.sanitize_execution_log_payload(list(range(520)))
    assert len(many) == 513 and "more" in many[-1]
    mapping = execution_log_service.sanitize_execution_log_payload({str(i): i for i in range(520)})
    assert mapping["__truncated_items__"] == 8
    assert execution_log_service.sanitize_execution_log_payload(object())

    path = tmp_path / "logs/run.jsonl"
    session = execution_log_service.ExecutionLogSession(
        path=path, trace_id="t", session_id="s", request_kind="agent", workspace_root="/w", storydex_root="/w/.storydex"
    )
    session.bind(empty="", none=None, value={"x": 1})
    session.write("started", {"secret": "safe"}, trace={"duration": 1})
    session.write("done")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[0]["metadata"] == {"value": {"x": 1}} and rows[0]["trace"]["duration"] == 1
    assert execution_log_service.get_current_execution_log_session() is None
    with execution_log_service.use_execution_log_session(session):
        assert execution_log_service.get_current_execution_log_session() is session
    assert execution_log_service.get_current_execution_log_session() is None

    log_dir = tmp_path / "unique"
    log_dir.mkdir()
    first = execution_log_service._build_unique_log_path(log_dir)
    first.write_text("", encoding="utf-8")
    second = execution_log_service._build_unique_log_path(log_dir)
    assert second != first

    fake_project = types.SimpleNamespace(
        storydex_root=tmp_path / ".storydex",
        agent_root=tmp_path / ".storydex" / ".agent",
        workspace_root=tmp_path,
    )
    import services.project_service as project_module
    monkeypatch.setattr(project_module, "get_project_service", lambda: fake_project)
    created = execution_log_service.create_execution_log_session(trace_id=" t ", session_id="", request_kind="", metadata={"a": 1})
    assert created.session_id == "default" and created.request_kind == "agent_run" and created.metadata == {"a": 1}
    assert created.path.parent == tmp_path / ".storydex" / ".agent" / "logs"


def test_secure_storage_roundtrip_validation_tampering_and_helpers(monkeypatch, tmp_path):
    service = secure_storage_service.SecureStorageService(root=tmp_path)
    monkeypatch.setattr(
        secure_storage_service,
        "_dpapi_protect",
        lambda raw, *, entropy: service._fallback_encrypt(raw, user_id=entropy.decode("utf-8")),
    )
    monkeypatch.setattr(
        secure_storage_service,
        "_dpapi_unprotect",
        lambda raw, *, entropy: service._fallback_decrypt(raw, user_id=entropy.decode("utf-8")),
    )
    with pytest.raises(secure_storage_service.SecureStorageError):
        service.encrypt_json({}, user_id="")
    encrypted = service.encrypt_json({"token": "secret", "n": 1}, user_id="u")
    expected_scheme = "dpapi-v1" if __import__("os").name == "nt" else "local-secret-v1"
    assert encrypted["scheme"] == expected_scheme
    assert service.decrypt_json(encrypted, user_id="u") == {"token": "secret", "n": 1}
    with pytest.raises(secure_storage_service.SecureStorageError):
        service.decrypt_json(encrypted, user_id="")
    with pytest.raises(secure_storage_service.SecureStorageError):
        service.decrypt_json({"scheme": "local-secret-v1"}, user_id="u")
    with pytest.raises(secure_storage_service.SecureStorageError):
        service.decrypt_json({"scheme": "bad", "ciphertext": encrypted["ciphertext"]}, user_id="u")
    tampered = dict(encrypted)
    raw = bytearray(__import__("base64").b64decode(tampered["ciphertext"]))
    raw[-1] ^= 1
    tampered["ciphertext"] = __import__("base64").b64encode(raw).decode()
    with pytest.raises(secure_storage_service.SecureStorageError):
        service.decrypt_json(tampered, user_id="u")
    with pytest.raises(secure_storage_service.SecureStorageError):
        service._fallback_decrypt(b"short", user_id="u")
    assert service._fallback_key(user_id="u") == service._fallback_key(user_id="u")
    assert secure_storage_service._xor_bytes(b"\x01\x02", b"\x01\x01") == b"\x00\x03"
    assert len(secure_storage_service._keystream(key=b"k" * 32, nonce=b"n" * 16, length=65)) == 65
    blob, buffer = secure_storage_service._blob_from_bytes(b"abc")
    assert secure_storage_service._bytes_from_blob(blob) == b"abc"
    assert buffer is not None
    assert secure_storage_service._bytes_from_blob(secure_storage_service._DATA_BLOB()) == b""


def test_secure_storage_cross_platform_dpapi_and_decode_branches(monkeypatch, tmp_path):
    service = secure_storage_service.SecureStorageService(root=tmp_path)
    monkeypatch.setattr(secure_storage_service.os, "name", "nt")
    monkeypatch.setattr(secure_storage_service, "_dpapi_protect", lambda raw, *, entropy: b"protected:" + raw)
    monkeypatch.setattr(secure_storage_service, "_dpapi_unprotect", lambda raw, *, entropy: raw.removeprefix(b"protected:"))
    encrypted = service.encrypt_json({"token": "secret"}, user_id="u")
    assert encrypted["scheme"] == "dpapi-v1"
    assert service.decrypt_json(encrypted, user_id="u") == {"token": "secret"}

    monkeypatch.setattr(secure_storage_service.os, "name", "posix")
    non_object = service.encrypt_json([], user_id="u")
    with pytest.raises(secure_storage_service.SecureStorageError, match="payload_invalid"):
        service.decrypt_json(non_object, user_id="u")
    monkeypatch.setattr(secure_storage_service.base64, "b64decode", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(secure_storage_service.SecureStorageError, match="ciphertext_invalid"):
        service.decrypt_json({"scheme": "local-secret-v1", "ciphertext": "bad"}, user_id="u")


def test_job_queue_lifecycle_dedup_filters_retries_wait_and_singleton(tmp_path):
    queue = job_queue.JobQueue(tmp_path / "jobs.db")
    first = queue.enqueue(kind="ok", payload={"x": 1}, dedup_key="same", project_id="p")
    assert queue.enqueue(kind="ok", payload={"x": 2}, dedup_key="same") == first
    second = queue.enqueue(kind="sync", payload={"y": 2})
    assert queue.get("missing") is None
    assert queue.get(first).payload == {"x": 1}
    assert queue.queue_depth() == 2
    assert len(queue.list_pending(kind="ok")) == 1

    called = []
    queue.register_handler("ok", lambda payload: called.append(("sync", payload)))

    async def async_handler(payload):
        called.append(("async", payload))

    queue.register_handler("sync", async_handler)
    assert asyncio.run(queue.process_pending()) == 2
    assert queue.get(first).status == "done" and queue.get(second).status == "done"
    assert len(called) == 2
    assert asyncio.run(queue.wait_for(kind="ok", timeout_ms=10)) is True

    missing_handler = queue.enqueue(kind="missing", payload={})
    asyncio.run(queue._run_job(queue.get(missing_handler)))
    assert queue.get(missing_handler).status == "failed"

    attempts = []

    def broken(payload):
        attempts.append(1)
        raise RuntimeError("bad")

    queue.register_handler("broken", broken)
    retry = queue.enqueue(kind="broken", payload={}, max_retries=2)
    asyncio.run(queue._run_job(queue.get(retry)))
    assert queue.get(retry).status == "pending" and queue.get(retry).retry_count == 1
    asyncio.run(queue._run_job(queue.get(retry)))
    assert queue.get(retry).status == "failed" and queue.get(retry).retry_count == 2

    pending = queue.enqueue(kind="wait", payload={}, project_id="p")
    assert asyncio.run(queue.wait_for(kind="wait", project_id="p", timeout_ms=1)) is False
    queue._mark(pending, status="done", last_error="x", started_at=1, finished_at=2)
    assert queue.get(pending).finished_at == 2
    queue._mark(pending)

    with queue._connect() as conn:
        conn.execute("UPDATE jobs SET payload='[]' WHERE id=?", (pending,))
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (pending,)).fetchone()
    assert job_queue._row_to_record(row).payload == {}

    job_queue.reset_default_queue()
    assert job_queue.get_default_queue(tmp_path / "default.db") is job_queue.get_default_queue()
    job_queue.reset_default_queue()


def test_global_config_preferences_workspace_auth_and_recent_projects(monkeypatch, tmp_path):
    service = global_config_service.GlobalConfigService()
    service.settings = types.SimpleNamespace(global_root=str(tmp_path))
    service.ensure_structure()
    assert service.global_memory_path().is_file()
    assert service.auth_token_path().parent.name == "auth"
    assert service.ui_preferences_path().parent.name == "ui"
    assert service.workspace_state_path().parent.name == "state"

    prefs = service.write_ui_preferences({
        "theme": "dark", "language": "zh-CN", "sidebarWidth": 9999, "agentWidth": "bad",
        "fileFontSize": 24, "playerFontSize": 99, "leftPaneFontScale": 1,
        "rightPaneFontScale": 999, "workbenchMode": "invalid", "sidebarCollapsed": 1,
    })
    assert prefs["theme"] == "dark" and prefs["sidebarWidth"] <= 1200 and prefs["fileFontSize"] >= 10
    assert prefs["leftPaneFontScale"] == 75 and prefs["centerPaneFontScale"] == 150
    assert prefs["rightPaneFontScale"] == 150
    assert service._normalize_ui_preferences({"fileFontSize": 18})["centerPaneFontScale"] == 115
    assert service.read_ui_preferences()["sidebarCollapsed"] is True
    state = service.write_workspace_state({"recentProjects": "bad", "activeProjectRoot": str(tmp_path)})
    assert state["recentProjects"] == []
    for index in range(10):
        state = service.record_recent_project(project_name=f"P{index}", workspace_root=str(tmp_path / str(index)))
    assert len(state["recentProjects"]) == global_config_service.MAX_RECENT_PROJECTS
    state = service.record_recent_project(project_name="Newest", workspace_root=str(tmp_path / "9"))
    assert state["recentProjects"][0]["projectName"] == "Newest"

    assert service._clamp_int("bad", 5, minimum=1, maximum=10) == 5
    assert service._normalize_choice("bad", fallback="x", allowed={"x"}) == "x"
    assert service._read_json(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    assert service._read_json(bad) == {}
    service._write_json(tmp_path / "nested/data.json", {"x": 1})
    assert service._read_json(tmp_path / "nested/data.json") == {"x": 1}
    assert service._server_key("HTTPS://EXAMPLE.TEST/") == service._server_key("https://example.test")
    assert service._empty_auth_session()["accessToken"] == ""

    class FakeSecure:
        def encrypt_json(self, payload, *, user_id):
            return {"scheme": "fake", "userId": user_id, "payload": payload}

        def decrypt_json(self, payload, *, user_id):
            return payload["payload"]

    monkeypatch.setattr(service, "_secure_storage", lambda: FakeSecure())
    with pytest.raises(ValueError):
        service.write_auth_session({})
    saved = service.write_auth_session({"accessToken": "token", "userId": "u", "username": "name", "serverBaseUrl": "https://example.test", "user": {"id": "u"}})
    assert saved["accessToken"] == "token"
    assert service.read_auth_session()["userId"] == "u"
    assert service.find_auth_session_by_token("token")["userId"] == "u"
    assert service.find_auth_session_by_token("wrong") == service._empty_auth_session()
    service.clear_auth_session()
    assert service.read_auth_session() == service._empty_auth_session()
