"""Схемы API оркестрации: планы, шаги, события (этап 4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.orchestration import FailurePolicy, PlanStatus, StepStatus

MAX_STEPS = 20
KEY_PATTERN = r"^[a-z0-9][a-z0-9_\-]{0,63}$"


class PlanStepCreate(BaseModel):
    """Шаг, объявленный клиентом вручную."""

    key: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=2, max_length=200)
    required_capabilities: list[str] = Field(default_factory=list, max_length=10)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    agent_id: uuid.UUID | None = None
    max_attempts: int = Field(default=2, ge=1, le=5)

    @field_validator("required_capabilities")
    @classmethod
    def _clean_capabilities(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            key = item.strip()
            if not key:
                continue
            if len(key) > 120:
                raise ValueError("capability key слишком длинный")
            if key not in cleaned:
                cleaned.append(key)
        return cleaned


class PlanCreate(BaseModel):
    """Создание плана.

    Либо шаги перечисляются явно (`steps`), либо задаётся `planner_agent_id` —
    тогда декомпозицию выполнит агент-планировщик (A2A, этап 4).
    """

    title: str = Field(min_length=2, max_length=200)
    goal: dict[str, Any] = Field(default_factory=dict)
    steps: list[PlanStepCreate] = Field(default_factory=list, max_length=MAX_STEPS)
    planner_agent_id: uuid.UUID | None = None
    max_parallelism: int = Field(default=4, ge=1, le=16)
    failure_policy: FailurePolicy = FailurePolicy.fail_plan
    step_timeout_seconds: int = Field(default=600, ge=5, le=86_400)
    auto_start: bool = True

    @model_validator(mode="after")
    def _validate_shape(self) -> PlanCreate:
        if not self.steps and self.planner_agent_id is None:
            raise ValueError("нужны шаги или агент-планировщик")
        if self.steps and self.planner_agent_id is not None:
            raise ValueError("укажите либо steps, либо planner_agent_id, но не оба")
        if not self.steps:
            return self
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("ключи шагов должны быть уникальны")
        # Существование зависимостей и ацикличность проверяет сервис:
        # так клиент получает точный код ошибки (unknown_dependency /
        # cyclic_dependencies), а не общий validation_error.
        for step in self.steps:
            if step.key in step.depends_on:
                raise ValueError(f"шаг {step.key} не может зависеть от себя")
        return self


class PlannerStep(BaseModel):
    """Шаг в ответе агента-планировщика (тот же формат, ключи обязательны)."""

    key: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=2, max_length=200)
    required_capabilities: list[str] = Field(default_factory=list, max_length=10)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    input: dict[str, Any] = Field(default_factory=dict)


class PlannerOutput(BaseModel):
    """Контракт декомпозиции: ровно это должен вернуть агент-планировщик.

    Формат намеренно простой и языко-независимый: любой агент на любом языке
    может вернуть такой JSON.
    """

    steps: list[PlannerStep] = Field(min_length=1, max_length=MAX_STEPS)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _validate_dag(self) -> PlannerOutput:
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("ключи шагов должны быть уникальны")
        known = set(keys)
        for step in self.steps:
            unknown = [dep for dep in step.depends_on if dep not in known]
            if unknown:
                raise ValueError(f"шаг {step.key} ссылается на неизвестные шаги: {unknown}")
            if step.key in step.depends_on:
                raise ValueError(f"шаг {step.key} не может зависеть от себя")
        return self


class PlanStepPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    position: int
    required_capabilities: list[str]
    depends_on: list[str]
    input_payload: dict[str, Any]
    resolved_input: dict[str, Any]
    output_payload: dict[str, Any]
    error: dict[str, Any] | None
    status: StepStatus
    agent_id: uuid.UUID | None
    task_id: uuid.UUID | None
    match_score: float | None
    attempts: int
    max_attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    note: str | None


class PlanPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    goal: dict[str, Any]
    status: PlanStatus
    max_parallelism: int
    failure_policy: FailurePolicy
    step_timeout_seconds: int
    planner_agent_id: uuid.UUID | None
    planner_message_id: uuid.UUID | None
    error: dict[str, Any] | None
    trace_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime | None = None


class PlanDetail(PlanPublic):
    steps: list[PlanStepPublic] = Field(default_factory=list)
    events: list[PlanEventPublic] = Field(default_factory=list)


class PlanEventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    event_type: str
    step_key: str | None
    actor: str
    message: str
    data: dict[str, Any]
    created_at: datetime


class PlanProgress(BaseModel):
    """Сводка исполнения плана."""

    plan_id: uuid.UUID
    status: PlanStatus
    total: int
    by_status: dict[str, int]
    ready: int
    running: int
    finished: int
    percent: float


class StepAssignmentPublic(BaseModel):
    """Результат подбора исполнителя для шага."""

    step_key: str
    applied: bool
    agent_id: uuid.UUID | None = None
    agent_slug: str | None = None
    agent_name: str | None = None
    score: float | None = None
    matched_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    reason: str | None = None


class PlanAssignmentResult(BaseModel):
    plan_id: uuid.UUID
    assigned: int
    blocked: int
    steps: list[StepAssignmentPublic]


class StepActionResult(BaseModel):
    step: PlanStepPublic
    task_id: uuid.UUID | None = None
    status: PlanStatus


class PlanRunResult(BaseModel):
    plan_id: uuid.UUID
    status: PlanStatus
    launched: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    progress: PlanProgress


class PlanAdoptRequest(BaseModel):
    """Принять декомпозицию, полученную от агента-планировщика."""

    steps: list[PlanStepCreate] = Field(min_length=1, max_length=MAX_STEPS)
    reason: str | None = Field(default=None, max_length=1000)


PlanSort = Literal["created_at", "-created_at", "status"]


__all__ = [
    "MAX_STEPS",
    "PlanAdoptRequest",
    "PlanAssignmentResult",
    "PlanCreate",
    "PlanDetail",
    "PlanEventPublic",
    "PlanProgress",
    "PlanPublic",
    "PlanRunResult",
    "PlanSort",
    "PlanStepCreate",
    "PlanStepPublic",
    "PlannerOutput",
    "PlannerStep",
    "StepActionResult",
    "StepAssignmentPublic",
]
