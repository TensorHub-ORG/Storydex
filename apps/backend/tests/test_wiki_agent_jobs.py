from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import routes_wiki
from core.exceptions import StorydexError
from services.execution_coordinator import ExecutionCoordinator


@pytest.fixture(autouse=True)
def isolated_wiki_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    coordinator = ExecutionCoordinator()
    monkeypatch.setattr(routes_wiki, "project_service", SimpleNamespace(workspace_root=tmp_path))
    monkeypatch.setattr(routes_wiki, "execution_coordinator", coordinator, raising=False)
    reset = getattr(routes_wiki, "_reset_wiki_agent_jobs_for_tests", None)
    if callable(reset):
        reset()
    yield coordinator
    if callable(reset):
        reset()


async def _wait_for_job(job_id: str, expected_status: str) -> dict:
    for _ in range(100):
        response = routes_wiki.read_agent_wiki_job(job_id)
        payload = response.data
        if payload["status"] == expected_status:
            return payload
        await asyncio.sleep(0)
    raise AssertionError(f"wiki job {job_id} did not reach {expected_status}")


def test_wiki_job_submit_returns_immediately_and_releases_execution_slot(
    monkeypatch: pytest.MonkeyPatch,
    isolated_wiki_jobs: ExecutionCoordinator,
) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()

        async def fake_workflow(_workspace_root, *, workflow, agent_runner):
            assert workflow == "generate_wiki"
            assert agent_runner is routes_wiki._run_coomi_wiki_agent
            await gate.wait()
            return {"summary": "生成完成", "fallbackUsed": False, "wiki": {"entries": []}}

        monkeypatch.setattr(routes_wiki.story_wiki_service, "run_agent_workflow", fake_workflow)
        submission = asyncio.create_task(routes_wiki.agent_generate_story_wiki())
        await asyncio.sleep(0)
        try:
            assert submission.done(), "submission must not wait for the full Agent workflow"
        finally:
            gate.set()
        response = await submission
        assert response.data["status"] == "running"
        job_id = response.data["jobId"]

        completed = await _wait_for_job(job_id, "completed")
        assert completed["result"]["summary"] == "生成完成"
        assert isolated_wiki_jobs.try_reserve() is True
        isolated_wiki_jobs.release_reservation()

    asyncio.run(scenario())


def test_wiki_job_rejects_duplicate_workspace_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()

        async def fake_workflow(_workspace_root, *, workflow, agent_runner):
            await gate.wait()
            return {"summary": workflow, "fallbackUsed": False, "wiki": {"entries": []}}

        monkeypatch.setattr(routes_wiki.story_wiki_service, "run_agent_workflow", fake_workflow)
        first = await routes_wiki.agent_generate_story_wiki()
        with pytest.raises(StorydexError) as exc_info:
            await routes_wiki.agent_update_story_wiki()
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "wiki_agent_job_running"
        gate.set()
        await _wait_for_job(first.data["jobId"], "completed")

    asyncio.run(scenario())


def test_wiki_job_returns_readable_agent_busy_error(
    isolated_wiki_jobs: ExecutionCoordinator,
) -> None:
    assert isolated_wiki_jobs.try_reserve() is True
    try:
        with pytest.raises(StorydexError) as exc_info:
            asyncio.run(routes_wiki.agent_generate_story_wiki())
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "agent_busy"
        assert "Agent 正忙" in exc_info.value.message
    finally:
        isolated_wiki_jobs.release_reservation()


def test_wiki_job_exposes_background_failure_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
    isolated_wiki_jobs: ExecutionCoordinator,
) -> None:
    async def fake_workflow(_workspace_root, *, workflow, agent_runner):
        raise RuntimeError("persist failed")

    monkeypatch.setattr(routes_wiki.story_wiki_service, "run_agent_workflow", fake_workflow)

    async def scenario() -> None:
        submitted = await routes_wiki.agent_review_story_wiki()
        failed = await _wait_for_job(submitted.data["jobId"], "failed")
        assert failed["errorMessage"] == "persist failed"
        assert isolated_wiki_jobs.try_reserve() is True
        isolated_wiki_jobs.release_reservation()

    asyncio.run(scenario())
