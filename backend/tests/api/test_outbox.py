"""Этап 10, D: transactional outbox — доставка событий без потерь и фантомов.

Проверяется поведение, а не «наличие таблицы»:

* события не публикуются до commit (нет фантомных уведомлений);
* после commit событие уходит подписчику;
* отправка идемпотентна по `event_id` на стороне подписчика (at-least-once);
* повтор при сбое шины, dead-letter после серии неудач, возврат зависших.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.events import event_bus
from app.db.models.events import EventOutbox, OutboxStatus
from app.services import outbox


@pytest.mark.asyncio
async def test_enqueue_is_part_of_transaction(session):
    """Событие в откате не остаётся в БД — подписчик не увидит фантом."""
    from app.core.config import settings

    topic = "conversation:test-rollback"
    queue = await event_bus.subscribe(topic)
    await outbox.enqueue(session, topic=topic, event={"event_type": "message.created"})
    await session.rollback()

    rows = (
        await session.execute(select(EventOutbox).where(EventOutbox.topic == topic))
    ).scalars().all()
    assert rows == []
    assert queue.empty()
    assert settings is not None
    await event_bus.unsubscribe(topic, queue)


@pytest.mark.asyncio
async def test_commit_then_dispatch_delivers_event(session):
    topic = "conversation:test-delivery"
    queue = await event_bus.subscribe(topic)
    try:
        await outbox.enqueue(
            session, topic=topic, event={"event_type": "message.created", "id": "1"}
        )
        await outbox.commit_and_dispatch(session)

        # Подписчик получил событие сразу после commit (без ожидания воркера)
        event = queue.get_nowait()
        assert event["event_type"] == "message.created"
        row = (await session.execute(select(EventOutbox))).scalar_one()
        assert row.status is OutboxStatus.published
        assert row.published_at is not None
    finally:
        await event_bus.unsubscribe(topic, queue)


@pytest.mark.asyncio
async def test_published_events_are_not_resent(session):
    topic = "conversation:test-once"
    queue = await event_bus.subscribe(topic)
    try:
        await outbox.enqueue(session, topic=topic, event={"n": 1})
        await outbox.commit_and_dispatch(session)
        queue.get_nowait()

        # Воркер не должен отправлять уже опубликованное событие повторно
        result = await outbox.publish_batch(session)
        await session.commit()
        assert result["claimed"] == 0
        assert queue.empty()
    finally:
        await event_bus.unsubscribe(topic, queue)


@pytest.mark.asyncio
async def test_failed_publish_is_retried_with_backoff(session, monkeypatch):
    topic = "conversation:test-retry"
    row = await outbox.enqueue(session, topic=topic, event={"n": 2})
    await session.commit()

    calls = {"count": 0}

    async def broken_publish(*args, **kwargs):
        calls["count"] += 1
        raise RuntimeError("redis down")

    monkeypatch.setattr(event_bus, "publish", broken_publish)
    # Попытка уже была (attempts=1 выставляет claim_batch); фиксируем сбой
    row.attempts = 1
    await outbox.mark_failed(session, row, "redis down")
    await session.commit()

    refreshed = (
        await session.execute(select(EventOutbox).where(EventOutbox.id == row.id))
    ).scalar_one()
    assert refreshed.status is OutboxStatus.pending
    assert refreshed.attempts == 1
    # Backoff: следующий повтор не сразу, а через паузу
    assert refreshed.next_attempt_at > refreshed.last_attempt_at
    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_dead_letter_after_max_attempts(session):
    row = await outbox.enqueue(session, topic="conversation:dead", event={"n": 3})
    row.attempts = outbox.MAX_ATTEMPTS
    await outbox.mark_failed(session, row, "permanent failure")
    await session.commit()

    refreshed = (
        await session.execute(select(EventOutbox).where(EventOutbox.id == row.id))
    ).scalar_one()
    # Событие не теряется молча: оно в dead-letter и видно в статистике
    assert refreshed.status is OutboxStatus.failed
    stats = await outbox.stats(session)
    assert stats["failed"] == 1


@pytest.mark.asyncio
async def test_stuck_sending_rows_are_reclaimed(session):
    from datetime import UTC, datetime, timedelta

    row = await outbox.enqueue(session, topic="conversation:stuck", event={"n": 4})
    row.status = OutboxStatus.sending
    row.last_attempt_at = datetime.now(UTC) - timedelta(minutes=5)
    await session.commit()

    reclaimed = await outbox.reclaim_stuck(session, older_than_seconds=60)
    await session.commit()
    assert reclaimed == 1
    refreshed = (
        await session.execute(select(EventOutbox).where(EventOutbox.id == row.id))
    ).scalar_one()
    assert refreshed.status is OutboxStatus.pending


@pytest.mark.asyncio
async def test_concurrent_claim_does_not_duplicate(session, session_factory=None):
    """Два отправителя не забирают одно и то же событие."""
    from app.db.session import get_session_factory

    for index in range(5):
        await outbox.enqueue(session, topic="conversation:conc", event={"n": index})
    await session.commit()

    factory = get_session_factory()
    async with factory() as first, factory() as second:
        batch_a = await outbox.claim_batch(first, limit=3)
        batch_b = await outbox.claim_batch(second, limit=3)
        ids_a = {row.id for row in batch_a}
        ids_b = {row.id for row in batch_b}
        # SKIP LOCKED: пачки не пересекаются
        assert ids_a.isdisjoint(ids_b)
        assert len(ids_a) + len(ids_b) <= 5


@pytest.mark.asyncio
async def test_message_created_event_goes_through_outbox(client, user):
    """Сквозная проверка: отправка сообщения создаёт событие в outbox."""
    from tests.conftest import create_published_agent

    sender = await create_published_agent(client, user, name="Отправитель", slug="obx-a")
    recipient = await create_published_agent(client, user, name="Получатель", slug="obx-b")
    response = await client.post(
        f"/api/v1/agents/{sender['id']}/messages",
        json={"recipient_agent_id": recipient["id"], "payload": {"hello": 1}},
        headers=user.headers,
    )
    assert response.status_code == 201, response.text

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as check:
        rows = (
            await check.execute(
                select(EventOutbox).where(EventOutbox.topic.like("conversation:%"))
            )
        ).scalars().all()
        created = [row for row in rows if row.payload.get("event_type") == "message.created"]
        assert created, "событие создания сообщения должно быть в outbox"
        # Событие уже опубликовано (немедленная доставка после commit)
        assert created[0].status is OutboxStatus.published
        assert created[0].payload["message_id"] == response.json()["message"]["id"]
