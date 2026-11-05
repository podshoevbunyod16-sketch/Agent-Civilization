"""API-тесты A2A-коммуникации: диалоги, сообщения, идемпотентность, права."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import create_published_agent, version_payload


async def _two_agents(client, user):
    """Два опубликованных агента одного владельца."""
    alpha = await create_published_agent(
        client, user, name="Агент-отправитель", slug="msg-alpha"
    )
    beta = await create_published_agent(
        client, user, name="Агент-получатель", slug="msg-beta"
    )
    return alpha, beta


async def _send(client, user, sender, recipient, **overrides):
    payload = {
        "recipient_agent_id": recipient["id"],
        "kind": "request",
        "payload": {"question": "Как дела?"},
    }
    payload.update(overrides)
    return await client.post(
        f"/api/v1/agents/{sender['id']}/messages", json=payload, headers=user.headers
    )


# ── Отправка ──────────────────────────────────────────────────────────
async def test_send_message_creates_conversation_and_queues_delivery(client, user):
    sender, recipient = await _two_agents(client, user)
    response = await _send(client, user, sender, recipient)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_duplicate"] is False
    message = body["message"]
    assert message["status"] == "pending"
    assert message["kind"] == "request"
    assert message["sender_agent_id"] == sender["id"]
    assert message["recipient_agent_id"] == recipient["id"]
    assert message["correlation_id"]

    conversation = await client.get(
        f"/api/v1/conversations/{body['conversation_id']}", headers=user.headers
    )
    assert conversation.status_code == 200
    detail = conversation.json()
    assert detail["message_count"] == 1
    assert set(detail["participant_ids"]) == {sender["id"], recipient["id"]}
    assert len(detail["messages"]) == 1


async def test_strategy_is_inferred_from_entrypoint(client, user):
    sender, recipient = await _two_agents(client, user)
    py_agent = await create_published_agent(
        client, user, name="Python агент", slug="msg-python"
    )
    version = await client.post(
        f"/api/v1/agents/{py_agent['id']}/versions",
        json=version_payload(
            publish=True,
            entrypoint_type="python",
            entrypoint_config={"code": "def run(payload):\n    return {'ok': True}\n"},
        ),
        headers=user.headers,
    )
    assert version.status_code == 201, version.text

    to_http = (await _send(client, user, sender, recipient)).json()["message"]
    assert to_http["strategy"] == "http"

    to_python = (await _send(client, user, sender, py_agent)).json()["message"]
    assert to_python["strategy"] == "sandbox"


async def test_idempotency_key_prevents_duplicate_messages(client, user):
    sender, recipient = await _two_agents(client, user)
    key = f"idem-{uuid.uuid4().hex[:8]}"
    first = await _send(client, user, sender, recipient, idempotency_key=key)
    assert first.status_code == 201
    assert first.json()["is_duplicate"] is False

    second = await _send(client, user, sender, recipient, idempotency_key=key)
    assert second.status_code == 201
    assert second.json()["is_duplicate"] is True
    assert second.json()["message"]["id"] == first.json()["message"]["id"]

    conversation = await client.get(
        f"/api/v1/conversations/{first.json()['conversation_id']}", headers=user.headers
    )
    assert conversation.json()["message_count"] == 1


async def test_cannot_send_request_to_self(client, user):
    sender, _ = await _two_agents(client, user)
    response = await _send(client, user, sender, sender)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "self_message_forbidden"


async def test_cannot_send_to_unpublished_agent(client, user):
    sender, _ = await _two_agents(client, user)
    draft = await client.post(
        "/api/v1/agents",
        json={
            "slug": "msg-draft",
            "name": "Черновик",
            "summary": "Агент, который ещё не опубликован в каталоге",
            "capabilities": [{"capability_key": "x.y", "declared_level": 1}],
        },
        headers=user.headers,
    )
    assert draft.status_code == 201
    response = await _send(client, user, sender, draft.json())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "recipient_not_available"


async def test_only_owner_can_send_as_agent(client, user, other_user):
    sender, recipient = await _two_agents(client, user)
    response = await _send(client, other_user, sender, recipient)
    assert response.status_code == 403


async def test_reply_flow_builds_chain(client, user):
    sender, recipient = await _two_agents(client, user)
    first = (await _send(client, user, sender, recipient)).json()

    reply = await client.post(
        f"/api/v1/agents/{recipient['id']}/messages/{first['message']['id']}/reply",
        json={"payload": {"answer": "Хорошо"}, "kind": "response"},
        headers=user.headers,
    )
    assert reply.status_code == 201, reply.text
    reply_message = reply.json()["message"]
    assert reply_message["parent_id"] == first["message"]["id"]
    assert reply_message["correlation_id"] == first["message"]["correlation_id"]
    assert reply_message["kind"] == "response"

    conversation = await client.get(
        f"/api/v1/conversations/{first['conversation_id']}", headers=user.headers
    )
    messages = conversation.json()["messages"]
    assert [m["kind"] for m in messages] == ["request", "response"]


async def test_reply_to_foreign_recipient_rejected(client, user):
    sender, recipient = await _two_agents(client, user)
    third = await create_published_agent(client, user, name="Третий", slug="msg-third")
    first = (await _send(client, user, sender, recipient)).json()

    # Третий агент пытается ответить на сообщение, адресованное не ему
    response = await client.post(
        f"/api/v1/agents/{third['id']}/messages/{first['message']['id']}/reply",
        json={"payload": {}},
        headers=user.headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_message_recipient"


async def test_chain_depth_is_limited(client, user):
    """Защита от бесконечного пинг-понга между агентами."""
    from app.services.messaging import MAX_CHAIN_DEPTH

    sender, recipient = await _two_agents(client, user)
    current_recipient, current_sender = recipient, sender
    message = (await _send(client, user, sender, recipient)).json()["message"]

    for hop in range(MAX_CHAIN_DEPTH):
        response = await client.post(
            f"/api/v1/agents/{current_recipient['id']}/messages/{message['id']}/reply",
            json={"payload": {"hop": hop}},
            headers=user.headers,
        )
        if response.status_code == 422:
            assert response.json()["error"]["code"] == "chain_depth_exceeded"
            break
        assert response.status_code == 201, response.text
        message = response.json()["message"]
        current_recipient, current_sender = current_sender, current_recipient
    else:  # pragma: no cover - цепочка не ограничилась
        raise AssertionError("глубина цепочки не была ограничена")


# ── Inbox и видимость ─────────────────────────────────────────────────
async def test_inbox_shows_incoming_messages(client, user):
    sender, recipient = await _two_agents(client, user)
    await _send(client, user, sender, recipient)

    inbox = await client.get(
        f"/api/v1/agents/{recipient['id']}/inbox", headers=user.headers
    )
    assert inbox.status_code == 200
    assert len(inbox.json()) == 1
    assert inbox.json()[0]["sender_agent_id"] == sender["id"]

    outbox = await client.get(
        f"/api/v1/agents/{sender['id']}/inbox", headers=user.headers
    )
    assert outbox.json() == []


async def test_inbox_forbidden_for_foreign_agent(client, user, other_user):
    _, recipient = await _two_agents(client, user)
    response = await client.get(
        f"/api/v1/agents/{recipient['id']}/inbox", headers=other_user.headers
    )
    assert response.status_code == 403


async def test_conversation_visibility_restricted(client, user, other_user):
    sender, recipient = await _two_agents(client, user)
    conversation_id = (await _send(client, user, sender, recipient)).json()["conversation_id"]

    mine = await client.get(f"/api/v1/conversations/{conversation_id}", headers=user.headers)
    assert mine.status_code == 200

    foreign = await client.get(
        f"/api/v1/conversations/{conversation_id}", headers=other_user.headers
    )
    assert foreign.status_code == 403

    listing = await client.get("/api/v1/conversations", headers=other_user.headers)
    assert listing.json()["items"] == []


async def test_message_details_restricted(client, user, other_user):
    sender, recipient = await _two_agents(client, user)
    message_id = (await _send(client, user, sender, recipient)).json()["message"]["id"]

    assert (
        await client.get(f"/api/v1/messages/{message_id}", headers=user.headers)
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/messages/{message_id}", headers=other_user.headers)
    ).status_code == 403


async def test_anonymous_cannot_send_messages(client):
    response = await client.post(
        f"/api/v1/agents/{uuid.uuid4()}/messages",
        json={"recipient_agent_id": str(uuid.uuid4()), "payload": {}},
    )
    assert response.status_code == 401


# ── Диалоги с привязкой к заданию ─────────────────────────────────────
async def test_conversation_can_be_linked_to_task(client, user):
    sender, recipient = await _two_agents(client, user)
    task = await client.post(
        "/api/v1/tasks",
        json={"agent_id": recipient["id"], "input_payload": {"q": "1"}},
        headers=user.headers,
    )
    assert task.status_code == 201, task.text

    response = await client.post(
        "/api/v1/conversations",
        json={
            "subject": "Обсуждение задания",
            "participant_ids": [sender["id"], recipient["id"]],
            "task_id": task.json()["id"],
        },
        headers=user.headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["task_id"] == task.json()["id"]

    close = await client.post(
        f"/api/v1/conversations/{response.json()['id']}/close", headers=user.headers
    )
    assert close.status_code == 200
    assert close.json()["status"] == "closed"

    after_close = await _send(
        client, user, sender, recipient, conversation_id=response.json()["id"]
    )
    assert after_close.status_code == 422
    assert after_close.json()["error"]["code"] == "conversation_closed"


async def test_conversation_rejects_unpublished_participant(client, user):
    draft = await client.post(
        "/api/v1/agents",
        json={
            "slug": "msg-draft-2",
            "name": "Черновик два",
            "summary": "Ещё не опубликованный агент для проверки диалогов",
            "capabilities": [{"capability_key": "x.y", "declared_level": 1}],
        },
        headers=user.headers,
    )
    response = await client.post(
        "/api/v1/conversations",
        json={"subject": "x", "participant_ids": [draft.json()["id"]]},
        headers=user.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "agent_not_published"


# ── Обнаружение агентов ───────────────────────────────────────────────
async def test_discovery_ranks_by_capability_coverage(client, user):
    full = await create_published_agent(
        client, user, name="Полное покрытие", slug="disc-full"
    )
    partial = await create_published_agent(
        client, user, name="Частичное покрытие", slug="disc-partial"
    )
    await client.put(
        f"/api/v1/agents/{partial['id']}/capabilities",
        json={"capabilities": [{"capability_key": "research.web_search", "declared_level": 3}]},
        headers=user.headers,
    )

    response = await client.get(
        "/api/v1/discovery/agents",
        params=[
            ("capabilities", "research.web_search"),
            ("capabilities", "text.summarize"),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["required_capabilities"] == ["research.web_search", "text.summarize"]
    ids = [match["agent_id"] for match in body["matches"]]
    assert ids[0] == full["id"], "агент с полным покрытием должен быть первым"
    assert partial["id"] in ids
    by_id = {match["agent_id"]: match for match in body["matches"]}
    assert by_id[full["id"]]["score"] > by_id[partial["id"]]["score"]
    assert by_id[partial["id"]]["missing_capabilities"] == ["text.summarize"]


async def test_discovery_returns_empty_for_unknown_capability(client, user):
    await create_published_agent(client, user)
    response = await client.get(
        "/api/v1/discovery/agents", params={"capabilities": "no.such.capability"}
    )
    assert response.status_code == 200
    assert response.json()["matches"] == []


async def test_discovery_hides_drafts(client, user):
    draft = await client.post(
        "/api/v1/agents",
        json={
            "slug": "disc-draft",
            "name": "Черновик-дискавери",
            "summary": "Черновик не должен попадать в результаты обнаружения",
            "capabilities": [{"capability_key": "unique.skill", "declared_level": 5}],
        },
        headers=user.headers,
    )
    response = await client.get(
        "/api/v1/discovery/agents", params={"capabilities": "unique.skill"}
    )
    assert response.json()["matches"] == []
    assert draft.status_code == 201


# ── MCP ───────────────────────────────────────────────────────────────
async def test_register_mcp_server_hides_token(client, user, other_user):
    agent = await create_published_agent(client, user)
    response = await client.put(
        f"/api/v1/agents/{agent['id']}/mcp",
        json={
            "name": "Инструменты",
            "server_url": "https://mcp.example.com/mcp",
            "auth_token": "super-secret-token",
        },
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_auth_token"] is True
    assert "super-secret-token" not in response.text
    assert "auth_token" not in body

    # Посторонний не видит ни инструментов, ни наличия токена
    foreign = await client.get(
        f"/api/v1/agents/{agent['id']}/mcp", headers=other_user.headers
    )
    assert foreign.status_code == 200
    assert foreign.json()["tools"] == []
    assert foreign.json()["has_auth_token"] is False


async def test_mcp_registration_requires_ownership(client, user, other_user):
    agent = await create_published_agent(client, user)
    response = await client.put(
        f"/api/v1/agents/{agent['id']}/mcp",
        json={"name": "Инструменты", "server_url": "https://evil.example.com/mcp"},
        headers=other_user.headers,
    )
    assert response.status_code == 403


async def test_mcp_url_is_validated(client, user):
    agent = await create_published_agent(client, user)
    response = await client.put(
        f"/api/v1/agents/{agent['id']}/mcp",
        json={"name": "Инструменты", "server_url": "ftp://nope"},
        headers=user.headers,
    )
    assert response.status_code == 422


async def test_mcp_tool_call_is_queued_for_worker(client, user):
    agent = await create_published_agent(client, user)
    await client.put(
        f"/api/v1/agents/{agent['id']}/mcp",
        json={"name": "Инструменты", "server_url": "https://mcp.example.com/mcp"},
        headers=user.headers,
    )
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/mcp/call",
        json={"tool_name": "search", "arguments": {"q": "agents"}},
        headers=user.headers,
    )
    assert response.status_code == 202, response.text
    message = response.json()["message"]
    assert message["strategy"] == "mcp"
    assert message["payload"]["tool"] == "search"


async def test_mcp_tool_call_without_server_rejected(client, user):
    agent = await create_published_agent(client, user)
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/mcp/call",
        json={"tool_name": "search"},
        headers=user.headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "mcp_server_missing"


@pytest.mark.parametrize("endpoint", ["/api/v1/messages/stats"])
async def test_message_stats_available(client, user, endpoint):
    sender, recipient = await _two_agents(client, user)
    await _send(client, user, sender, recipient)
    response = await client.get(endpoint)
    assert response.status_code == 200
    assert response.json()["pending"] >= 1


# ── WebSocket диалога ─────────────────────────────────────────────────
async def test_conversation_websocket_snapshot_and_live_event(client, user):
    from starlette.testclient import TestClient

    from app.main import app as fastapi_app

    sender, recipient = await _two_agents(client, user)
    created = (await _send(client, user, sender, recipient)).json()
    conversation_id = created["conversation_id"]

    token = user.tokens["access_token"] if hasattr(user, "tokens") else user.access_token
    with TestClient(fastapi_app) as sync_client:
        with sync_client.websocket_connect(
            f"/api/v1/ws/conversations/{conversation_id}?token={token}"
        ) as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["conversation_id"] == conversation_id
            assert len(snapshot["messages"]) == 1

            # Новое сообщение в диалоге приходит подписчику событием
            await client.post(
                f"/api/v1/agents/{recipient['id']}/messages",
                json={
                    "recipient_agent_id": sender["id"],
                    "payload": {"answer": 1},
                    "conversation_id": conversation_id,
                    "kind": "response",
                },
                headers=user.headers,
            )
            event = ws.receive_json()
            assert event["type"] == "event"
            assert event["event_type"] == "message.created"
            assert event["payload"] == {"answer": 1}


async def test_conversation_websocket_denies_foreign_user(client, user, other_user):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import app as fastapi_app

    sender, recipient = await _two_agents(client, user)
    conversation_id = (await _send(client, user, sender, recipient)).json()["conversation_id"]
    token = other_user.tokens["access_token"] if hasattr(other_user, "tokens") else other_user.access_token

    with TestClient(fastapi_app) as sync_client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with sync_client.websocket_connect(
                f"/api/v1/ws/conversations/{conversation_id}?token={token}"
            ) as ws:
                ws.receive_json()
    assert exc.value.code == 4403
