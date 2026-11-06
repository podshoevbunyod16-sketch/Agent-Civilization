"""Сбор состояния системы из базы (этап 9).

Gauge'и не хранятся в памяти процесса, а считаются запросом: при нескольких
репликах API каждый под отдаёт одно и то же значение, и метрика не «прыгает»
в зависимости от того, кто ответил. Функция вызывается при scrape `/metrics`
и из `/ops/health`.

Ключевые сигналы для эксплуатации:

* очередь: задания в ожидании и возраст старейшего — видно, что система
  не справляется, до появления жалоб;
* застрявшие аренды и просроченные дедлайны — «тихая» деградация узлов;
* узлы online/offline/stale — ёмкость исполнения;
* доставка A2A: очередь, повторы, отказы;
* экономика: замороженные бюджеты и сходимость журнала;
* проверки совместимости: сколько прогонов провалено.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import metrics
from app.db.models.economy import (
    Account,
    AccountType,
    HoldStatus,
    TaskBudget,
    Transaction,
)
from app.db.models.ecosystem import CompatibilityCheck, Integration
from app.db.models.events import EventOutbox, OutboxStatus
from app.db.models.messaging import AgentMessage
from app.db.models.node import InfrastructureNode, NodeStatus
from app.db.models.task import Task, TaskStatus
from app.db.models.trust import TaskVerdict, Verdict

NODE_STALE_AFTER = timedelta(seconds=120)


def _now() -> datetime:
    return datetime.now(UTC)


def _counts(rows: list[Any]) -> dict[str, int]:
    return {
        (status.value if hasattr(status, "value") else str(status)): int(count)
        for status, count in rows
    }


async def _balance_units(session: AsyncSession) -> float:
    """Сумма балансов всех счетов. Обязана быть нулём (см. ADR-0009)."""
    total = (
        await session.execute(select(func.coalesce(func.sum(Account.balance_units), 0.0)))
    ).scalar_one()
    return round(float(total), 6)


async def collect_gauges(session: AsyncSession) -> dict[str, float]:
    """Считать gauge'и из базы и записать их в реестр метрик."""
    started = time.perf_counter()
    now = _now()

    task_rows = (
        await session.execute(select(Task.status, func.count()).group_by(Task.status))
    ).all()
    tasks = _counts(list(task_rows))

    oldest_queued = (
        await session.execute(
            select(func.min(Task.created_at)).where(Task.status == TaskStatus.queued)
        )
    ).scalar_one_or_none()

    stuck_leases = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status.in_([TaskStatus.leased, TaskStatus.running]),
                    Task.lease_expires_at.is_not(None),
                    Task.lease_expires_at < now,
                )
            )
        ).scalar_one()
    )
    overdue = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status.in_([TaskStatus.queued, TaskStatus.leased, TaskStatus.running]),
                    Task.deadline_at.is_not(None),
                    Task.deadline_at < now,
                )
            )
        ).scalar_one()
    )

    message_rows = (
        await session.execute(
            select(AgentMessage.status, func.count()).group_by(AgentMessage.status)
        )
    ).all()
    messages = _counts(list(message_rows))

    node_rows = (
        await session.execute(
            select(InfrastructureNode.status, func.count()).group_by(
                InfrastructureNode.status
            )
        )
    ).all()
    nodes = _counts(list(node_rows))
    stale_nodes = int(
        (
            await session.execute(
                select(func.count())
                .select_from(InfrastructureNode)
                .where(
                    InfrastructureNode.status == NodeStatus.online,
                    InfrastructureNode.last_seen_at.is_not(None),
                    InfrastructureNode.last_seen_at < now - NODE_STALE_AFTER,
                )
            )
        ).scalar_one()
    )

    check_rows = (
        await session.execute(
            select(CompatibilityCheck.status, func.count()).group_by(CompatibilityCheck.status)
        )
    ).all()
    checks = _counts(list(check_rows))

    held_budgets = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(TaskBudget.budget_units - TaskBudget.spent_units), 0.0),
            ).where(TaskBudget.status == HoldStatus.held)
        )
    ).one()

    verdict_rows = (
        await session.execute(
            select(TaskVerdict.verdict, func.count()).group_by(TaskVerdict.verdict)
        )
    ).all()
    verdicts = _counts(list(verdict_rows))

    # Outbox (этап 10, D): недоставленные и «мёртвые» события видны в health.
    outbox_rows = (
        await session.execute(
            select(EventOutbox.status, func.count()).group_by(EventOutbox.status)
        )
    ).all()
    outbox_counts = _counts(list(outbox_rows))
    outbox_oldest_pending = (
        await session.execute(
            select(func.min(EventOutbox.created_at)).where(
                EventOutbox.status == OutboxStatus.pending
            )
        )
    ).scalar_one_or_none()

    gauges: dict[str, float] = {
        "outbox_pending": float(outbox_counts.get("pending", 0)),
        "outbox_failed": float(outbox_counts.get("failed", 0)),
        "outbox_published": float(outbox_counts.get("published", 0)),
        "outbox_oldest_pending_age_seconds": (
            round((now - outbox_oldest_pending).total_seconds(), 1)
            if outbox_oldest_pending
            else 0.0
        ),
        "tasks_total": float(sum(tasks.values())),
        "tasks_queued": float(tasks.get("queued", 0)),
        "tasks_running": float(tasks.get("leased", 0) + tasks.get("running", 0)),
        "tasks_succeeded": float(tasks.get("succeeded", 0)),
        "tasks_failed": float(tasks.get("failed", 0)),
        "tasks_stuck_leases": float(stuck_leases),
        "tasks_overdue": float(overdue),
        "queue_oldest_age_seconds": (
            round((now - oldest_queued).total_seconds(), 1) if oldest_queued else 0.0
        ),
        "messages_total": float(sum(messages.values())),
        "messages_pending": float(messages.get("pending", 0)),
        "messages_failed": float(messages.get("failed", 0)),
        "messages_delivered": float(messages.get("delivered", 0)),
        "nodes_total": float(sum(nodes.values())),
        "nodes_online": float(nodes.get("online", 0)),
        "nodes_stale": float(stale_nodes),
        "compat_checks_passed": float(checks.get("passed", 0)),
        "compat_checks_failed": float(checks.get("failed", 0)),
        "integrations_total": float(
            (await session.execute(select(func.count()).select_from(Integration))).scalar_one()
        ),
        "held_budgets_count": float(held_budgets[0]),
        "held_budget_units": round(float(held_budgets[1] or 0.0), 4),
        "verdicts_verified": float(verdicts.get(Verdict.verified.value, 0)),
        "verdicts_rejected": float(verdicts.get(Verdict.rejected.value, 0)),
        "ledger_total_units": await _balance_units(session),
        "transactions_total": float(
            (await session.execute(select(func.count()).select_from(Transaction))).scalar_one()
        ),
        "treasury_accounts": float(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Account)
                    .where(Account.type == AccountType.treasury)
                )
            ).scalar_one()
        ),
    }
    metrics.set_gauges(gauges)
    metrics.observe_duration("collect_gauges", (time.perf_counter() - started) * 1000)
    return gauges


