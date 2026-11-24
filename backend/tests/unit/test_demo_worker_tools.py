from __future__ import annotations

import pytest

from app.sandbox.runner import ExecutionResult
from app.worker import node_worker
from app.worker.node_worker import NodeWorker


def assignment(**overrides):
    return {
        "agent_slug": "researcher-prime",
        "agent_owner_id": "demo-owner",
        "requester_id": "demo-owner",
        "task_id": "task-1",
        "lease_token": "lease-1",
        "entrypoint_type": "python",
        "entrypoint_config": {"code": "def run(payload): return {}"},
        "input_payload": {"query": "example search"},
        "resource_limits": {},
        **overrides,
    }


def worker_with_progress(monkeypatch):
    worker = NodeWorker(client=None, sandbox_backend="subprocess")

    async def no_progress(task_id: str, lease: str, message: str) -> None:
        return None

    monkeypatch.setattr(worker, "report_progress", no_progress)
    return worker


@pytest.mark.asyncio
async def test_researcher_worker_calls_real_search_tool_then_sandbox(monkeypatch):
    search_calls: list[str] = []
    sandbox_inputs: list[dict] = []

    async def fake_search(query: str, *, api_key: str):
        search_calls.append(query)
        return [{"title": "Result", "url": "https://example.org", "snippet": "Actual snippet"}]

    async def fake_run_python(self, code: str, payload: dict):
        sandbox_inputs.append(payload)
        return ExecutionResult(status="succeeded", output={"source_count": 1})

    monkeypatch.setattr(node_worker, "web_search", fake_search)
    monkeypatch.setattr(node_worker.SandboxRunner, "run_python", fake_run_python)
    worker = worker_with_progress(monkeypatch)

    outcome = await worker.execute(assignment())

    assert search_calls == ["example search"]
    assert sandbox_inputs[0]["search_results"][0]["snippet"] == "Actual snippet"
    assert outcome["success"] is True
    assert outcome["metrics"]["tool"] == "web_search"
    assert outcome["metrics"]["result_count"] == 1


@pytest.mark.asyncio
async def test_researcher_task_fails_explicitly_if_search_is_unconfigured(monkeypatch):
    async def missing_key(query: str, *, api_key: str):
        from app.worker.tools.web_search import WebSearchUnavailable

        raise WebSearchUnavailable("TAVILY_API_KEY не задан")

    monkeypatch.setattr(node_worker, "web_search", missing_key)
    worker = worker_with_progress(monkeypatch)

    outcome = await worker.execute(assignment())

    assert outcome["success"] is False
    assert "TAVILY_API_KEY" in outcome["error_message"]
    assert outcome["metrics"] == {"tool": "web_search", "status": "failed"}


@pytest.mark.asyncio
async def test_paid_search_tool_is_restricted_to_demo_agent_owner(monkeypatch):
    async def should_not_search(*args, **kwargs):
        raise AssertionError("Search must not run for another user's task")

    monkeypatch.setattr(node_worker, "web_search", should_not_search)
    worker = worker_with_progress(monkeypatch)

    outcome = await worker.execute(assignment(requester_id="different-user"))

    assert outcome["success"] is False
    assert outcome["metrics"] == {"tool": "web_search", "status": "forbidden"}
