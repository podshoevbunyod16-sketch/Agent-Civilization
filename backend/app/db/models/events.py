"""Модель таблицы outbox (этап 10, D)."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class OutboxStatus(enum.StrEnum):
    pending = "pending"
    sending = "sending"
    published = "published"
    failed = "failed"  # dead-letter: исчерпаны попытки


class EventOutbox(BaseModel):
    """Исходящее событие, записанное в транзакции с доменным изменением.

    `status` + `attempts` + `next_attempt_at` дают:
    * повтор с backoff при сбое публикации;
    * dead-letter после `MAX_ATTEMPTS` (виден в /ops/health);
    * возврат «зависших» строк (`sending` дольше минуты).
    """

    __tablename__ = "event_outbox"
    __table_args__ = (
        Index("ix_event_outbox_pending", "status", "next_attempt_at"),
        Index("ix_event_outbox_topic", "topic", "created_at"),
    )

    topic: Mapped[str] = mapped_column(String(120), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status", native_enum=False, length=20),
        default=OutboxStatus.pending,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
