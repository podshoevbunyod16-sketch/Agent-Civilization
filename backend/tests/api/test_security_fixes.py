"""Этап 10, A: регрессионные тесты на найденные уязвимости.

Каждый тест воспроизводит сценарий эксплуатации из аудита и проверяет,
что он больше не проходит.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest

from tests.conftest import NodeClient, register_node_client, register_user


def _sign(secret_hash_hex: str, *, node_key: str, method: str, path: str, body: bytes = b""):
    """Подпись тем же способом, что и узел (ключ = sha256(secret))."""
    from app.core.security import build_signing_payload

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    payload = build_signing_payload(
        node_key=node_key, method=method, path=path, timestamp=timestamp, nonce=nonce, body=body
    )
    signature = hmac.new(bytes.fromhex(secret_hash_hex), payload, hashlib.sha256).hexdigest()
    return {
        "X-Node-Signature": signature,
        "X-Node-Timestamp": timestamp,
        "X-Node-Nonce": nonce,
    }


# ── A #1: ротация секрета узла требует доказательства владения ─────────
@pytest.mark.asyncio
async def test_rotate_secret_requires_proof_of_possession(client, user):
    node, _node_client = await register_node_client(client, user)
    response = await client.post(
        f"/api/v1/nodes/{node['id']}/rotate-secret", headers=user.headers
    )
    # Без подписи текущим секретом ротация невозможна даже владельцу аккаунта
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "proof_of_possession_required"


@pytest.mark.asyncio
async def test_rotate_secret_with_valid_signature_succeeds(client, user):
    node, node_client = await register_node_client(client, user)
    headers = node_client.headers("POST", f"/api/v1/nodes/{node['id']}/rotate-secret")
    headers.pop("_json", None)
    headers.pop("X-Body-Length", None)
    headers["Authorization"] = user.headers["Authorization"]

    response = await client.post(
        f"/api/v1/nodes/{node['id']}/rotate-secret", headers=headers
    )
    assert response.status_code == 200, response.text
    new_secret = response.json()["node_secret"]
    assert new_secret != node_client.key_hex
    # node_key не меняется — это публичный идентификатор
    assert response.json()["node_key"] == node["node_key"]

    # Новый секрет работает: узел подтверждает владение (pending → online)
    rotated_client = NodeClient(client, node["node_key"], new_secret)
    heartbeat = await rotated_client.request(
        "POST", "/api/v1/nodes/me/heartbeat", {"status": "healthy"}
    )
    assert heartbeat.status_code == 200, heartbeat.text

    # Старый секрет больше не подходит
    stale = await node_client.request(
        "POST", "/api/v1/nodes/me/heartbeat", {"status": "healthy"}
    )
    assert stale.status_code == 401


@pytest.mark.asyncio
async def test_rotate_secret_rejects_foreign_signature(client, user, other_user):
    node, _ = await register_node_client(client, user)
    other_node, other_client = await register_node_client(client, other_user)
    # Подпись другого узла не подходит: путь привязан к конкретному node_id
    headers = other_client.headers("POST", f"/api/v1/nodes/{node['id']}/rotate-secret")
    headers.pop("_json", None)
    headers.pop("X-Body-Length", None)
    headers["Authorization"] = user.headers["Authorization"]
    response = await client.post(f"/api/v1/nodes/{node['id']}/rotate-secret", headers=headers)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rotate_secret_replay_rejected(client, user):
    node, node_client = await register_node_client(client, user)
    headers = node_client.headers("POST", f"/api/v1/nodes/{node['id']}/rotate-secret")
    headers.pop("_json", None)
    headers.pop("X-Body-Length", None)
    headers["Authorization"] = user.headers["Authorization"]

    first = await client.post(f"/api/v1/nodes/{node['id']}/rotate-secret", headers=headers)
    assert first.status_code == 200, first.text
    # Повтор того же nonce — отказ (старый секрет уже неактуален, но
    # проверяется именно anti-replay, а не подпись)
    second = await client.post(f"/api/v1/nodes/{node['id']}/rotate-secret", headers=headers)
    assert second.status_code == 401
    assert second.json()["error"]["code"] in ("replay_detected", "invalid_signature")


@pytest.mark.asyncio
async def test_force_rotation_admin_only(client, user, admin_user):
    node, _ = await register_node_client(client, user)

    # Владелец не может «принудительно» обойти подпись
    forbidden = await client.post(
        f"/api/v1/nodes/{node['id']}/rotate-secret?force=true", headers=user.headers
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "force_rotation_forbidden"

    # Админ может — это аварийное восстановление при потере секрета
    allowed = await client.post(
        f"/api/v1/nodes/{node['id']}/rotate-secret?force=true", headers=admin_user.headers
    )
    assert allowed.status_code == 200, allowed.text


# ── A #4: спуфинг IP через X-Forwarded-For ─────────────────────────────
@pytest.mark.asyncio
async def test_forwarded_for_ignored_from_untrusted_peer(client):
    from app.api.deps import client_ip

    class FakeRequest:
        def __init__(self, peer: str, forwarded: str | None):
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
            self.client = type("C", (), {"host": peer})()

    # Недоверенный пир: заголовок игнорируется, берётся адрес соединения
    assert client_ip(FakeRequest("203.0.113.7", "1.2.3.4")) == "203.0.113.7"


@pytest.mark.asyncio
async def test_forwarded_for_used_only_from_trusted_proxy(monkeypatch):
    from app.api.deps import client_ip
    from app.core.config import settings

    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8", raising=False)

    class FakeRequest:
        def __init__(self, peer: str, forwarded: str | None):
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
            self.client = type("C", (), {"host": peer})()

    # Доверенный прокси: берётся последний недоверенный адрес в цепочке —
    # то, что добавил сам прокси, а не то, что подставил клиент.
    assert client_ip(FakeRequest("10.0.0.5", "6.6.6.6, 198.51.100.9")) == "198.51.100.9"
    # Запрос напрямую от недоверенного клиента с подделанной цепочкой
    assert client_ip(FakeRequest("198.51.100.9", "6.6.6.6")) == "198.51.100.9"


@pytest.mark.asyncio
async def test_forwarded_for_cannot_bypass_bruteforce_lockout(client):
    """Перебор с меняющимся X-Forwarded-For не обходит блокировку по аккаунту."""
    from app.core.config import settings

    user = await register_user(client)
    for index in range(settings.login_max_attempts):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": "WrongPassword123"},
            headers={"X-Forwarded-For": f"198.51.100.{index}"},
        )
        assert response.status_code == 401
    blocked = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": user.password},
        headers={"X-Forwarded-For": "198.51.100.200"},
    )
    assert blocked.json()["error"]["code"] == "account_locked"


# ── A #6: rate limit не «fail-open» при недоступном Redis ──────────────
@pytest.mark.asyncio
async def test_rate_limit_uses_local_fallback_when_redis_down(monkeypatch):
    import fakeredis.aioredis

    from app.core import redis_client
    from app.core.config import settings
    from app.core.errors import RateLimitError
    from app.services import ratelimit

    class BrokenRedis:
        async def incr(self, *args, **kwargs):
            raise ConnectionError("redis down")

        async def expire(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rate_limit_critical_per_minute", 2, raising=False)
    ratelimit.reset_local()
    redis_client.set_redis(BrokenRedis())  # type: ignore[arg-type]
    try:
        await ratelimit.check_critical("ip:1.2.3.4")
        await ratelimit.check_critical("ip:1.2.3.4")
        with pytest.raises(RateLimitError):
            await ratelimit.check_critical("ip:1.2.3.4")
    finally:
        ratelimit.reset_local()
        redis_client.set_redis(fakeredis.aioredis.FakeRedis(decode_responses=True))


@pytest.mark.asyncio
async def test_anti_replay_fails_closed_without_redis(monkeypatch):
    """Подписанный запрос при недоступном Redis отклоняется, а не проходит."""
    from app.core import redis_client
    from app.core.errors import AuthenticationError
    from app.db.models.node import InfrastructureNode, NodeKind, NodeStatus

    class BrokenRedis:
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

        async def setnx(self, *args, **kwargs):
            raise ConnectionError("redis down")

        async def incr(self, *args, **kwargs):
            raise ConnectionError("redis down")

    node = InfrastructureNode(
        node_key="node_test",
        secret_hash=hashlib.sha256(b"secret").hexdigest(),
        name="n",
        kind=NodeKind.community,
        owner_id=uuid.uuid4(),
        status=NodeStatus.online,
    )
    redis_client.set_redis(BrokenRedis())  # type: ignore[arg-type]
    try:
        from app.services import nodes as node_service

        with pytest.raises(AuthenticationError):
            await node_service.assert_rotation_proof(
                node, signature="00", timestamp=str(int(time.time())), nonce="nonce-123456"
            )
    finally:
        import fakeredis.aioredis

        redis_client.set_redis(fakeredis.aioredis.FakeRedis(decode_responses=True))


# ── A: глобальные заголовки безопасности ───────────────────────────────
@pytest.mark.asyncio
async def test_security_headers_present(client):
    response = await client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Permissions-Policy"]


@pytest.mark.asyncio
async def test_request_id_correlation(client):
    response = await client.get("/health", headers={"X-Request-ID": "corr-test-1"})
    assert response.headers["X-Request-ID"] == "corr-test-1"


# ── A: валидация production-конфигурации ───────────────────────────────
def test_unsafe_production_config_refuses_to_start(monkeypatch):
    from app.core.config import Settings

    base = {
        "environment": "production",
        "debug": False,
        "secret_key": "x" * 40,
        "internal_service_token": "t" * 20,
        "redis_url": "redis://localhost:6379/0",
        "cors_origins": ["https://agent-civilization.dev"],
        "public_base_url": "https://agent-civilization.dev",
        "secret_encryption_keys": "1:" + __import__("base64").b64encode(b"k" * 32).decode(),
        "sandbox_backend": "docker",
        "sandbox_allow_network": False,
    }
    settings = Settings(**base)
    settings.validate_for_runtime()  # безопасная конфигурация стартует

    for patch, expected in [
        ({"secret_key": "change-me"}, "SECRET_KEY"),
        ({"debug": True}, "DEBUG"),
        ({"internal_service_token": ""}, "INTERNAL_SERVICE_TOKEN"),
        ({"cors_origins": ["*"]}, "CORS_ORIGINS"),
        ({"sandbox_allow_network": True}, "SANDBOX_ALLOW_NETWORK"),
        ({"sandbox_backend": "subprocess"}, "SANDBOX_BACKEND"),
        ({"secret_encryption_keys": ""}, "SECRET_ENCRYPTION_KEYS"),
        ({"public_base_url": "http://insecure"}, "PUBLIC_BASE_URL"),
    ]:
        broken = Settings(**{**base, **patch})
        with pytest.raises(RuntimeError) as exc:
            broken.validate_for_runtime()
        assert expected in str(exc.value), (patch, str(exc.value))


def test_development_config_allows_dev_defaults():
    from app.core.config import Settings

    settings = Settings(environment="development", secret_key="dev-secret-key-1234567890")
    settings.validate_for_runtime()


# ── A: секреты не утекают в ответах и логах ────────────────────────────
@pytest.mark.asyncio
async def test_node_secret_shown_only_once(client, user):
    payload = {
        "name": "node-secret-once",
        "kind": "community",
        "cpu_cores": 2,
        "memory_mb": 1024,
        "max_concurrency": 2,
        "limit_cpu_seconds": 60,
        "limit_wall_seconds": 120,
        "limit_memory_mb": 512,
        "capabilities": ["python"],
    }
    created = await client.post("/api/v1/nodes", json=payload, headers=user.headers)
    secret = created.json()["credentials"]["node_secret"]
    node_id = created.json()["node"]["id"]

    detail = await client.get(f"/api/v1/nodes/{node_id}", headers=user.headers)
    assert secret not in detail.text
    listed = await client.get("/api/v1/nodes", headers=user.headers)
    assert secret not in listed.text


@pytest.mark.asyncio
async def test_expired_signature_rejected(client, user):
    node, node_client = await register_node_client(client, user)
    headers = node_client.headers("POST", "/api/v1/nodes/me/heartbeat", b'{"status":"healthy"}')
    headers.pop("_json", None)
    headers.pop("X-Body-Length", None)
    headers["X-Node-Timestamp"] = str(int(time.time()) - 3600)
    response = await client.post(
        "/api/v1/nodes/me/heartbeat",
        headers=headers,
        content=json.dumps({"status": "healthy"}).encode(),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "signature_expired"


# ── A #3: WebSocket — тикет вместо access-токена в query ───────────────
@pytest.mark.asyncio
async def test_ws_ticket_is_short_lived_and_one_time(client, user):
    from app.core.security import decode_token

    response = await client.post("/api/v1/ws/ticket", headers=user.headers)
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    payload = decode_token(ticket, expected_type="ws")
    # Тикет живёт секунды, а не часы, и помечен отдельным типом
    assert payload["exp"] - payload["iat"] <= 60
    assert payload["type"] == "ws"
    # Тикет не работает как access-токен для HTTP API
    http_use = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {ticket}"}
    )
    assert http_use.status_code == 401


@pytest.mark.asyncio
async def test_ws_rejects_access_token_in_query(client, user):
    """Новые access-токены (со `sid`) в query-строке не принимаются."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import app as fastapi_app

    task = await _create_task(client, user)
    with TestClient(fastapi_app) as sync_client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with sync_client.websocket_connect(
                f"/api/v1/ws/tasks/{task['id']}?token={user.access_token}"
            ) as ws:
                ws.receive_json()
        assert exc.value.code == 4401
        assert "ticket" in exc.value.reason


