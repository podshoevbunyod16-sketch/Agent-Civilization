"""Тесты безопасности и прав доступа (этап 1, правило №6)."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import create_agent, create_published_agent, register_user


async def test_anonymous_cannot_modify_registry(client):
    agent = await create_agent(client, await register_user(client))
    assert (await client.patch(f"/api/v1/agents/{agent['id']}", json={"name": "x"})).status_code == 401
    assert (await client.post(f"/api/v1/agents/{agent['id']}/publish", json={"publish": True})).status_code == 401
    assert (await client.post(f"/api/v1/agents/{agent['id']}/versions", json={})).status_code == 401


async def test_user_cannot_publish_or_suspend_foreign_agent(client, user, other_user):
    agent = await create_agent(client, user)
    agent_id = agent["id"]
    for path, body in [
        ("/publish", {"publish": True}),
        ("/suspend", {"reason": "hack"}),
        ("/resume", {}),
        ("/archive", {}),
    ]:
        response = await client.post(
            f"/api/v1/agents/{agent_id}{path}", json=body, headers=other_user.headers
        )
        assert response.status_code in (403, 404), f"{path}: {response.text}"


async def test_user_cannot_add_version_to_foreign_agent(client, user, other_user):
    agent = await create_agent(client, user)
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={"entrypoint_type": "http", "entrypoint_config": {"url": "https://evil.example.com"}},
        headers=other_user.headers,
    )
    assert response.status_code == 403


async def test_capability_registration_requires_admin(client, user):
    response = await client.post(
        "/api/v1/capabilities",
        json={"key": "custom.skill", "name": "Скилл"},
        headers=user.headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_agent_verification_requires_admin(client, user):
    agent = await create_published_agent(client, user)
    forbidden = await client.post(
        f"/api/v1/agents/{agent['id']}/verify", headers=user.headers
    )
    assert forbidden.status_code == 403


async def test_audit_log_is_scoped_for_non_admin(client, user, other_user):
    await create_agent(client, user)
    await create_agent(client, other_user)

    # не-админ не может увидеть чужие записи, даже передав all_entries
    response = await client.get(
        "/api/v1/audit", params={"all_entries": True}, headers=other_user.headers
    )
    assert response.status_code == 200
    actors = {item["actor_id"] for item in response.json()["items"]}
    assert actors <= {other_user.id}


async def test_admin_sees_all_audit_entries(client, user, admin_user):
    await create_agent(client, user)
    response = await client.get(
        "/api/v1/audit", params={"all_entries": True}, headers=admin_user.headers
    )
    assert response.status_code == 200
    actors = {item["actor_id"] for item in response.json()["items"]}
    assert user.id in actors


async def test_inactive_user_token_rejected(client, user, session):
    from sqlalchemy import update

    from app.db.models.user import User

    await session.execute(
        update(User).where(User.id == uuid.UUID(user.id)).values(is_active=False)
    )
    await session.commit()
    response = await client.get("/api/v1/auth/me", headers=user.headers)
    assert response.status_code == 401


async def test_sql_injection_attempt_in_search_is_safe(client, user):
    await create_published_agent(client, user)
    payloads = [
        "' OR 1=1 --",
        "'; DROP TABLE agents; --",
        "%' OR name LIKE '%",
    ]
    for payload in payloads:
        response = await client.get("/api/v1/agents", params={"q": payload})
        assert response.status_code == 200, payload
        assert response.json()["items"] == [], payload
    # таблица на месте
    assert (await client.get("/api/v1/agents")).status_code == 200


async def test_invalid_uuid_in_path_returns_422(client, user):
    response = await client.get("/api/v1/agents/not-a-uuid", headers=user.headers)
    assert response.status_code == 422


async def test_error_responses_do_not_leak_internals(client):
    response = await client.get("/api/v1/agents/00000000-0000-0000-0000-000000000000")
    body = response.json()
    assert set(body["error"]) == {"code", "message", "details", "request_id"}
    assert "Traceback" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


async def test_node_endpoints_require_signature(client):
    for path in ("/api/v1/nodes/me", "/api/v1/nodes/me/heartbeat"):
        response = await client.get(path) if path.endswith("me") else await client.post(path, json={})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "missing_node_signature"


async def test_node_signature_replay_rejected(client, user):
    from tests.conftest import register_node_client

    _, node = await register_node_client(client, user)
    import json

    body = {"sequence": 1, "health": "healthy", "running_tasks": 0}
    raw = json.dumps(body).encode()
    headers = node.headers("POST", "/api/v1/nodes/me/heartbeat", raw)
    headers.pop("_json", None)
    headers.pop("X-Body-Length", None)

    first = await client.post("/api/v1/nodes/me/heartbeat", content=raw, headers=headers)
    assert first.status_code == 200, first.text

    # Тот же nonce и та же подпись повторно — replay-атака
    replay = await client.post("/api/v1/nodes/me/heartbeat", content=raw, headers=headers)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "replay_detected"


async def test_node_signature_rejects_wrong_secret_and_stale_timestamp(client, user):
    import time

    from app.core.security import build_signing_payload
    from tests.conftest import NodeClient, register_node_client

    node_data, _ = await register_node_client(client, user)

    # Неверный секрет
    attacker = NodeClient(client, node_data["node_key"], "attacker-secret-value")
    response = await attacker.request("GET", "/api/v1/nodes/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"

    # Устаревший timestamp
    import hashlib
    import hmac as hmac_mod
    import uuid as _uuid

    secret = "attacker-secret-value"
    timestamp = str(int(time.time()) - 600)
    nonce = _uuid.uuid4().hex
    payload = build_signing_payload(
        node_key=node_data["node_key"], method="GET", path="/nodes/me",
        timestamp=timestamp, nonce=nonce, body=b"",
    )
    signature = hmac_mod.new(hashlib.sha256(secret.encode()).digest(), payload, hashlib.sha256).hexdigest()
    stale = await client.get(
        "/api/v1/nodes/me",
        headers={
            "X-Node-Key": node_data["node_key"],
            "X-Node-Signature": signature,
            "X-Node-Timestamp": timestamp,
            "X-Node-Nonce": nonce,
        },
    )
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "signature_expired"


async def test_revoked_node_cannot_operate(client, user):
    from tests.conftest import register_node_client

    node_data, node = await register_node_client(client, user)
    await client.post(
        f"/api/v1/nodes/{node_data['id']}/revoke", headers=user.headers
    )
    response = await node.request("GET", "/api/v1/nodes/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "node_revoked"


async def test_other_user_cannot_manage_node(client, user, other_user):
    from tests.conftest import register_node_client

    node_data, _ = await register_node_client(client, user)
    response = await client.patch(
        f"/api/v1/nodes/{node_data['id']}",
        json={"name": "hijacked"},
        headers=other_user.headers,
    )
    assert response.status_code == 403

    listing = await client.get("/api/v1/nodes", headers=other_user.headers)
    assert listing.json()["items"] == []


async def test_user_cannot_submit_task_for_foreign_draft(client, user, other_user):
    agent = await create_agent(client, user)
    response = await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input_payload": {}},
        headers=other_user.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "agent_not_published"


async def test_task_visibility_is_restricted(client, user, other_user):
    agent = await create_published_agent(client, user)
    task = await client.post(
        "/api/v1/tasks",
        json={"agent_id": agent["id"], "input_payload": {"q": "x"}},
        headers=user.headers,
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]

    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=other_user.headers)).status_code == 403
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=user.headers)).status_code == 200
    assert (await client.get("/api/v1/tasks", headers=other_user.headers)).json()["items"] == []


@pytest.mark.parametrize(
    "header",
    [
        {"X-Node-Key": "node_x", "X-Node-Signature": "a" * 64, "X-Node-Timestamp": "1", "X-Node-Nonce": "nonce-123456"},
    ],
)
async def test_unknown_node_key_rejected(client, header):
    response = await client.get("/api/v1/nodes/me", headers=header)
    assert response.status_code == 401
