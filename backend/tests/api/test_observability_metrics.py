"""Этап 10, G: метрики, по которым можно поставить алерты.

Проверяется не «метрика существует», а «метрика меняется тогда, когда
должна»: задержка задания появляется только после ответа узла, возраст
воркера растёт при простое и падает после прохода, пул соединений виден
отдельно от доменных запросов.
"""

from __future__ import annotations

import time

import pytest

from app.core import db_metrics
from app.core.metrics import metrics


@pytest.fixture(autouse=True)
def _clean():
    db_metrics.reset_beats()
    metrics.durations.clear()
    yield
    db_metrics.reset_beats()
    metrics.durations.clear()


def _event_count(name: str, outcome: str) -> int:
    """Счётчик события: реестр метрик общий на процесс, поэтому сравниваем
    дельты, а не абсолютные значения."""
    return metrics.events[(name, outcome)]


def _histogram_count(name: str) -> int:
    histogram = metrics.durations.get(name)
    return 0 if histogram is None else histogram.count


def test_worker_heartbeat_reports_age_not_timestamp():
    now = time.time()
    db_metrics.heartbeat("outbox", at=now - 42)
    snapshot = db_metrics.worker_health_snapshot(now=now)
    assert snapshot["worker_outbox_age_seconds"] == pytest.approx(42.0, abs=0.1)


def test_worker_becomes_stale_after_threshold():
    now = time.time()
    db_metrics.heartbeat("outbox", at=now - 10)
    assert db_metrics.stale_workers(now=now) == []
    db_metrics.heartbeat("outbox", at=now - 10_000)
    assert db_metrics.stale_workers(now=now) == ["outbox"]


def test_successful_task_emits_duration_and_outcome():
    from datetime import UTC, datetime, timedelta

    from app.db.models.task import Task, TaskStatus
    from app.services.gateway import _observe_task_outcome

    before_outcome = _event_count("task_outcome", "succeeded")
    started = datetime.now(UTC) - timedelta(seconds=2)
    task = Task(status=TaskStatus.succeeded)
    task.created_at = started
    task.started_at = started
    task.finished_at = started + timedelta(seconds=2)
    _observe_task_outcome(task, will_retry=False)

    assert _histogram_count("task_duration_ms") >= 1
    assert _histogram_count("task_execution_ms") >= 1
    assert _event_count("task_outcome", "succeeded") == before_outcome + 1


def test_retry_outcome_is_labelled_separately():
    from datetime import UTC, datetime

    from app.db.models.task import Task, TaskStatus
    from app.services.gateway import _observe_task_outcome

    before = _event_count("task_outcome", "retry")
    task = Task(status=TaskStatus.queued)
    task.created_at = datetime.now(UTC)
    _observe_task_outcome(task, will_retry=True)
    assert _event_count("task_outcome", "retry") == before + 1


def test_pool_snapshot_reports_sizes_without_database():
    values = db_metrics.pool_snapshot()
    # В тестах пул NullPool — счётчиков занятости нет, но функция не падает
    # и не делает запросов в БД.
    assert isinstance(values, dict)


def test_pool_snapshot_of_queue_pool(monkeypatch):
    """Пул отдаёт занятость/overflow через публичный API SQLAlchemy."""

    class FakePool:
        def checkedout(self) -> int:
            return 3

        def overflow(self) -> int:
            return 1

    class Engine:
        pool = FakePool()

    monkeypatch.setattr("app.core.config.settings.database_pool_size", 10)
    values = db_metrics.pool_snapshot(Engine())
    assert values["db_pool_checked_out"] == 3.0
    assert values["db_pool_overflow"] == 1.0
    assert values["db_pool_size"] == 10.0
    assert values["db_pool_max"] == 30.0
    assert values["db_pool_utilization"] == pytest.approx(0.3)


async def test_metrics_endpoint_includes_worker_metrics(client, monkeypatch):
    """/metrics отдаёт возраст воркера (метрика живости цикла)."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "internal_service_token", "test-service-token")
    db_metrics.heartbeat("outbox")
    response = await client.get("/metrics", headers={"X-Service-Token": "test-service-token"})
    assert response.status_code == 200
    body = response.text
    assert "# TYPE ac_worker_outbox_age_seconds gauge" in body


async def test_metrics_endpoint_includes_pool_metrics(client, monkeypatch):
    """Пул появляется в выдаче, когда у движка есть настоящий пул.

    В тестах SQLAlchemy работает на `NullPool` (счётчиков занятости нет), а
    в production — на `QueuePool`. Поэтому пул подставляется явно: проверяется
    проводка, а не её отсутствие в тестовой конфигурации.
    """

    class FakePool:
        def checkedout(self) -> int:
            return 2

        def overflow(self) -> int:
            return 0

    class Engine:
        pool = FakePool()

    from app.core.config import settings

    monkeypatch.setattr(settings, "internal_service_token", "test-service-token")
    monkeypatch.setattr("app.db.session.get_engine", lambda: Engine())

    response = await client.get("/metrics", headers={"X-Service-Token": "test-service-token"})
    assert response.status_code == 200
    body = response.text
    assert 'ac_db_pool_checked_out{instance="' in body
    assert 'ac_db_pool_checked_out{instance="' in body and "} 2.0" in body
    assert "# TYPE ac_db_pool_size gauge" in body


async def test_health_report_warns_about_stalled_worker(client, admin_user):
    db_metrics.heartbeat("backup", at=time.time() - 10_000)
    response = await client.get("/api/v1/ops/health", headers=admin_user.headers)
    assert response.status_code == 200
    codes = {item["code"] for item in response.json()["warnings"]}
    assert "worker_stalled" in codes
