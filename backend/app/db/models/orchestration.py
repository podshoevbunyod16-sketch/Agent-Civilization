"""Модели оркестрации: план задания и его шаги (этап 4).

План — это DAG шагов. Каждый шаг связан с реальным заданием (`tasks`) и
исполняется существующим runtime (узел/песочница), поэтому оркестрация не
дублирует движок исполнения, а только решает «что, кем и в каком порядке».
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel


class PlanStatus(enum.StrEnum):
    draft = "draft"        # шаги ещё не назначены
    planned = "planned"    # шаги назначены, можно запускать
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class StepStatus(enum.StrEnum):
    pending = "pending"    # ждёт зависимостей
    ready = "ready"        # зависимости выполнены, можно запускать
    queued = "queued"      # задание создано, ждёт узел
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"    # не запускался из-за провала зависимости
    blocked = "blocked"    # нет подходящего агента


class FailurePolicy(enum.StrEnum):
    fail_plan = "fail_plan"   # провал шага валит весь план
    continue_ = "continue"    # зависимые шаги блокируются, остальные идут


TERMINAL_STEP_STATUSES = frozenset(
    {StepStatus.succeeded, StepStatus.failed, StepStatus.skipped, StepStatus.blocked}
)
TERMINAL_PLAN_STATUSES = frozenset(
    {PlanStatus.succeeded, PlanStatus.failed, PlanStatus.cancelled}
)


class TaskPlan(BaseModel):
    """План выполнения составного задания."""

    __tablename__ = "task_plans"

    requester_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    goal: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus, name="plan_status", native_enum=False, length=20),
        default=PlanStatus.draft,
        nullable=False,
        index=True,
    )
    max_parallelism: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    failure_policy: Mapped[FailurePolicy] = mapped_column(
        Enum(FailurePolicy, name="plan_failure_policy", native_enum=False, length=20),
        default=FailurePolicy.fail_plan,
        nullable=False,
    )
    step_timeout_seconds: Mapped[int] = mapped_column(Integer, default=600, nullable=False)

    planner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    # A2A-сообщение, которым запросили декомпозицию у агента-планировщика
    planner_message_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list[PlanStep]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PlanStep.position",
        lazy="raise",
    )

    __table_args__ = (Index("ix_plans_requester_status", "requester_id", "status"),)


class PlanStep(BaseModel):
    """Шаг плана: одна задача для одного агента."""

    __tablename__ = "plan_steps"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("task_plans.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Возможности, которым должен соответствовать исполнитель
    required_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    # Ключи шагов-зависимостей (топологический порядок не обязателен)
    depends_on: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # Что реально получил исполнитель: цель плана + собственный вход шага +
    # результаты зависимостей на момент запуска. Нужен для отладки и аудита:
    # `output_payload` перезаписывается результатом задачи, а вход — нет.
    resolved_input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    status: Mapped[StepStatus] = mapped_column(
        Enum(StepStatus, name="plan_step_status", native_enum=False, length=20),
        default=StepStatus.pending,
        nullable=False,
        index=True,
    )

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    # Оценка соответствия исполнителя на момент назначения (0..1)
    match_score: Mapped[float | None] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)

    plan: Mapped[TaskPlan] = relationship(back_populates="steps", lazy="raise")

    __table_args__ = (
        UniqueConstraint("plan_id", "key", name="uq_plan_step_key"),
        Index("ix_steps_plan_status", "plan_id", "status"),
    )


class PlanEvent(BaseModel):
    """Журнал изменений плана (аналог task_events, для аудита оркестрации)."""

    __tablename__ = "plan_events"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("task_plans.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    step_key: Mapped[str | None] = mapped_column(String(80))
    # actor может быть "user:<uuid>" или "node:<node_key>"
    actor: Mapped[str] = mapped_column(String(120), default="system", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_plan_events_plan_seq", "plan_id", "sequence"),
        UniqueConstraint("plan_id", "sequence", name="uq_plan_event_sequence"),
    )


__all__ = [
    "FailurePolicy",
    "PlanEvent",
    "PlanStatus",
    "PlanStep",
    "StepStatus",
    "TERMINAL_PLAN_STATUSES",
    "TERMINAL_STEP_STATUSES",
    "TaskPlan",
]
