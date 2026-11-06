"""Outbox исходящих событий (этап 10, D).

Проблема, которую это решает: `_publish_message_event` и события заданий
публиковались в шину **до** commit транзакции. Если транзакция затем
откатывалась, подписчики уже получили событие о том, чего нет в БД
(«фантомное» сообщение в WebSocket). Обратный порядок (публиковать после
commit) тоже неверен: падение процесса между commit и публикацией теряет
уведомление навсегда.

Решение — transactional outbox:

1. событие записывается в таблицу `event_outbox` **в той же транзакции**,
   что и доменное изменение;
2. фоновый воркер (`python -m app.worker.outbox`) читает неотправленные
   строки `FOR UPDATE SKIP LOCKED`, публикует в шину и ставит `published_at`;
3. повторная публикация возможна (at-least-once). Это осознанно: подписчик
   должен быть идемпотентен по `event_id`, а не «exactly-once», которого
   без распределённых транзакций не существует. Так и написано в ADR.

Что важно для честности:

* порядок в рамках одной темы сохраняется (`id` монотонный, выборка
  `ORDER BY id`), но между темами порядок не гарантируется;
* зависшие строки (`status=pending` слишком долго) не «теряются»: их
  возвращает `reclaim_stuck` (тот же проход обслуживания);
* события старше `retention` удаляются — outbox не растёт бесконечно.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import metrics
from app.db.models.events import EventOutbox, OutboxStatus

DEFAULT_RETENTION = timedelta(days=7)
MAX_ATTEMPTS = 10


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue(
    session: AsyncSession,
    *,
    topic: str,
    event: dict[str, Any],
    event_id: str | None = None,
) -> EventOutbox:
    """Кладёт событие в outbox (в текущую транзакцию)."""
    row = EventOutbox(
        topic=topic[:120],
        event_id=event_id or uuid.uuid4().hex,
        payload=event,
        status=OutboxStatus.pending,
        attempts=0,
        next_attempt_at=_now(),
    )
    session.add(row)
    await session.flush()
    # Идентификатор запоминается в сессии: после commit его события можно
    # опубликовать немедленно, не дожидаясь прохода воркера.
    session.info.setdefault("outbox_ids", []).append(row.id)
    return row


async def claim_batch(
    session: AsyncSession, *, limit: int = 100, now: datetime | None = None
) -> list[EventOutbox]:
    """Забирает пачку неотправленных событий, блокируя строки.

    `SKIP LOCKED` позволяет запускать несколько отправителей: каждый берёт
    свою пачку, дублирования работы нет.
    """
    now = now or _now()
    rows = (
        await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.status == OutboxStatus.pending,
                EventOutbox.next_attempt_at <= now,
            )
            .order_by(EventOutbox.created_at.asc(), EventOutbox.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for row in rows:
        row.status = OutboxStatus.sending
        row.attempts += 1
        row.last_attempt_at = now
    await session.flush()
    return list(rows)


async def mark_published(session: AsyncSession, row: EventOutbox) -> None:
    row.status = OutboxStatus.published
    row.published_at = _now()
    row.last_error = None
    await session.flush()
    metrics.incr("outbox_published")


async def mark_failed(session: AsyncSession, row: EventOutbox, error: str) -> None:
    """Экспоненциальный backoff; после `MAX_ATTEMPTS` — dead-letter.

    Dead-letter (`status=failed`) виден в `/ops/health` и метрике
    `outbox_dead_letter_total`: молча «потерять» событие нельзя.
    """
    row.last_error = error[:500]
    row.last_attempt_at = row.last_attempt_at or _now()
    if row.attempts >= MAX_ATTEMPTS:
        row.status = OutboxStatus.failed
        row.next_attempt_at = _now()
        metrics.incr("outbox_dead_letter")
    else:
        row.status = OutboxStatus.pending
        delay = min(2 ** row.attempts, 300)
        row.next_attempt_at = _now() + timedelta(seconds=delay)
        metrics.incr("outbox_retry")
    await session.flush()


async def reclaim_stuck(
    session: AsyncSession, *, older_than_seconds: int = 60
) -> int:
    """Возвращает в очередь события, застрявшие в статусе `sending`.

    Сценарий: отправитель упал между «взял пачку» и публикацией либо после
    публикации, но до commit. Повторная доставка возможна (at-least-once).
    """
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    rows = (
        await session.execute(
            select(EventOutbox).where(
                EventOutbox.status == OutboxStatus.sending,
                EventOutbox.last_attempt_at < cutoff,
            )
        )
    ).scalars().all()
    for row in rows:
        row.status = OutboxStatus.pending
        row.next_attempt_at = _now()
    if rows:
        await session.flush()
        metrics.incr("outbox_reclaimed", len(rows))
    return len(rows)


async def purge_published(
    session: AsyncSession, *, retention: timedelta = DEFAULT_RETENTION
) -> int:
    """Удаляет опубликованные события старше retention."""
    cutoff = _now() - retention
    result = await session.execute(
        EventOutbox.__table__.delete().where(
            EventOutbox.status == OutboxStatus.published,
            EventOutbox.published_at < cutoff,
        )
    )
    return int(result.rowcount or 0)


async def stats(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(EventOutbox.status, func.count()).group_by(EventOutbox.status)
        )
    ).all()
    result = {status.value: 0 for status in OutboxStatus}
    for status, count in rows:
        key = status.value if hasattr(status, "value") else str(status)
        result[key] = int(count)
    return result


async def publish_batch(session: AsyncSession, *, limit: int = 100) -> dict[str, int]:
    """Публикует пачку событий. Возвращает счётчики для логов и метрик."""
    from app.core.events import event_bus

    rows = await claim_batch(session, limit=limit)
    published = 0
    failed = 0
    for row in rows:
        try:
            # `publish` сам доставляет локальным подписчикам и рассылает в
            # Redis для остальных реплик. Вызывать `publish_local` отдельно
            # нельзя: подписчики получили бы событие дважды.
            await event_bus.publish(row.topic, row.payload)
            await mark_published(session, row)
            published += 1
        except Exception as exc:  # pragma: no cover - сбой шины
            await mark_failed(session, row, str(exc))
            failed += 1
    return {"claimed": len(rows), "published": published, "failed": failed}


async def dispatch_pending(session: AsyncSession, *, limit: int = 200) -> int:
    """Публикует события, записанные этой сессией, сразу после commit.

    Зачем не ждать воркера: задержка доставки в WebSocket измеряется
    секундами, и держать её на уровне «следующий проход фонового цикла»
    бессмысленно. Событие сначала надёжно записано в БД (durability), затем
    сразу публикуется лучшим усилием (latency). Если публикация не удалась,
    строка остаётся `pending` и её заберёт воркер — это и есть at-least-once.
    """
    from app.core.events import event_bus

    ids = session.info.pop("outbox_ids", [])
    if not ids:
        return 0
    rows = (
        await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.id.in_(ids[:limit]),
                EventOutbox.status == OutboxStatus.pending,
            )
            .order_by(EventOutbox.created_at.asc())
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    published = 0
    for row in rows:
        try:
            await event_bus.publish(row.topic, row.payload)
            await mark_published(session, row)
            published += 1
        except Exception as exc:  # pragma: no cover - шина недоступна
            row.last_error = str(exc)[:500]
    await session.flush()
    return published


async def commit_and_dispatch(session: AsyncSession) -> None:
    """Commit доменной транзакции + немедленная доставка её событий."""
    await session.commit()
    if session.info.get("outbox_ids"):
        await dispatch_pending(session)
        # Фиксируем published_at: иначе воркер отправит событие повторно.
        await session.commit()
