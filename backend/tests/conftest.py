"""Общие фикстуры тестов.

* Тесты работают на РЕАЛЬНОМ PostgreSQL 16 (встроенный pgserver, temp-каталог).
* Схема создаётся НАСТОЯЩИМИ миграциями Alembic, а не create_all().
* Redis подменяется fakeredis (быстро, без внешнего сервиса).
* Окружение выставляется до импорта `app.*`, чтобы настройки были корректны.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# ── 1. Поднимаем PostgreSQL и фиксируем переменные окружения ДО импорта app ──
_PG_TMPDIR = tempfile.mkdtemp(prefix="ac-test-pg-")
_TEST_DB = f"ac_test_{uuid.uuid4().hex[:8]}"
_pg_server = None


def _bootstrap_postgres() -> str:
    global _pg_server
    import pgserver

    _pg_server = pgserver.get_server(_PG_TMPDIR, cleanup_mode="delete")
    _pg_server.psql(f'CREATE DATABASE "{_TEST_DB}";')
    host = _pg_server.get_uri().split("host=")[-1]
    return f"postgresql+asyncpg://postgres@/{_TEST_DB}?host={host}"


DATABASE_URL = _bootstrap_postgres()

os.environ.update(
    {
        "DATABASE_URL": DATABASE_URL,
        "ENVIRONMENT": "test",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-for-hs256-32",
        "RATE_LIMIT_ENABLED": "false",
        "SANDBOX_BACKEND": "subprocess",
        "SANDBOX_MAX_WALL_SECONDS": "10",
        "SANDBOX_MAX_MEMORY_MB": "256",
        "SANDBOX_MAX_CPU_SECONDS": "5",
        "LOG_LEVEL": "ERROR",
        "WORKER_LEASE_SECONDS": "60",
        "REDIS_URL": "redis://localhost:6379/15",
    }
)

PASSWORD = "SuperSecret123"


@pytest.fixture(scope="session", autouse=True)
def migrated_schema() -> Iterator[str]:
    """Прогоняем миграции на чистой базе — это и есть тест миграций."""
    env = {**os.environ, "DATABASE_URL": DATABASE_URL, "PYTHONPATH": str(BACKEND_DIR)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover
        hint = ""
        if "greenlet" in result.stderr:
            hint = (
                "\nПодсказка: SQLAlchemy async требует greenlet. "
                "Установите зависимости проекта: pip install -e '.[dev]'."
            )
        raise RuntimeError(
            f"alembic upgrade failed:{hint}\n{result.stderr}\n{result.stdout}"
        )
    yield DATABASE_URL


@pytest.fixture(autouse=True)
async def clean_database(migrated_schema: str) -> AsyncIterator[None]:
    from sqlalchemy import text

    from app.db.session import get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename <> 'alembic_version'"
                )
            )
        ).scalars().all()
        if tables:
            quoted = ", ".join(f'"{table}"' for table in tables)
            await conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture(autouse=True)
async def fake_redis(migrated_schema: str) -> AsyncIterator[None]:
    import fakeredis.aioredis

    from app.core import redis_client

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_client.set_redis(client)
    yield
    await client.flushall()
    await client.aclose()
    redis_client.set_redis(None)


@pytest.fixture()
def app(_app_factory):
    return _app_factory()


@pytest.fixture(scope="session")
def _app_factory(migrated_schema: str):
    from app.main import create_app

    return create_app


@pytest.fixture()
async def client(app) -> AsyncIterator[object]:
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.fixture()
async def session(client) -> AsyncIterator[object]:
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as db_session:
        yield db_session


# ── Хелперы ────────────────────────────────────────────────────────────
class ApiUser:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.id = payload["user"]["id"]
        self.email = payload["user"]["email"]
        self.access_token = payload["access_token"]
        self.refresh_token = payload["refresh_token"]

    @property
    def password(self) -> str:
        return PASSWORD

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def register_user(client, *, email: str | None = None, name: str = "Dev") -> ApiUser:
    email = email or f"user_{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": name},
    )
    assert response.status_code == 201, response.text
    return ApiUser(response.json())


@pytest.fixture()
async def user(client) -> ApiUser:
    return await register_user(client)


@pytest.fixture()
async def other_user(client) -> ApiUser:
    return await register_user(client, name="Other")


@pytest.fixture()
async def admin_user(client, session) -> ApiUser:
    from sqlalchemy import update

    from app.db.models.user import User, UserRole

    api_user = await register_user(client, name="Admin")
    await session.execute(
        update(User).where(User.id == uuid.UUID(api_user.id)).values(role=UserRole.admin)
    )
    await session.commit()
    return api_user


def agent_payload(**overrides) -> dict:
    payload = {
        "slug": f"agent-{uuid.uuid4().hex[:8]}",
        "name": "Агент-исследователь",
        "summary": "Ищет и структурирует информацию из открытых источников",
        "description": "Подробное описание агента для каталога",
        "category": "research",
        "tags": ["search", "analysis"],
        "capabilities": [
            {"capability_key": "research.web_search", "declared_level": 3},
            {"capability_key": "text.summarize", "declared_level": 2},
        ],
    }
    payload.update(overrides)
    return payload


def version_payload(**overrides) -> dict:
    payload = {
        "changelog": "Первая версия",
        "entrypoint_type": "http",
        "entrypoint_config": {"url": "https://agent.example.com/a2a"},
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "required_capabilities": [],
        "publish": False,
    }
    payload.update(overrides)
    return payload


async def create_agent(client, user: ApiUser, **overrides) -> dict:
    response = await client.post(
        "/api/v1/agents", json=agent_payload(**overrides), headers=user.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_published_agent(client, user: ApiUser, **overrides) -> dict:
    agent = await create_agent(client, user, **overrides)
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json=version_payload(publish=True),
        headers=user.headers,
    )
    assert version.status_code == 201, version.text
    detail = await client.get(f"/api/v1/agents/{agent['id']}", headers=user.headers)
    assert detail.status_code == 200, detail.text
    return detail.json()


# ── Подписанные запросы узла (используется в тестах узлов) ─────────────
class NodeClient:
    """Мини-клиент узла: подписывает запросы так же, как реальный worker."""

    def __init__(self, http, node_key: str, node_secret: str, api_prefix: str = "/api/v1"):
        import hashlib

        self.http = http
        self.node_key = node_key
        # Секрет никогда не хранится на сервере: клиент подписывает
        # HMAC-ключом sha256(secret), сервер хранит только этот хеш.
        self.key_hex = hashlib.sha256(node_secret.encode()).hexdigest()
        self.api_prefix = api_prefix

    def headers(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        import hashlib
        import hmac
        import json
        import time
        import uuid as _uuid

        from app.core.security import build_signing_payload

        timestamp = str(int(time.time()))
        nonce = _uuid.uuid4().hex
        # Путь в подписи — без префикса API (см. deps.get_signed_node)
        signed_path = path[len(self.api_prefix):] if path.startswith(self.api_prefix) else path
        payload = build_signing_payload(
            node_key=self.node_key,
            method=method,
            path=signed_path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        signature = hmac.new(bytes.fromhex(self.key_hex), payload, hashlib.sha256).hexdigest()
        return {
            "X-Node-Key": self.node_key,
            "X-Node-Signature": signature,
            "X-Node-Timestamp": timestamp,
            "X-Node-Nonce": nonce,
            "Content-Type": "application/json",
            "X-Body-Length": str(len(body)),
            "_json": json.dumps(json.loads(body.decode()) if body else {}),
        }

    async def request(self, method: str, path: str, json_body: dict | None = None):
        import json

        body = json.dumps(json_body).encode() if json_body is not None else b""
        headers = self.headers(method, path, body)
        headers.pop("_json", None)
        headers.pop("X-Body-Length", None)
        return await self.http.request(method, path, headers=headers, content=body)


async def register_node_client(client, user: ApiUser, **overrides) -> tuple[dict, NodeClient]:
    payload = {
        "name": f"node-{uuid.uuid4().hex[:6]}",
        "kind": "community",
        "cpu_cores": 4,
        "memory_mb": 2048,
        "max_concurrency": 4,
        "limit_cpu_seconds": 60,
        "limit_wall_seconds": 120,
        "limit_memory_mb": 1024,
        "capabilities": ["python"],
    }
    payload.update(overrides)
    response = await client.post("/api/v1/nodes", json=payload, headers=user.headers)
    assert response.status_code == 201, response.text
    data = response.json()
    node = data["node"]
    credentials = data["credentials"]
    return node, NodeClient(client, credentials["node_key"], credentials["node_secret"])


def run_async(coro):
    return asyncio.run(coro)
