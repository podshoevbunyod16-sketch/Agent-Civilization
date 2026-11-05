"""E2E: настоящий worker (app.worker.node_worker) против настоящего API.

Проверяется сквозной путь: публикация python-агента → регистрация узла →
worker забирает задание → исполняет код в песочнице → отдаёт результат.
"""

from __future__ import annotations

import httpx

from app.worker.node_worker import NodeClient, NodeWorker
from tests.conftest import register_node_client, version_payload

PYTHON_AGENT_CODE = """
def run(payload):
    text = payload.get("text", "")
    return {
        "length": len(text),
        "upper": text.upper(),
        "words": len(text.split()),
    }
"""


async def _publish_python_agent(client, user):
    response = await client.post(
        "/api/v1/agents",
        json={
            "slug": "text-tools-py",
            "name": "Текстовый агент",
            "summary": "Считает статистику текста в изолированной песочнице",
            "category": "text",
            "capabilities": [{"capability_key": "text.analyze", "declared_level": 2}],
        },
        headers=user.headers,
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(
            publish=True,
            entrypoint_type="python",
            entrypoint_config={"code": PYTHON_AGENT_CODE, "runtime": "python3.12"},
            resource_limits={
                "max_cpu_seconds": 5,
                "max_wall_seconds": 10,
                "max_memory_mb": 256,
                "allow_network": False,
                "max_output_bytes": 65536,
            },
        ),
        headers=user.headers,
    )
    assert version.status_code == 201, version.text
    detail = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    return detail.json()


async def test_worker_executes_python_agent_end_to_end(client, app, user):
    agent = await _publish_python_agent(client, user)
    node, node_http = await register_node_client(client, user)

    worker_client = NodeClient(
        "http://testserver",
        node_http.node_key,
        # секрет известен только клиенту: восстанавливаем из key_hex
        "",
        transport=httpx.ASGITransport(app=app),
    )
    # NodeClient ожидает секрет; для теста подставляем готовый hmac-ключ
    worker_client.key_hex = node_http.key_hex

    worker = NodeWorker(worker_client, poll_interval=0.01, max_tasks=1)

    task = await client.post(
        "/api/v1/tasks",
        json={
            "agent_id": agent["id"],
            "input_payload": {"text": "агент цивилизация работает"},
            "timeout_seconds": 30,
        },
        headers=user.headers,
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]

    await worker.verify()
    processed = await worker.run_once()
    assert processed == 1

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=user.headers)
    body = detail.json()
    assert body["status"] == "succeeded", body
    assert body["output_payload"]["length"] == len("агент цивилизация работает")
    assert body["output_payload"]["words"] == 3
    assert body["output_payload"]["upper"].isupper()

    agent_after = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert agent_after.json()["completed_tasks"] == 1
    await worker_client.close()


async def test_worker_reports_agent_crash_as_failed_task(client, app, user):
    response = await client.post(
        "/api/v1/agents",
        json={
            "slug": "crashing-agent",
            "name": "Падающий агент",
            "summary": "Всегда выбрасывает исключение для проверки обработки ошибок",
            "capabilities": [{"capability_key": "test.crash", "declared_level": 1}],
        },
        headers=user.headers,
    )
    agent = response.json()
    await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(
            publish=True,
            entrypoint_type="python",
            entrypoint_config={"code": "def run(payload):\n    raise RuntimeError('boom')"},
        ),
        headers=user.headers,
    )

    _, node_http = await register_node_client(client, user)
    worker_client = NodeClient(
        "http://testserver", node_http.node_key, "", transport=httpx.ASGITransport(app=app)
    )
    worker_client.key_hex = node_http.key_hex
    worker = NodeWorker(worker_client, max_tasks=1)

    task = await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input_payload": {}, "max_attempts": 1},
        headers=user.headers,
    )
    task_id = task.json()["id"]

    await worker.verify()
    assert await worker.run_once() == 1

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=user.headers)
    assert detail.json()["status"] == "failed"
    assert "boom" in detail.json()["error"]["message"]
    await worker_client.close()


async def test_worker_heartbeat_loop_and_stop(client, app, user):
    import asyncio

    _, node_http = await register_node_client(client, user)
    worker_client = NodeClient(
        "http://testserver", node_http.node_key, "", transport=httpx.ASGITransport(app=app)
    )
    worker_client.key_hex = node_http.key_hex
    worker = NodeWorker(worker_client, poll_interval=0.01)

    async def stop_soon():
        await asyncio.sleep(0.3)
        worker.stop()

    await asyncio.gather(worker.run_forever(), stop_soon())
    assert worker._executed == 0
    node = await client.get("/api/v1/nodes", headers=user.headers)
    assert node.json()["items"][0]["status"] == "online"
    assert node.json()["items"][0]["last_seen_at"] is not None
