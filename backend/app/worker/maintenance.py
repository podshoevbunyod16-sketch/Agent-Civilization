"""Демон обслуживания (этап 9): возврат аренд, offline-узлы, истечение сообщений.

Зачем отдельный процесс, а не задача в API: при нескольких репликах API
невозможно гарантировать «ровно один проход», а блокировку в Redis давать
нельзя (Redis может быть недоступен, а аренды при этом надо возвращать).
Демон — это stateless-цикл, который можно запускать в любом числе копий:
все операции идемпотентны и опираются на `FOR UPDATE SKIP LOCKED` в выборках,
поэтому два демона не сделают двойной работы.

    python -m app.worker.maintenance --interval 30

Что он делает за проход:

1. `gateway.expire_leases` — задачи с истёкшей арендой возвращаются в очередь
   (или помечаются `expired`, если попытки кончились);
2. `nodes.mark_stale_nodes_offline` — узлы без heartbeat > порога перестают
   считаться исполнителями;
3. `messaging.expire_messages` — недоставленные сообщения с истёкшим сроком
   не висят вечно в очереди;
4. снимает gauge'и и пишет их в лог: даже без Prometheus видно динамику;
5. логирует предупреждения эксплуатации (застой очереди, разбалансировка).

Остановка — по SIGTERM/SIGINT с завершением текущего прохода.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db_metrics
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.metrics import metrics
from app.db.session import dispose_engine, get_session_factory
from app.services import gateway, observability
from app.services import messaging as messaging_service
from app.services import nodes as node_service

logger = logging.getLogger("maintenance")


async def run_once(
    session: AsyncSession, *, stale_after_seconds: int = 120, collect: bool = True
) -> dict[str, Any]:
    """Один проход обслуживания. Идемпотентен: повтор ничего не ломает."""
    started = time.perf_counter()
    requeued = await gateway.expire_leases(session)
    # Просроченные в очереди задания: без этого шага они висят вечно, если
    # подходящих узлов нет (дефект найден chaos-тестом этапа 10).
    overdue_closed = await gateway.expire_overdue_tasks(session)
    offline = await node_service.mark_stale_nodes_offline(
        session, stale_after_seconds=stale_after_seconds
    )
    expired = await messaging_service.expire_messages(session)
    await session.commit()

    gauges: dict[str, float] = {}
    warnings: list[dict[str, str]] = []
    if collect:
        report = await observability.health_report(session)
        gauges = report["gauges"]
        warnings = report["warnings"]

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if requeued or offline or expired or overdue_closed:
        metrics.incr("maintenance.runs", outcome="changed")
    else:
        metrics.incr("maintenance.runs", outcome="noop")
    metrics.observe_duration("maintenance", duration_ms)
    # Отметка «цикл жив»: по ней `/ops/health` и алерт видят, что демон не
    # встал, а не только что БД отвечает (этап 10, G).
    db_metrics.heartbeat("maintenance")

    outcome = {
        "requeued_tasks": requeued,
        "overdue_closed": overdue_closed,
        "nodes_offline": offline,
        "expired_messages": expired,
        "duration_ms": duration_ms,
        "warnings": [item["code"] for item in warnings],
        "queue_depth": gauges.get("tasks_queued"),
        "nodes_online": gauges.get("nodes_online"),
    }
    if requeued or offline or expired or overdue_closed:
        logger.info("maintenance_pass", extra=outcome)
    if warnings:
        logger.warning(
            "maintenance_warnings",
            extra={"warnings": warnings, "queue_depth": gauges.get("tasks_queued")},
        )
    return outcome


class MaintenanceWorker:
    """Цикл обслуживания с корректной остановкой."""

    def __init__(self, *, interval: float = 30.0, stale_after_seconds: int = 120) -> None:
        self.interval = interval
        self.stale_after_seconds = stale_after_seconds
        self._running = True
        self.passes = 0

    def stop(self) -> None:
        self._running = False

    async def run_forever(self) -> None:
        factory = get_session_factory()
        logger.info(
            "maintenance_started",
            extra={"interval": self.interval, "stale_after": self.stale_after_seconds},
        )
        while self._running:
            started = time.perf_counter()
            try:
                async with factory() as session:
                    await run_once(session, stale_after_seconds=self.stale_after_seconds)
                self.passes += 1
            except Exception as exc:  # БД недоступна — ждём и пробуем снова
                metrics.incr("worker_pass_failed", outcome="maintenance")
                logger.error("maintenance_failed", extra={"error": str(exc)[:300]})
            elapsed = time.perf_counter() - started
            delay = max(0.5, self.interval - elapsed)
            slept = 0.0
            while self._running and slept < delay:
                await asyncio.sleep(min(0.5, delay - slept))
                slept += 0.5
        await dispose_engine()
        logger.info("maintenance_stopped", extra={"passes": self.passes})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Демон обслуживания Agent Civilization")
    parser.add_argument(
        "--interval",
        type=float,
        default=float(settings.maintenance_interval_seconds),
        help="пауза между проходами, секунды",
    )
    parser.add_argument(
        "--stale-after",
        type=int,
        default=settings.node_stale_after_seconds,
        help="через сколько секунд без heartbeat узел считается offline",
    )
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    configure_logging()
    if args.once:
        factory = get_session_factory()
        async with factory() as session:
            outcome = await run_once(session, stale_after_seconds=args.stale_after)
        logger.info("maintenance_once", extra=outcome)
        await dispose_engine()
        return 0

    worker = MaintenanceWorker(
        interval=args.interval, stale_after_seconds=args.stale_after
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, worker.stop)
    await worker.run_forever()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
