"""Снимок состояния БД и воркеров для gauge-метрик (этап 10, G).

Почему это отдельный модуль, а не `observability.py`: метрики должны быть
видны, даже когда запросы к домену падают. `render_metrics` ловит исключение
из `collect_gauges`, и при недоступной БД доменные метрики просто исчезали.
Здесь два независимых сигнала:

**Пул соединений.** Рост занятых соединений без роста нагрузки означает
утечку (не закрыли сессию); постоянный `overflow` — пул мал, и увеличивать
надо `DATABASE_POOL_SIZE`, а не `max_connections` в Postgres. Читается
публичный API SQLAlchemy (`checkedout()`, `overflow()`), запросов в БД нет.
У `NullPool` (тесты) этих методов нет — тогда метрик занятости не
возвращается вовсе: «метрики нет» и «метрика равна нулю» — разные вещи.

**Живость воркеров.** Воркер отмечается в конце каждого прохода.
Публикуется *возраст* последнего прохода, а не timestamp: алерт читается
как «воркер не просыпался N секунд», и не нужно помнить эпоху.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from app.core.config import settings
from app.core.metrics import metrics

WORKER_STALE_AFTER_SECONDS = 300.0


def pool_snapshot(engine: Any | None = None) -> dict[str, float]:
    """Состояние пула без обращения к серверу БД."""
    if engine is None:
        try:
            from app.db.session import get_engine

            engine = get_engine()
        except Exception:  # pragma: no cover - движок ещё не создан
            return {}
    if engine is None:
        return {}
    pool = getattr(engine, "pool", None)
    if pool is None:  # pragma: no cover - защитный случай
        return {}

    values: dict[str, float] = {}
    if not callable(getattr(pool, "checkedout", None)):
        return values
    checked_out = float(pool.checkedout())
    overflow = float(pool.overflow()) if callable(getattr(pool, "overflow", None)) else 0.0
    pool_size = float(settings.database_pool_size)
    max_size = pool_size + float(settings.database_max_overflow)
    values["db_pool_checked_out"] = checked_out
    values["db_pool_overflow"] = max(overflow, 0.0)
    values["db_pool_size"] = pool_size
    values["db_pool_max"] = max_size
    values["db_pool_available"] = max(max_size - checked_out, 0.0)
    values["db_pool_utilization"] = round(checked_out / max(pool_size, 1.0), 3)
    return values


async def collect_pool_metrics(engine: Any | None = None) -> dict[str, float]:
    """Записать пул в реестр метрик (не стирая уже собранные gauge'и)."""
    values = await asyncio.to_thread(pool_snapshot, engine)
    if values:
        metrics.set_gauges({**metrics.gauges, **values})
    return values


# ── живость воркеров ───────────────────────────────────────────────────
_beats: dict[str, float] = {}
_beats_lock = threading.Lock()


def heartbeat(worker: str, *, at: float | None = None) -> None:
    """Отметить, что цикл воркера выполнил проход.

    Вызывается в конце каждого прохода (`maintenance`, `outbox`, `backup`,
    `message_worker`). Значение живёт в процессе: у каждого воркера своя
    метрика, поэтому расхождение реплик не мешает.
    """
    with _beats_lock:
        _beats[worker] = time.time() if at is None else at


def worker_health_snapshot(*, now: float | None = None) -> dict[str, float]:
    """Возраст последнего прохода по каждому известному воркеру."""
    current = time.time() if now is None else now
    with _beats_lock:
        return {
            f"worker_{name}_age_seconds": round(max(current - beat, 0.0), 1)
            for name, beat in sorted(_beats.items())
        }


def stale_workers(*, now: float | None = None) -> list[str]:
    """Воркеры, молчащие дольше порога (для предупреждения в health)."""
    current = time.time() if now is None else now
    with _beats_lock:
        return sorted(
            name
            for name, beat in _beats.items()
            if current - beat > WORKER_STALE_AFTER_SECONDS
        )


def reset_beats() -> None:
    """Сброс отметок (тесты)."""
    with _beats_lock:
        _beats.clear()


__all__ = [
    "WORKER_STALE_AFTER_SECONDS",
    "collect_pool_metrics",
    "heartbeat",
    "pool_snapshot",
    "reset_beats",
    "stale_workers",
    "worker_health_snapshot",
]
