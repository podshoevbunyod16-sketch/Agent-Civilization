"""Модели верификации и доверия (этап 5).

Ключевая идея: репутация — не одно число, которое кто-то «выставляет»,
а **сумма проверяемых вкладов**. Каждое изменение репутации — строка
`reputation_events` со ссылкой на источник доверия (проверку, отзыв,
апелляцию, аномалию). Число в `agents.reputation_score` — производная,
пересчитываемая из событий, поэтому его всегда можно объяснить и отозвать.

Проверка результата (`task_verifications`) — отдельная сущность:

* автоматические проверки исполняются **в песочнице** (никогда в процессе API);
* независимая проверка выполняется другим агентом или участником, который
  не совпадает с исполнителем;
* итоговый вердикт — агрегат проверок с весами, а не мнение одного звена.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class VerificationMethod(enum.StrEnum):
    """Кто/что проверяет результат."""

    schema = "schema"            # соответствие output_schema версии агента
    auto_test = "auto_test"      # тест-код, исполняемый в песочнице
    assertions = "assertions"    # декларативные проверки (пути/операторы)
    peer_agent = "peer_agent"    # независимый агент-верификатор
    human = "human"              # владелец задания / админ
    cross_check = "cross_check"  # сравнение с результатом другого исполнителя


class VerificationStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"      # проверку не удалось выполнить (инфраструктура)
    skipped = "skipped"  # проверка не применима

    @property
    def is_finished(self) -> bool:
        return self in {
            VerificationStatus.passed,
            VerificationStatus.failed,
            VerificationStatus.error,
            VerificationStatus.skipped,
        }


class Verdict(enum.StrEnum):
    """Итог по задаче после агрегации проверок."""

    unverified = "unverified"  # проверок нет или они не завершены
    verified = "verified"      # результат подтверждён
    rejected = "rejected"      # результат опровергнут
    disputed = "disputed"      # есть противоречивые проверки


class ReputationSource(enum.StrEnum):
    """Источник вклада в репутацию."""

    task_success = "task_success"
    task_failure = "task_failure"
    verification_passed = "verification_passed"
    verification_failed = "verification_failed"
    review = "review"
    anomaly_penalty = "anomaly_penalty"
    appeal = "appeal"
    manual = "manual"


class AppealStatus(enum.StrEnum):
    open = "open"
    accepted = "accepted"
    rejected = "rejected"
    withdrawn = "withdrawn"

    @property
    def is_terminal(self) -> bool:
        return self is not AppealStatus.open


class AnomalyType(enum.StrEnum):
    rapid_results = "rapid_results"        # подозрительно быстрые ответы
    result_reuse = "result_reuse"          # одинаковый результат на разные входы
    flaky_output = "flaky_output"          # нестабильный результат на тот же вход
    failure_spike = "failure_spike"        # резкий рост отказов
    self_dealing = "self_dealing"          # агент «проверяет» сам себя
    review_ring = "review_ring"            # круг взаимных отзывов


class AnomalyStatus(enum.StrEnum):
    open = "open"
    confirmed = "confirmed"
    dismissed = "dismissed"


class TaskVerification(BaseModel):
    """Одна проверка результата задания."""

    __tablename__ = "task_verifications"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    # Кто инициировал проверку (для независимой верификации — другой агент/человек)
    verifier_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    verifier_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )

    method: Mapped[VerificationMethod] = mapped_column(
        Enum(VerificationMethod, name="verification_method", native_enum=False, length=20),
        nullable=False,
    )
    status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status", native_enum=False, length=20),
        default=VerificationStatus.pending,
        nullable=False,
        index=True,
    )
    # Вес проверки при агрегации (независимые проверки весят больше)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    passed: Mapped[bool | None] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Float)  # 0..1, для частичных результатов
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Идемпотентность повторной проверки тем же способом
    dedup_key: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("task_id", "dedup_key", name="uq_verification_dedup"),
        CheckConstraint("weight > 0", name="verification_weight_positive"),
    )


class TaskVerdict(BaseModel):
    """Итоговый вердикт по заданию (одна строка на задачу)."""

    __tablename__ = "task_verdicts"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    verdict: Mapped[Verdict] = mapped_column(
        Enum(Verdict, name="verdict", native_enum=False, length=20),
        default=Verdict.unverified,
        nullable=False,
        index=True,
    )
    score: Mapped[float | None] = mapped_column(Float)  # агрегированная уверенность 0..1
    checks_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks_passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    independent_checks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_verdict_agent_verdict", "agent_id", "verdict"),)


class ReputationEvent(BaseModel):
    """Вклад в репутацию: единственный источник истины о рейтинге агента."""

    __tablename__ = "reputation_events"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[ReputationSource] = mapped_column(
        Enum(ReputationSource, name="reputation_source", native_enum=False, length=30),
        nullable=False,
    )
    # Вклад в «доверие»: положительно — успех, отрицательно — провал
    delta: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    reason: Mapped[str] = mapped_column(String(280), default="", nullable=False)

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_verifications.id", ondelete="SET NULL")
    )
    review_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    anomaly_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # Отзыв (компенсация) вклада — при апелляции/пересмотре
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by_appeal_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    # Идемпотентность: один источник — один вклад
    dedup_key: Mapped[str | None] = mapped_column(String(160))

    __table_args__ = (
        UniqueConstraint("agent_id", "dedup_key", name="uq_reputation_dedup"),
        Index("ix_reputation_agent_source", "agent_id", "source"),
    )


class TaskReview(BaseModel):
    """Отзыв владельца задания о результате (только по своим заданиям)."""

    __tablename__ = "task_reviews"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..5
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Отзыв от владельца задания считается подтверждённым результатом
    confirmed_result: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="review_rating_range"),
    )


class Appeal(BaseModel):
    """Апелляция на решение по качеству (вердикт, проверку, аномалию, отзыв)."""

    __tablename__ = "appeals"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    opened_by_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL")
    )
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("task_verifications.id", ondelete="SET NULL")
    )
    anomaly_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[AppealStatus] = mapped_column(
        Enum(AppealStatus, name="appeal_status", native_enum=False, length=20),
        default=AppealStatus.open,
        nullable=False,
        index=True,
    )
    resolution: Mapped[str] = mapped_column(Text, default="", nullable=False)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnomalySignal(BaseModel):
    """Обнаруженная подозрительная активность агента."""

    __tablename__ = "anomaly_signals"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    anomaly_type: Mapped[AnomalyType] = mapped_column(
        Enum(AnomalyType, name="anomaly_type", native_enum=False, length=30),
        nullable=False,
    )
    status: Mapped[AnomalyStatus] = mapped_column(
        Enum(AnomalyStatus, name="anomaly_status", native_enum=False, length=20),
        default=AnomalyStatus.open,
        nullable=False,
        index=True,
    )
    severity: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)  # 0..1
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # Сколько задач затронуто сигналом
    task_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Идемпотентность: одна аномалия типа X для агента не дублируется
    dedup_key: Mapped[str | None] = mapped_column(String(160))

    __table_args__ = (
        UniqueConstraint("agent_id", "dedup_key", name="uq_anomaly_dedup"),
        CheckConstraint("severity >= 0 AND severity <= 1", name="anomaly_severity_range"),
    )


__all__ = [
    "AnomalySignal",
    "AnomalyStatus",
    "AnomalyType",
    "Appeal",
    "AppealStatus",
    "ReputationEvent",
    "ReputationSource",
    "TaskReview",
    "TaskVerdict",
    "TaskVerification",
    "Verdict",
    "VerificationMethod",
    "VerificationStatus",
]