async def health_report(session: AsyncSession) -> dict[str, Any]:
    """Сводка для эксплуатации: состояние + предупреждения.

    `status` = `ok` | `degraded` | `critical`. Предупреждения формулируются
    как действия («истёкшие аренды некому вернуть»), а не как голые числа.
    """
    gauges = await collect_gauges(session)
    warnings: list[dict[str, str]] = []

    if gauges["ledger_total_units"] != 0:
        warnings.append(
            {
                "code": "ledger_unbalanced",
                "severity": "critical",
                "message": (
                    f"сумма всех балансов {gauges['ledger_total_units']} вместо нуля — "
                    "запустите POST /economy/verify"
                ),
            }
        )
    if gauges["tasks_stuck_leases"] > 0:
        warnings.append(
            {
                "code": "stuck_leases",
                "severity": "high",
                "message": (
                    f"аренд истекло: {int(gauges['tasks_stuck_leases'])} — "
                    "проверьте, что воркер возврата аренд запущен"
                ),
            }
        )
    if gauges["queue_oldest_age_seconds"] > 300:
        warnings.append(
            {
                "code": "queue_backlog",
                "severity": "high",
                "message": (
                    "старейшее задание в очереди ждёт "
                    f"{gauges['queue_oldest_age_seconds']:.0f} с — не хватает узлов "
                    "или они не забирают работу"
                ),
            }
        )
    if gauges["outbox_failed"] > 0:
        warnings.append(
            {
                "code": "outbox_dead_letter",
                "severity": "high",
                "message": (
                    f"событий в dead-letter: {int(gauges['outbox_failed'])} — "
                    "подписчики не получили уведомления, разберите причину"
                ),
            }
        )
    if gauges["outbox_oldest_pending_age_seconds"] > 120:
        warnings.append(
            {
                "code": "outbox_stalled",
                "severity": "medium",
                "message": (
                    "старейшее неотправленное событие ждёт "
                    f"{gauges['outbox_oldest_pending_age_seconds']:.0f} с — "
                    "проверьте, что воркер outbox запущен"
                ),
            }
        )
    if gauges["nodes_stale"] > 0:
        warnings.append(
            {
                "code": "stale_nodes",
                "severity": "medium",
                "message": (
                    f"узлов без heartbeat > 120 с: {int(gauges['nodes_stale'])} — "
                    "их аренды будут возвращены в очередь"
                ),
            }
        )
    if gauges["nodes_online"] == 0 and gauges["tasks_queued"] > 0:
        warnings.append(
            {
                "code": "no_nodes",
                "severity": "critical",
                "message": "нет ни одного online-узла, а задания в очереди есть",
            }
        )
    if gauges["compat_checks_failed"] > gauges["compat_checks_passed"] * 3:
        warnings.append(
            {
                "code": "compat_failures",
                "severity": "low",
                "message": "больше трёх провалов совместимости на успех — смотрите шаблоны",
            }
        )

    severities = {item["severity"] for item in warnings}
    status = "ok"
    if "critical" in severities:
        status = "critical"
    elif severities:
        status = "degraded"

    return {
        "status": status,
        "checked_at": _now().isoformat(),
        "gauges": gauges,
        "warnings": warnings,
    }


async def table_sizes(session: AsyncSession) -> list[dict[str, Any]]:
    """Размеры ключевых таблиц: план по росту и по партиционированию."""
    from app.services.gateway import table_row_counts

    counts = await table_row_counts(session)
    return [
        {"table": name, "rows": count}
        for name, count in sorted(counts.items(), key=lambda item: -item[1])
    ]


__all__ = [
    "NODE_STALE_AFTER",
    "collect_gauges",
    "health_report",
    "table_sizes",
]
