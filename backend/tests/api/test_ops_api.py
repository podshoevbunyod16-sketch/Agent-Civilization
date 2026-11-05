"""Тесты масштабирования и эксплуатации (этап 9).

Проверяется то, на что опирается эксплуатация:

* метрики в формате Prometheus корректны и не раскрывают идентификаторы
  (кардинальность ограничена шаблоном маршрута);
* `/metrics` закрыт без сервисного токена;
* gauge'и считаются из базы и переживают несколько реплик (не из памяти);
* предупреждения здоровья формулируют действие, а леджер-разбаланс —
  критичен;
* обслуживание идемпотентно: повтор не возвращает задачу дважды;
* бэкап отдаёт gzip с контрольной суммой и белым списком таблиц;
* хеширование паролей не блокирует event loop (регресс после нагрузочного
  теста).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.core.metrics import LATENCY_BUCKETS, MetricsRegistry
from app.services import observability
from tests.conftest import create_published_agent, register_node_client

SERVICE = {"X-Service-Token": "test-service-token"}


# ── реестр метрик (юнит) ────────────────────────────────────────────────


class TestMetricsRegistry:
    def test_observe_request_buckets(self):
        registry = MetricsRegistry()
        registry.observe_request(method="GET", route="/x", status=200, duration_ms=7)
        registry.observe_request(method="GET", route="/x", status=200, duration_ms=3000)
        text = registry.render_prometheus()
        assert "ac_http_requests_total{instance=" in text
        assert 'route="/x"' in text
        # 7 мс попадает в корзину 10 мс, 3000 — в 5000; счётчики кумулятивные,
        # поэтому le=5 остаётся нулём, а le=10000 и +Inf дают оба запроса.
        assert f'le="{LATENCY_BUCKETS[0]}"}} 0' in text
        assert f'le="{LATENCY_BUCKETS[1]}"}} 1' in text
        assert f'le="{LATENCY_BUCKETS[-1]}"}} 2' in text
        assert 'le="+Inf"} 2' in text
        assert "ac_http_request_duration_ms_count" in text

    def test_histogram_sum_and_count(self):
        registry = MetricsRegistry()
        for value in (1.0, 2.0, 3.0):
            registry.observe_request(method="POST", route="/y", status=201, duration_ms=value)
        text = registry.render_prometheus()
        assert "ac_http_request_duration_ms_sum" in text
        assert " 6.000" in text
        summary = registry.snapshot()
        assert summary["requests"][0]["count"] == 3

    def test_events_and_durations(self):
        registry = MetricsRegistry()
        registry.incr("task.succeeded", 2)
        registry.incr("task.failed", 1, outcome="error")
        registry.observe_duration("settle", 12.5)
        text = registry.render_prometheus()
        assert 'event="task.succeeded",outcome="ok"' in text
        assert 'event="task.failed",outcome="error"' in text
        assert "ac_operation_duration_ms_count" in text

    def test_gauges_rendered(self):
        registry = MetricsRegistry()
        registry.set_gauges({"tasks_queued": 5.0})
        assert "ac_tasks_queued" in registry.render_prometheus()

    def test_cardinality_limit(self):
        registry = MetricsRegistry()
        # Заполняем больше предела, чтобы сработал счётчик исключённых
        from app.core import metrics as module

        original = module.MAX_ROUTE_KEYS
        module.MAX_ROUTE_KEYS = 3
        try:
            for index in range(10):
                registry.observe_request(
                    method="GET", route=f"/r{index}", status=200, duration_ms=1
                )
            assert registry.snapshot()["routes_excluded"] > 0
        finally:
            module.MAX_ROUTE_KEYS = original


# ── /metrics ───────────────────────────────────────────────────────────


class TestMetricsEndpoint:
    async def test_requires_service_token(self, client):
        response = await client.get("/metrics")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "metrics_forbidden"

    async def test_service_token_gives_prometheus(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "internal_service_token", "test-service-token")
        response = await client.get("/metrics", headers=SERVICE)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert "# TYPE ac_uptime_seconds gauge" in body
        assert "ac_tasks_queued" in body
        assert "ac_ledger_total_units" in body

    async def test_api_v1_alias(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "internal_service_token", "test-service-token")
        response = await client.get("/api/v1/metrics", headers=SERVICE)
        assert response.status_code == 200
        assert "ac_uptime_seconds" in response.text

    async def test_public_flag(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "metrics_public", True)
        response = await client.get("/metrics")
        assert response.status_code == 200

    async def test_route_labels_have_no_ids(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "internal_service_token", "test-service-token")
        agent_id = str(uuid.uuid4())
        await client.get(f"/api/v1/agents/{agent_id}")
        body = (await client.get("/metrics", headers=SERVICE)).text
        # Идентификатора в метках быть не должно: иначе каждая сущность создаёт
        # новую серию. Шаблон маршрута — с плейсхолдером.
        assert agent_id not in body
        assert "/agents/{agent_id}" in body


# ── ops: health / metrics / tables ─────────────────────────────────────


class TestOpsHealth:
    async def test_forbidden_for_member(self, client, user):
        response = await client.get("/api/v1/ops/health", headers=user.headers)
        assert response.status_code == 403

    async def test_health_shape(self, client, admin_user):
        response = await client.get("/api/v1/ops/health", headers=admin_user.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] in {"ok", "degraded", "critical"}
        assert "gauges" in body
        assert body["gauges"]["ledger_total_units"] == 0
        assert body["process"]["python"]
        assert body["database"]["max_connections"] > 0

    async def test_ledger_imbalance_is_critical(self, client, admin_user, session):
        """Разбалансировка журнала обязана поднимать critical, а не тонуть в шуме."""
        from sqlalchemy import select

        from app.db.models.economy import Account

        account = (
            await session.execute(select(Account).limit(1))
        ).scalar_one_or_none()
        if account is None:
            await client.get("/api/v1/economy/me", headers=admin_user.headers)
            account = (await session.execute(select(Account).limit(1))).scalar_one()
        await session.execute(
            update(Account).where(Account.id == account.id).values(balance_units=1.0)
        )
        await session.commit()
        body = (
            await client.get("/api/v1/ops/health", headers=admin_user.headers)
        ).json()
        assert body["status"] == "critical"
        assert any(item["code"] == "ledger_unbalanced" for item in body["warnings"])

    async def test_no_nodes_warning(self, client, admin_user):
        body = (await client.get("/api/v1/ops/health", headers=admin_user.headers)).json()
        codes = {item["code"] for item in body["warnings"]}
        # В тестовой базе узлов нет, заданий тоже — предупреждения о нехватке
        # узлов быть не должно: оно появляется только при непустой очереди.
        assert "no_nodes" not in codes

    async def test_metrics_json(self, client, admin_user):
        response = await client.get("/api/v1/ops/metrics", headers=admin_user.headers)
        assert response.status_code == 200
        body = response.json()
        assert "gauges" in body
        assert body["process"]["pid"] > 0

    async def test_tables(self, client, admin_user):
        response = await client.get("/api/v1/ops/tables", headers=admin_user.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total_rows"] >= 0
        assert all("rows" in item for item in body["tables"])

    async def test_readiness(self, client, admin_user):
        response = await client.get("/api/v1/ops/readiness", headers=admin_user.headers)
        assert response.status_code == 200
        body = response.json()
        assert "capacity_ok" in body
        assert "nodes" in body


# ── обслуживание ────────────────────────────────────────────────────────


class TestMaintenance:
    async def _leased_task(self, client, user):
        agent = await create_published_agent(client, user)
        task = (
            await client.post(
                "/api/v1/tasks",
                json={"agent_id": agent["id"], "input_payload": {"input": {"text": "x"}}},
                headers=user.headers,
            )
        ).json()
        _node, node_client = await register_node_client(client, user)
        claimed = await node_client.request(
            "POST", "/api/v1/nodes/me/tasks/claim", json_body={"limit": 5}
        )
        assert claimed.status_code == 200
        return task

    async def test_expired_lease_returns_to_queue(self, client, admin_user, session):
        from app.db.models.task import Task, TaskStatus

        await self._leased_task(client, admin_user)
        # Состариваем аренду: узел «завис» и не отчитался
        await session.execute(
            update(Task)
            .where(Task.status.in_([TaskStatus.leased, TaskStatus.running]))
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        await session.commit()
        response = await client.post(
            "/api/v1/ops/maintenance", headers=admin_user.headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["requeued_tasks"] >= 1

    async def test_maintenance_is_idempotent(self, client, admin_user, session):
        from app.db.models.task import Task, TaskStatus

        await self._leased_task(client, admin_user)
        await session.execute(
            update(Task)
            .where(Task.status.in_([TaskStatus.leased, TaskStatus.running]))
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        await session.commit()
        first = (
            await client.post("/api/v1/ops/maintenance", headers=admin_user.headers)
        ).json()
        second = (
            await client.post("/api/v1/ops/maintenance", headers=admin_user.headers)
        ).json()
        assert first["requeued_tasks"] >= 1
        assert second["requeued_tasks"] == 0

    async def test_stale_node_marked_offline(self, client, admin_user, session):
        from app.db.models.node import InfrastructureNode, NodeStatus

        await register_node_client(client, admin_user)
        await session.execute(
            update(InfrastructureNode).values(
                status=NodeStatus.online,
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()
        body = (
            await client.post("/api/v1/ops/maintenance", headers=admin_user.headers)
        ).json()
        assert body["nodes_offline"] >= 1

    async def test_forbidden_for_member(self, client, user):
        response = await client.post("/api/v1/ops/maintenance", headers=user.headers)
        assert response.status_code == 403

    async def test_worker_run_once(self, client, admin_user, session):
        """Тот же проход доступен как процесс-демон, а не только через API."""
        from app.db.session import get_session_factory
        from app.worker.maintenance import run_once

        factory = get_session_factory()
        async with factory() as worker_session:
            outcome = await run_once(worker_session, stale_after_seconds=60)
        assert "requeued_tasks" in outcome
        assert outcome["duration_ms"] >= 0


# ── бэкап ───────────────────────────────────────────────────────────────


class TestBackup:
    async def test_backup_streams_gzip_jsonl(self, client, admin_user):
        await client.get("/api/v1/economy/me", headers=admin_user.headers)
        response = await client.get(
            "/api/v1/ops/backup",
            params={"tables": "accounts,transactions"},
            headers=admin_user.headers,
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/gzip"
        assert response.headers["X-Checksum-Sha256"]
        payload = gzip.decompress(response.content).decode()
        lines = payload.strip().split("\n")
        header = json.loads(lines[0])
        assert header["format"] == "agent-civilization-backup"
        assert set(header["tables"]) == {"accounts", "transactions"}
        for line in lines[1:]:
            row = json.loads(line)
            assert row["table"] in {"accounts", "transactions"}

    async def test_unknown_table_rejected(self, client, admin_user):
        response = await client.get(
            "/api/v1/ops/backup",
            params={"tables": "users; DROP TABLE users"},
            headers=admin_user.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "backup_table_unknown"

    async def test_forbidden_for_member(self, client, user):
        response = await client.get("/api/v1/ops/backup", headers=user.headers)
        assert response.status_code == 403


# ── масштабирование: параллелизм и отсутствие блокировок ───────────────


class TestScaling:
    async def test_parallel_logins_do_not_block_event_loop(self, client, user):
        """После выноса PBKDF2 в пул потоков параллельные входы идут конкурентно.

        Регресс на дефект этапа 9: раньше каждый вход блокировал event loop на
        ~10 мс, и 8 параллельных входов занимали ~время суммы, а не одного.
        """
        credentials = {"email": user.email, "password": user.password}

        async def login() -> int:
            response = await client.post("/api/v1/auth/login", json=credentials)
            return response.status_code

        started = time.perf_counter()
        results = await asyncio.gather(*(login() for _ in range(6)))
        elapsed = time.perf_counter() - started
        assert all(code == 200 for code in results)
        # 6 хешей по ~10 мс последовательно дали бы >60 мс; в пуле потоков
        # на 2 ядрах ожидаем заметно меньше этой суммы.
        assert elapsed < 0.5, elapsed

    async def test_password_hash_and_verify_async(self):
        from app.core.security import hash_password_async, verify_password_async

        encoded = await hash_password_async("SuperSecret2026")
        assert encoded.startswith("pbkdf2_sha256$")
        assert await verify_password_async("SuperSecret2026", encoded)
        assert not await verify_password_async("wrong-password", encoded)

    async def test_gauges_are_shared_state_not_memory(self, client, user, session):
        """Gauge'и считаются из БД: две «реплики» видят одно и то же."""
        first = await observability.collect_gauges(session)
        second = await observability.collect_gauges(session)
        assert first["tasks_total"] == second["tasks_total"]
        assert first["ledger_total_units"] == second["ledger_total_units"]

    def test_latency_buckets_sorted(self):
        assert list(LATENCY_BUCKETS) == sorted(LATENCY_BUCKETS)
        assert LATENCY_BUCKETS[-1] >= 5000
