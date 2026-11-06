"""Тесты доставки A2A-сообщений: очередь, ретраи, стратегии воркера, MCP-клиент."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.core.config import settings
from app.db.models.messaging import MessageStatus
from app.sandbox.mcp import McpClient, McpError, call_mcp_tool, sync_tools
from app.worker.message_worker import MessageDispatcher, MessageWorker
from tests.conftest import create_published_agent

SERVICE_TOKEN = "test-service-token"


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_service_token", SERVICE_TOKEN)
    yield


async def _agents(client, user):
    sender = await create_published_agent(client, user, name="Отправитель", slug="dlv-a")
    recipient = await create_published_agent(client, user, name="Получатель", slug="dlv-b")
    return sender, recipient


async def _send(client, user, sender, recipient, **overrides):
    payload = {"recipient_agent_id": recipient["id"], "payload": {"n": 1}}
    payload.update(overrides)
    response = await client.post(
        f"/api/v1/agents/{sender['id']}/messages", json=payload, headers=user.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# ── Внутренний API: доступ ────────────────────────────────────────────
async def test_internal_api_requires_service_token(client, session, user):
    sender, recipient = await _agents(client, user)
    await _send(client, user, sender, recipient)

    anonymous = await client.post("/api/v1/internal/messages/claim", json={"limit": 5})
    assert anonymous.status_code == 403
    assert anonymous.json()["error"]["code"] == "invalid_service_token"

    wrong = await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 5},
        headers={"X-Service-Token": "nope"},
    )
    assert wrong.status_code == 403


async def test_internal_api_disabled_when_token_not_configured(client, user, monkeypatch):
    sender, recipient = await _agents(client, user)
    await _send(client, user, sender, recipient)
    monkeypatch.setattr(settings, "internal_service_token", "")

    response = await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 5},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "internal_api_disabled"


# ── Claim / result ────────────────────────────────────────────────────
async def test_claim_returns_message_with_assignment(client, session, user):
    sender, recipient = await _agents(client, user)
    created = await _send(client, user, sender, recipient)

    response = await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 10},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert len(messages) == 1
    claimed = messages[0]
    assert claimed["id"] == created["message"]["id"]
    assert claimed["strategy"] == "http"
    assert claimed["payload"] == {"n": 1}
    assert claimed["assignment"]["entrypoint_type"] == "http"
    assert claimed["assignment"]["entrypoint_config"]["url"]

    # Повторный claim не отдаёт то же сообщение дважды
    again = await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 10},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert again.json()["messages"] == []


async def test_successful_delivery_marks_message_delivered(client, session, user):
    sender, recipient = await _agents(client, user)
    created = await _send(client, user, sender, recipient)
    message_id = created["message"]["id"]

    await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 10},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    result = await client.post(
        f"/api/v1/internal/messages/{message_id}/result",
        json={
            "success": True,
            "response_payload": {"answer": 42},
            "latency_ms": 12,
        },
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "delivered"
    assert result.json()["retry_scheduled"] is False

    details = await client.get(f"/api/v1/messages/{message_id}", headers=user.headers)
    assert details.json()["status"] == "delivered"
    assert details.json()["response_payload"] == {"answer": 42}
    assert details.json()["latency_ms"] == 12


async def test_failed_delivery_schedules_retry_then_exhausts(client, session, user):
    sender, recipient = await _agents(client, user)
    created = await _send(client, user, sender, recipient, max_attempts=2)
    message_id = created["message"]["id"]

    # Первая неудача → возврат в pending с backoff
    await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 10},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    first = await client.post(
        f"/api/v1/internal/messages/{message_id}/result",
        json={"success": False, "error": {"type": "Timeout", "message": "нет ответа"}},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert first.json()["retry_scheduled"] is True
    assert first.json()["status"] == "pending"

    details = await client.get(f"/api/v1/messages/{message_id}", headers=user.headers)
    body = details.json()
    assert body["attempts"] == 1
    assert body["error"]["type"] == "Timeout"
    # Пока next_attempt_at в будущем, сообщение не забирается
    assert body["status"] == "pending"

    # Принудительно «созреваем» backoff
    from sqlalchemy import update

    from app.db.models.messaging import AgentMessage

    await session.execute(
        update(AgentMessage)
        .where(AgentMessage.id == uuid.UUID(message_id))
        .values(next_attempt_at=None)
    )
    await session.commit()

    await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 10},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    second = await client.post(
        f"/api/v1/internal/messages/{message_id}/result",
        json={"success": False, "error": {"type": "Timeout", "message": "снова нет"}},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert second.json()["retry_scheduled"] is False
    assert second.json()["status"] == "failed"


async def test_terminal_message_cannot_be_reported_twice(client, user):
    sender, recipient = await _agents(client, user)
    message_id = (await _send(client, user, sender, recipient))["message"]["id"]
    await client.post(
        "/api/v1/internal/messages/claim",
        json={"limit": 10},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    await client.post(
        f"/api/v1/internal/messages/{message_id}/result",
        json={"success": True},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    twice = await client.post(
        f"/api/v1/internal/messages/{message_id}/result",
        json={"success": True},
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert twice.status_code == 422
    assert twice.json()["error"]["code"] == "message_terminal"


async def test_expired_messages_are_marked(client, session, user):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.db.models.messaging import AgentMessage

    sender, recipient = await _agents(client, user)
    message_id = (await _send(client, user, sender, recipient))["message"]["id"]
    await session.execute(
        update(AgentMessage)
        .where(AgentMessage.id == uuid.UUID(message_id))
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()

    response = await client.post(
        "/api/v1/internal/messages/expire", headers={"X-Service-Token": SERVICE_TOKEN}
    )
    assert response.status_code == 200
    assert "expired=1" in response.json()["detail"]

    details = await client.get(f"/api/v1/messages/{message_id}", headers=user.headers)
    assert details.json()["status"] == MessageStatus.expired.value


# ── Диспетчер: стратегии ──────────────────────────────────────────────
async def test_dispatcher_http_strategy_posts_payload():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer key-1"
        return httpx.Response(200, json={"output": {"echo": True}})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    import app.worker.message_worker as mw

    mw.httpx.AsyncClient = PatchedClient
    try:
        dispatcher = MessageDispatcher()
        outcome = await dispatcher.deliver(
            {"strategy": "http", "payload": {"q": "hi"}},
            {
                "entrypoint_type": "http",
                "entrypoint_config": {"url": "https://agent.example.com/run", "api_key": "key-1"},
            },
        )
    finally:
        mw.httpx.AsyncClient = original

    assert outcome["success"] is True
    assert outcome["response_payload"] == {"echo": True}
    assert calls == [{"input": {"q": "hi"}}]


async def test_dispatcher_http_failure_reports_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="busy"))

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    import app.worker.message_worker as mw

    original = mw.httpx.AsyncClient
    mw.httpx.AsyncClient = PatchedClient
    try:
        outcome = await MessageDispatcher().deliver(
            {"strategy": "http", "payload": {}},
            {"entrypoint_config": {"url": "https://agent.example.com/run"}},
        )
    finally:
        mw.httpx.AsyncClient = original

    assert outcome["success"] is False
    assert outcome["error"]["type"] == "AgentHttpError"
    assert "503" in outcome["error"]["message"]


async def test_dispatcher_sandbox_runs_code_isolated():
    code = "def run(payload):\n    return {'doubled': payload['n'] * 2}\n"
    outcome = await MessageDispatcher(sandbox_backend="subprocess").deliver(
        {"strategy": "sandbox", "payload": {"n": 21}},
        {"entrypoint_config": {"code": code}, "resource_limits": {"max_wall_seconds": 10}},
    )
    assert outcome["success"] is True, outcome
    assert outcome["response_payload"] == {"doubled": 42}


async def test_dispatcher_sandbox_reports_agent_error():
    code = "def run(payload):\n    raise ValueError('boom')\n"
    outcome = await MessageDispatcher(sandbox_backend="subprocess").deliver(
        {"strategy": "sandbox", "payload": {}},
        {"entrypoint_config": {"code": code}},
    )
    assert outcome["success"] is False
    assert "boom" in json.dumps(outcome["error"], ensure_ascii=False)


async def test_dispatcher_inbox_is_noop():
    outcome = await MessageDispatcher().deliver({"strategy": "inbox", "payload": {}}, {})
    assert outcome["success"] is True
    assert outcome["response_payload"] == {}


async def test_dispatcher_rejects_bad_http_endpoint():
    outcome = await MessageDispatcher().deliver(
        {"strategy": "http", "payload": {}}, {"entrypoint_config": {"url": "ftp://nope"}}
    )
    assert outcome["success"] is False
    assert outcome["error"]["type"] == "BadEndpoint"


# ── Воркер: цикл ──────────────────────────────────────────────────────
class _FakeInternalClient:
    def __init__(self, batch, *, retry_on_failure: bool = True):
        self.batch = batch
        self.retry_on_failure = retry_on_failure
        self.reported: list[dict] = []

    async def claim(self, *, limit=10):
        batch, self.batch = self.batch, {"messages": []}
        return batch

    async def close(self):
        return None

    async def report(self, message_id, **kwargs):
        self.reported.append({"id": message_id, **kwargs})
        return {"retry_scheduled": self.retry_on_failure and not kwargs["success"]}


async def test_worker_processes_batch_and_reports():
    batch = {
        "messages": [
            {
                "id": "m-1",
                "strategy": "inbox",
                "payload": {},
                "assignment": {},
            },
            {
                "id": "m-2",
                "strategy": "http",
                "payload": {},
                "assignment": {"entrypoint_config": {"url": "ftp://bad"}},
            },
        ]
    }
    client = _FakeInternalClient(batch)
    worker = MessageWorker(client, batch_size=5)

    outcome = await worker.run_once()
    # Отказ с запланированным повтором — это retry, а не терминальный failed
    assert outcome == {"processed": 2, "delivered": 1, "failed": 0, "retried": 1}
    assert [item["id"] for item in client.reported] == ["m-1", "m-2"]
    assert client.reported[0]["success"] is True
    assert client.reported[1]["error"]["type"] == "BadEndpoint"


async def test_worker_loop_stops_on_signal_flag():
    client = _FakeInternalClient({"messages": []})
    worker = MessageWorker(client, poll_interval=0.01)

    import asyncio

    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, timeout=2)
    assert worker.delivered == 0


# ── MCP-клиент ────────────────────────────────────────────────────────
def _mcp_transport(routes: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        if method not in routes:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32601, "message": "no method"}})
        payload = routes[method]
        if callable(payload):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": payload(body)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": payload})

    return httpx.MockTransport(handler)


async def test_mcp_client_lists_tools_and_calls_tool():
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        body = json.loads(request.content)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
        elif body["method"] == "tools/list":
            result = {
                "tools": [
                    {"name": "search", "description": "Поиск", "inputSchema": {"type": "object"}},
                    {"name": "fetch", "description": "Загрузка"},
                ]
            }
        elif body["method"] == "tools/call":
            assert body["params"]["name"] == "search"
            result = {"content": [{"type": "text", "text": "{\"found\": 3}"}]}
        else:  # pragma: no cover
            result = {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    transport = httpx.MockTransport(handler)
    async with McpClient(
        "https://mcp.example.com/mcp", auth_token="tok", transport=transport
    ) as client:
        init = await client.initialize()
        assert init["protocolVersion"] == "2024-11-05"
        tools = await client.list_tools()
        assert [tool["name"] for tool in tools] == ["search", "fetch"]
        result = await client.call_tool_as_json("search", {"q": "x"})
        assert result["output"] == {"found": 3}

    assert seen_auth and all(value == "Bearer tok" for value in seen_auth)


async def test_mcp_client_raises_on_rpc_error():
    transport = _mcp_transport({})
    async with McpClient("https://mcp.example.com/mcp", transport=transport) as client:
        with pytest.raises(McpError):
            await client.list_tools()


async def test_mcp_client_raises_on_http_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with pytest.raises(McpError):
        await call_mcp_tool(
            server_url="https://mcp.example.com/mcp",
            tool_name="x",
            transport=transport,
        )


async def test_mcp_sync_tools_helper():
    transport = _mcp_transport(
        {
            "initialize": {"protocolVersion": "2024-11-05"},
            "tools/list": {"tools": [{"name": "only", "description": "d", "inputSchema": {}}]},
        }
    )
    tools = await sync_tools(server_url="https://mcp.example.com/mcp", transport=transport)
    assert tools == [{"name": "only", "description": "d", "input_schema": {}}]


async def test_dispatcher_mcp_without_server_fails():
    outcome = await MessageDispatcher().deliver(
        {"strategy": "mcp", "payload": {"tool": "x"}}, {"mcp": None}
    )
    assert outcome["success"] is False
    assert outcome["error"]["type"] == "McpMissing"


async def test_worker_counts_terminal_failures():
    client = _FakeInternalClient(
        {
            "messages": [
                {"id": "m-3", "strategy": "http", "payload": {},
                 "assignment": {"entrypoint_config": {"url": "ftp://bad"}}}
            ]
        },
        retry_on_failure=False,
    )
    worker = MessageWorker(client)
    outcome = await worker.run_once()
    assert outcome == {"processed": 1, "delivered": 0, "failed": 1, "retried": 0}
    assert worker.failed == 1


async def test_mcp_errors_are_counted_by_type():
    """Этап 10, G: отказы MCP видны в метриках по типу, без токена в метке."""
    from app.core.metrics import metrics

    before = dict(metrics.events)
    dispatcher = MessageDispatcher()
    # MCP-сервер не зарегистрирован — отказ до сетевого вызова
    outcome = await dispatcher.deliver(
        {"strategy": "mcp", "payload": {"tool": "x"}}, {"mcp": None}
    )
    assert outcome["success"] is False

    def delta(outcome_name: str) -> int:
        return metrics.events[("mcp_error", outcome_name)] - before.get(
            ("mcp_error", outcome_name), 0
        )

    assert delta("McpMissing") == 1
    assert metrics.durations["mcp_tool_call"].count >= 1
