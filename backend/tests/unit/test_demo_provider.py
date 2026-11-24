from __future__ import annotations

import pytest

from app.worker.demo_node_worker import DemoNodeWorker, _check_python_syntax
from app.worker.demo_provider import DEMO_AGENT_SLUGS, run_demo_agent


def test_demo_provider_builds_python_as_data_and_syntax_tool_checks_it():
    result = run_demo_agent(
        "demo-tkinter-builder",
        {"input": {"prompt": "Investor task manager"}, "depends_on_outputs": {}},
    )

    assert result["filename"] == "app.py"
    assert "Investor task manager" in result["source_code"]
    assert _check_python_syntax(result["source_code"]) == {"status": "passed"}
    assert len(DEMO_AGENT_SLUGS) == 10


def test_demo_provider_rejects_any_non_seeded_agent():
    with pytest.raises(ValueError, match="allow-list"):
        run_demo_agent("arbitrary-agent", {})


@pytest.mark.asyncio
async def test_render_demo_worker_never_executes_registry_python(monkeypatch):
    worker = DemoNodeWorker(client=None)

    async def no_progress(task_id: str, lease: str, message: str) -> None:
        return None

    monkeypatch.setattr(worker, "report_progress", no_progress)
    outcome = await worker.execute(
        {
            "agent_slug": "demo-tkinter-builder",
            "task_id": "task-id",
            "lease_token": "lease-token",
            "entrypoint_type": "python",
            "entrypoint_config": {"code": "raise RuntimeError('must not run')"},
            "input_payload": {"input": {"prompt": "safe demo"}},
            "resource_limits": {},
        }
    )

    assert outcome["success"] is True
    assert outcome["output"]["demo_provider"] == "deterministic"
    assert outcome["output"]["syntax_check"] == {"status": "passed"}
    assert outcome["output"]["tool_calls"] == [{"name": "python.syntax_check", "status": "passed"}]
    assert "raise RuntimeError" not in outcome["output"]["source_code"]


@pytest.mark.asyncio
async def test_demo_provider_web_search_requires_owner_and_returns_provider_results(monkeypatch):
    worker = DemoNodeWorker(client=None)
    progress: list[str] = []

    async def report_progress(task_id: str, lease: str, message: str) -> None:
        progress.append(message)

    async def fake_search(query: str, *, api_key: str):
        return [{"title": "Observed result", "url": "https://example.org", "snippet": query}]

    from app.worker import demo_node_worker

    monkeypatch.setattr(worker, "report_progress", report_progress)
    monkeypatch.setattr(demo_node_worker, "web_search", fake_search)
    outcome = await worker.execute(
        {
            "agent_slug": "researcher-prime",
            "agent_owner_id": "account-1",
            "requester_id": "account-1",
            "task_id": "task-id",
            "lease_token": "lease-token",
            "input_payload": {"query": "real query"},
        }
    )

    assert outcome["success"] is True
    assert outcome["output"]["sources"][0]["title"] == "Observed result"
    assert outcome["output"]["tool_calls"] == [
        {"name": "web_search", "status": "succeeded", "result_count": 1}
    ]
    assert outcome["metrics"]["result_count"] == 1
    assert progress == ["tool_call: web_search"]