@pytest.mark.asyncio
async def test_ws_ticket_cannot_be_reused(client, user):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import app as fastapi_app

    task = await _create_task(client, user)
    ticket = (await client.post("/api/v1/ws/ticket", headers=user.headers)).json()["ticket"]

    with TestClient(fastapi_app) as sync_client:
        with sync_client.websocket_connect(
            f"/api/v1/ws/tasks/{task['id']}?ticket={ticket}"
        ) as ws:
            assert ws.receive_json()["type"] == "snapshot"
        # Повторная попытка с тем же тикетом отклоняется
        with pytest.raises(WebSocketDisconnect) as exc:
            with sync_client.websocket_connect(
                f"/api/v1/ws/tasks/{task['id']}?ticket={ticket}"
            ) as ws:
                ws.receive_json()
        assert exc.value.code == 4401


@pytest.mark.asyncio
async def test_ws_ticket_scoped_to_user(client, user, other_user):
    """Тикет одного пользователя не даёт доступ к чужому заданию."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.main import app as fastapi_app

    task = await _create_task(client, user)
    ticket = (
        await client.post("/api/v1/ws/ticket", headers=other_user.headers)
    ).json()["ticket"]
    with TestClient(fastapi_app) as sync_client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with sync_client.websocket_connect(
                f"/api/v1/ws/tasks/{task['id']}?ticket={ticket}"
            ) as ws:
                ws.receive_json()
        assert exc.value.code == 4403


async def _create_task(client, user) -> dict:
    """Минимальное задание для проверки WS-доступа."""
    from tests.conftest import create_published_agent

    agent = await create_published_agent(client, user)
    response = await client.post(
        "/api/v1/tasks",
        json={
            "agent_id": agent["id"],
            "input": {"prompt": "ws test"},
            "budget_units": 5,
        },
        headers=user.headers,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()
