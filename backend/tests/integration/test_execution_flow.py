"""Интеграционные тесты Execution Gateway: полный жизненный цикл задания.

Сценарий повторяет реальную работу узла-воркера:
  регистрация узла → подтверждение → heartbeat → claim → progress → result.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import (
    create_agent,
    create_published_agent,
    register_node_client,
    version_payload,
)


async def _submit(client, user, agent, **overrides):
    payload = {
        "agent_id": agent["id"],
        "input_payload": {"query": "Что такое Agent Civilization?"},
        "priority": 5,
        "timeout_seconds": 60,
        "max_attempts": 2,
    }
    payload.update(overrides)
    response = await client.post("/api/v1/tasks", json=payload, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_node_lifecycle_heartbeat_and_verification(client, user):
    node, node_client = await register_node_client(client, user)
    assert node["status"] == "pending"
    assert node["health"] == "unknown"

    profile = await node_client.request("GET", "/api/v1/nodes/me")
    assert profile.status_code == 200, profile.text
    assert profile.json()["node_key"] == node["node_key"]
    assert "secret_hash" not in profile.text

    verified = await node_client.request("POST", "/api/v1/nodes/me/verify")
    assert verified.status_code == 200
    assert verified.json()["status"] == "online"
    assert verified.json()["verified_at"] is not None

    heartbeat = await node_client.request(
        "POST",
        "/api/v1/nodes/me/heartbeat",
        {"sequence": 1, "running_tasks": 0, "cpu_load": 12.5, "memory_used_mb": 512},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["acknowledged"] is True

    history = await client.get(
        f"/api/v1/nodes/{node['id']}/heartbeats", headers=user.headers
    )
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert history.json()[0]["cpu_load"] == 12.5


async def test_full_task_execution_success(client, user):
    agent = await create_published_agent(client, user)
    node, node_client = await register_node_client(client, user)
    await node_client.request("POST", "/api/v1/nodes/me/verify")

    task = await _submit(client, user, agent)
    assert task["status"] == "queued"

    claimed = await node_client.request(
        "POST", "/api/v1/nodes/me/tasks/claim", {"capabilities": ["python"], "max_tasks": 1}
    )
    assert claimed.status_code == 200, claimed.text
    assignments = claimed.json()["assignments"]
    assert len(assignments) == 1
    assignment = assignments[0]
    assert assignment["task_id"] == task["id"]
    assert assignment["entrypoint_config"]["url"] == "https://agent.example.com/a2a"
    lease = assignment["lease_token"]

    progress = await node_client.request(
        "POST",
        f"/api/v1/nodes/me/tasks/{task['id']}/progress",
        {"lease_token": lease, "message": "обработка 50%"},
    )
    assert progress.status_code == 200
    assert progress.json()["status"] == "running"

    result = await node_client.request(
        "POST",
        f"/api/v1/nodes/me/tasks/{task['id']}/result",
        {
            "lease_token": lease,
            "success": True,
            "output": {"answer": "цивилизация агентов"},
            "cost_units": 1.5,
            "metrics": {"tokens": 120},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "succeeded"
    assert result.json()["will_retry"] is False

    detail = await client.get(f"/api/v1/tasks/{task['id']}", headers=user.headers)
    body = detail.json()
    assert body["status"] == "succeeded"
    assert body["output_payload"] == {"answer": "цивилизация агентов"}
    assert body["result_hash"] and len(body["result_hash"]) == 64
    assert float(body["cost_units"]) == 1.5

    events = await client.get(f"/api/v1/tasks/{task['id']}/events", headers=user.headers)
    types = [e["event_type"] for e in events.json()]
    assert types[0] == "task.queued"
    assert "task.leased" in types
    assert "task.progress" in types
    assert types[-1] == "task.succeeded"

    # Счётчики агента обновились, репутация пересчитана
    agent_after = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert agent_after.json()["completed_tasks"] == 1
    assert agent_after.json()["reputation_score"] > 50


async def test_failed_task_is_retried_then_fails(client, user):
    agent = await create_published_agent(client, user)
    _, node_client = await register_node_client(client, user)
    await node_client.request("POST", "/api/v1/nodes/me/verify")

    task = await _submit(client, user, agent, max_attempts=2)

    for attempt in (1, 2):
        claimed = await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 1})
        assignments = claimed.json()["assignments"]
        assert len(assignments) == 1, f"попытка {attempt}: задание должно вернуться в очередь"
        result = await node_client.request(
            "POST",
            f"/api/v1/nodes/me/tasks/{task['id']}/result",
            {"lease_token": assignments[0]["lease_token"], "success": False,
             "error_message": "агент упал"},
        )
        assert result.status_code == 200
        if attempt == 1:
            assert result.json()["will_retry"] is True
            assert result.json()["status"] == "queued"
        else:
            assert result.json()["will_retry"] is False
            assert result.json()["status"] == "failed"

    detail = await client.get(f"/api/v1/tasks/{task['id']}", headers=user.headers)
    assert detail.json()["status"] == "failed"
    assert detail.json()["attempts"] == 2
    assert "агент упал" in detail.json()["error"]["message"]

    agent_after = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert agent_after.json()["failed_tasks"] == 1


async def test_result_from_foreign_lease_is_rejected(client, user):
    """Fencing: узел не может завершить чужое задание или подделать lease."""
    agent = await create_published_agent(client, user)
    _, node_a = await register_node_client(client, user)
    _, node_b = await register_node_client(client, user)
    await node_a.request("POST", "/api/v1/nodes/me/verify")
    await node_b.request("POST", "/api/v1/nodes/me/verify")

    task = await _submit(client, user, agent)
    claimed = await node_a.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 1})
    lease = claimed.json()["assignments"][0]["lease_token"]

    stolen = await node_b.request(
        "POST",
        f"/api/v1/nodes/me/tasks/{task['id']}/result",
        {"lease_token": lease, "success": True, "output": {"hacked": True}},
    )
    assert stolen.status_code == 403
    assert stolen.json()["error"]["code"] == "task_not_yours"

    bad_lease = await node_a.request(
        "POST",
        f"/api/v1/nodes/me/tasks/{task['id']}/result",
        {"lease_token": "0" * 48, "success": True, "output": {}},
    )
    assert bad_lease.status_code == 422
    assert bad_lease.json()["error"]["code"] == "invalid_lease"


async def test_task_cannot_be_claimed_twice(client, user):
    agent = await create_published_agent(client, user)
    _, node_client = await register_node_client(client, user)
    await node_client.request("POST", "/api/v1/nodes/me/verify")

    await _submit(client, user, agent)
    first = await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 5})
    assert len(first.json()["assignments"]) == 1

    second = await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 5})
    assert second.json()["assignments"] == [], "задание не должно выдаваться дважды"


async def test_node_concurrency_limit_respected(client, user):
    agent = await create_published_agent(client, user)
    _, node_client = await register_node_client(client, user, max_concurrency=1)
    await node_client.request("POST", "/api/v1/nodes/me/verify")

    for _ in range(3):
        await _submit(client, user, agent)

    claimed = await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 3})
    assert len(claimed.json()["assignments"]) == 1


async def test_task_respects_node_resource_limits(client, user):
    """Задача с лимитами больше возможностей узла не выдаётся этому узлу."""
    agent = await create_published_agent(client, user)
    await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(
            publish=True,
            resource_limits={
                "max_cpu_seconds": 600,
                "max_wall_seconds": 1200,
                "max_memory_mb": 4096,
                "allow_network": False,
                "max_output_bytes": 65536,
            },
        ),
        headers=user.headers,
    )
    _, small_node = await register_node_client(
        client, user, limit_cpu_seconds=10, limit_wall_seconds=20, limit_memory_mb=256
    )
    await small_node.request("POST", "/api/v1/nodes/me/verify")

    await _submit(client, user, agent)
    claimed = await small_node.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 1})
    assert claimed.json()["assignments"] == []


async def test_cancel_task(client, user):
    agent = await create_published_agent(client, user)
    task = await _submit(client, user, agent)
    response = await client.post(
        f"/api/v1/tasks/{task['id']}/cancel", json={"reason": "передумал"}, headers=user.headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    again = await client.post(
        f"/api/v1/tasks/{task['id']}/cancel", json={}, headers=user.headers
    )
    assert again.status_code == 422


async def test_idempotency_key_prevents_duplicate_tasks(client, user):
    agent = await create_published_agent(client, user)
    key = f"idem-{uuid.uuid4().hex[:8]}"
    first = await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input_payload": {}, "idempotency_key": key},
        headers=user.headers,
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input_payload": {}, "idempotency_key": key},
        headers=user.headers,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_idempotency_key"


async def test_deadline_expiry_on_claim(client, user, session):
    import datetime as dt

    from sqlalchemy import update

    from app.db.models.task import Task

    agent = await create_published_agent(client, user)
    task = await _submit(client, user, agent)

    await session.execute(
        update(Task)
        .where(Task.id == uuid.UUID(task["id"]))
        .values(deadline_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
    )
    await session.commit()

    _, node_client = await register_node_client(client, user)
    await node_client.request("POST", "/api/v1/nodes/me/verify")
    claimed = await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 1})
    assert claimed.json()["assignments"] == []

    detail = await client.get(f"/api/v1/tasks/{task['id']}", headers=user.headers)
    assert detail.json()["status"] == "expired"


async def test_expire_leases_requeues_and_fails(client, user, session):
    import datetime as dt

    from sqlalchemy import update

    from app.db.models.task import Task
    from app.services import gateway

    agent = await create_published_agent(client, user)
    _, node_client = await register_node_client(client, user)
    await node_client.request("POST", "/api/v1/nodes/me/verify")
    task = await _submit(client, user, agent, max_attempts=2)
    await node_client.request("POST", "/api/v1/nodes/me/tasks/claim", {"max_tasks": 1})

    await session.execute(
        update(Task)
        .where(Task.id == uuid.UUID(task["id"]))
        .values(lease_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5))
    )
    await session.commit()

    requeued = await gateway.expire_leases(session)
    await session.commit()
    assert requeued == 1

    detail = await client.get(f"/api/v1/tasks/{task['id']}", headers=user.headers)
    assert detail.json()["status"] == "queued"
    assert detail.json()["error"]["type"] == "LeaseExpired"


async def test_platform_stats_reflect_activity(client, user):
    agent = await create_published_agent(client, user)
    await _submit(client, user, agent)
    stats = await client.get("/api/v1/platform/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["agents"]["published"] == 1
    assert body["tasks"]["total"] == 1
    assert body["users"] >= 1


async def test_unpublished_agent_cannot_receive_tasks(client, user):
    agent = await create_agent(client, user)
    await client.post(
        f"/api/v1/agents/{agent['id']}/versions", json=version_payload(), headers=user.headers
    )
    response = await client.post(
        "/api/v1/tasks", json={"agent_id": agent["id"], "input_payload": {}}, headers=user.headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "agent_not_published"


@pytest.mark.parametrize("paused_status", ["suspended", "archived"])
async def test_paused_agent_rejects_new_tasks(client, user, paused_status):
    agent = await create_published_agent(client, user)
    path = "/suspend" if paused_status == "suspended" else "/archive"
    body = {"reason": "пауза"} if paused_status == "suspended" else {}
    await client.post(f"/api/v1/agents/{agent['id']}{path}", json=body, headers=user.headers)

    response = await client.post(
        "/api/v1/tasks", json={"agent_id": agent["id"], "input_payload": {}}, headers=user.headers
    )
    assert response.status_code == 422
