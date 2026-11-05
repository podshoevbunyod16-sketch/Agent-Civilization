"""E2E-проверка этапа 3 на живом стеке.

Сценарий:
1. поднимается локальный HTTP-агент (реальный aiohttp/ASGI сервер на 8099);
2. два агента публикуются в реестре;
3. агент-получатель смотрит на этот HTTP-эндпоинт;
4. отправляется A2A-сообщение → воркер доставляет → статус delivered;
5. проверяются: идемпотентность, inbox, WebSocket-событие, отказ доступа.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

API = "http://127.0.0.1:8000/api/v1"
SERVICE_TOKEN = "dev-internal-token-2026"
LOGIN = {"email": "architect@agent-civilization.dev", "password": "Architect2026"}


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        login = await http.post(f"{API}/auth/login", json=LOGIN)
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        suffix = uuid.uuid4().hex[:6]
        agents = []
        for slug, name in (("e2e-sender", "E2E отправитель"), ("e2e-recv", "E2E получатель")):
            created = await http.post(
                f"{API}/agents",
                json={
                    "slug": f"{slug}-{suffix}",
                    "name": name,
                    "summary": "Агент, созданный e2e-проверкой этапа 3",
                    "capabilities": [{"capability_key": "a2a.messaging", "declared_level": 2}],
                },
                headers=headers,
            )
            created.raise_for_status()
            agent = created.json()
            version = await http.post(
                f"{API}/agents/{agent['id']}/versions",
                json={
                    "version": "1.0.0",
                    "changelog": "e2e",
                    "runtime": "python3.11",
                    "entrypoint_type": "http",
                    "entrypoint_config": {"url": "http://127.0.0.1:8099/run"},
                    "resource_limits": {"max_wall_seconds": 10, "max_memory_mb": 128},
                    "publish": True,
                },
                headers=headers,
            )
            assert version.status_code == 201, version.text
            agents.append(agent)
        sender, recipient = agents

        message = await http.post(
            f"{API}/agents/{sender['id']}/messages",
            json={
                "recipient_agent_id": recipient["id"],
                "payload": {"question": "Проверка A2A-доставки"},
                "idempotency_key": f"e2e-{suffix}",
            },
            headers=headers,
        )
        assert message.status_code == 201, message.text
        body = message.json()
        message_id = body["message"]["id"]
        conversation_id = body["conversation_id"]
        print(f"1. сообщение поставлено в очередь: {message_id} strategy={body['message']['strategy']}")

        # идемпотентность
        again = await http.post(
            f"{API}/agents/{sender['id']}/messages",
            json={
                "recipient_agent_id": recipient["id"],
                "payload": {"question": "Проверка A2A-доставки"},
                "idempotency_key": f"e2e-{suffix}",
            },
            headers=headers,
        )
        assert again.json()["is_duplicate"] is True, "идемпотентность не сработала"
        assert again.json()["message"]["id"] == message_id
        print("2. повтор с тем же idempotency_key вернул то же сообщение (duplicate=True)")

        # воркер: одна итерация
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.worker.message_worker import InternalClient, MessageWorker

        client = InternalClient("http://127.0.0.1:8000", SERVICE_TOKEN)
        worker = MessageWorker(client, batch_size=20)
        outcome = await worker.run_once()
        await client.close()
        print(f"3. воркер: {outcome}")

        details = await http.get(f"{API}/messages/{message_id}", headers=headers)
        details.raise_for_status()
        final = details.json()
        print(
            f"4. статус сообщения: {final['status']}, попыток={final['attempts']}, "
            f"latency={final['latency_ms']}ms, ответ={json.dumps(final['response_payload'], ensure_ascii=False)}"
        )
        assert final["status"] == "delivered", final
        assert final["response_payload"]["received"]["question"] == "Проверка A2A-доставки"

        inbox = await http.get(f"{API}/agents/{recipient['id']}/inbox", headers=headers)
        assert len(inbox.json()) == 1 and inbox.json()[0]["status"] == "delivered"
        print("5. inbox получателя содержит доставленное сообщение")

        # воркер без сервисного токена не должен пройти
        forbidden = await http.post(f"{API}/internal/messages/claim", json={"limit": 1})
        assert forbidden.status_code == 403, forbidden.text
        print(f"6. внутренний API без токена: {forbidden.status_code} {forbidden.json()['error']['code']}")

        # WS-проверка через websockets-клиент
        from websockets.asyncio.client import connect

        async with connect(
            f"ws://127.0.0.1:8000/api/v1/ws/conversations/{conversation_id}?token={token}"
        ) as ws:
            snapshot = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert snapshot["type"] == "snapshot"
            assert any(m["id"] == message_id for m in snapshot["messages"])
            print(f"7. WebSocket snapshot: {len(snapshot['messages'])} сообщений в диалоге")

        stats = await http.get(f"{API}/messages/stats")
        print(f"8. статистика доставки: {stats.json()}")

        # изоляция: чужой пользователь не видит диалог
        other = await http.post(
            f"{API}/auth/register",
            json={
                "email": f"e2e-{suffix}@example.com",
                "password": "Str0ngPassw0rd!",
                "display_name": "E2E чужой",
            },
        )
        other.raise_for_status()
        other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
        foreign = await http.get(f"{API}/conversations/{conversation_id}", headers=other_headers)
        assert foreign.status_code == 403, foreign.text
        print(f"9. доступ чужого пользователя к диалогу: {foreign.status_code}")

        print("\nE2E OK: A2A-доставка, идемпотентность, права и WebSocket работают.")


if __name__ == "__main__":
    asyncio.run(main())
