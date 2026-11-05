from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.node import NodeHealth, NodeKind, NodeStatus


class NodeRegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    kind: NodeKind = NodeKind.community
    region: str = Field(default="global", max_length=60)
    endpoint_url: str | None = Field(default=None, max_length=500)
    cpu_cores: int = Field(default=1, ge=1, le=512)
    memory_mb: int = Field(default=512, ge=128, le=2_097_152)
    max_concurrency: int = Field(default=2, ge=1, le=1024)
    limit_cpu_seconds: int = Field(default=60, ge=1, le=3600)
    limit_wall_seconds: int = Field(default=120, ge=1, le=7200)
    limit_memory_mb: int = Field(default=512, ge=64, le=32768)
    allow_network: bool = False
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    labels: dict[str, Any] = Field(default_factory=dict)

    @field_validator("endpoint_url")
    @classmethod
    def _valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("endpoint_url должен начинаться с http:// или https://")
        return value


class NodeCredentials(BaseModel):
    """Секрет возвращается один раз при регистрации/ротации."""

    node_key: str
    node_secret: str
    warning: str = "Сохраните node_secret: он больше не будет показан"


class NodeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    region: str | None = Field(default=None, max_length=60)
    endpoint_url: str | None = Field(default=None, max_length=500)
    max_concurrency: int | None = Field(default=None, ge=1, le=1024)
    limit_cpu_seconds: int | None = Field(default=None, ge=1, le=3600)
    limit_wall_seconds: int | None = Field(default=None, ge=1, le=7200)
    limit_memory_mb: int | None = Field(default=None, ge=64, le=32768)
    allow_network: bool | None = None
    capabilities: list[str] | None = None
    labels: dict[str, Any] | None = None


class NodePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_key: str
    name: str
    kind: NodeKind
    status: NodeStatus
    health: NodeHealth
    region: str
    endpoint_url: str | None
    cpu_cores: int
    memory_mb: int
    max_concurrency: int
    running_tasks: int
    limit_cpu_seconds: int
    limit_wall_seconds: int
    limit_memory_mb: int
    allow_network: bool
    capabilities: list[str]
    labels: dict[str, Any]
    reputation_score: float
    trust_level: int
    last_seen_at: datetime | None
    verified_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: str | None
    owner_id: uuid.UUID
    created_at: datetime


class HeartbeatRequest(BaseModel):
    sequence: int = Field(default=0, ge=0)
    running_tasks: int = Field(default=0, ge=0)
    cpu_load: float = Field(default=0.0, ge=0, le=100)
    memory_used_mb: int = Field(default=0, ge=0)
    health: NodeHealth = NodeHealth.healthy
    details: dict[str, Any] = Field(default_factory=dict)


class HeartbeatResponse(BaseModel):
    acknowledged: bool = True
    status: NodeStatus
    server_time: datetime
    lease_seconds: int


# ── Task assignment (Execution Gateway, pull-модель для узлов) ─────────
class TaskClaimRequest(BaseModel):
    """Узел забирает готовую задачу из очереди."""

    capabilities: list[str] = Field(default_factory=list)
    max_tasks: int = Field(default=1, ge=1, le=10)


class TaskAssignment(BaseModel):
    task_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID | None
    entrypoint_type: str
    entrypoint_config: dict[str, Any]
    input_payload: dict[str, Any]
    resource_limits: dict[str, Any]
    lease_token: str
    lease_expires_at: datetime
    deadline_at: datetime | None
    attempt: int
    trace_id: str | None


class TaskClaimResponse(BaseModel):
    assignments: list[TaskAssignment]


class TaskProgressRequest(BaseModel):
    lease_token: str
    message: str = Field(default="", max_length=500)
    data: dict[str, Any] = Field(default_factory=dict)


class TaskResultRequest(BaseModel):
    lease_token: str
    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = Field(default=None, max_length=2000)
    cost_units: float = Field(default=0, ge=0, le=1_000_000)
    metrics: dict[str, Any] = Field(default_factory=dict)


class TaskResultResponse(BaseModel):
    task_id: uuid.UUID
    status: str
    will_retry: bool


# ── Очередь / задачи (клиентская часть) ───────────────────────────────
class TaskSubmitRequest(BaseModel):
    agent_id: uuid.UUID
    input_payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=0, le=10)
    max_attempts: int = Field(default=2, ge=1, le=5)
    timeout_seconds: int = Field(default=600, ge=5, le=86_400)
    budget_units: float | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, max_length=120)
    wait: bool = False


class TaskCancelRequest(BaseModel):
    reason: str = Field(default="", max_length=280)


class TaskPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID | None
    node_id: uuid.UUID | None
    status: str
    priority: int
    attempts: int
    max_attempts: int
    input_payload: dict[str, Any]
    output_payload: dict[str, Any]
    error: dict[str, Any] | None
    result_hash: str | None
    cost_units: float
    # Бюджет, замороженный при постановке (этап 6). Раньше принимался, но не
    # возвращался — клиент не мог подтвердить, что деньги заморожены.
    budget_units: float | None = None
    lease_expires_at: datetime | None
    deadline_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    trace_id: str | None
    created_at: datetime
    updated_at: datetime


class TaskEventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    event_type: str
    actor: str
    message: str
    data: dict[str, Any]
    created_at: datetime


class NodeHeartbeatPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    running_tasks: int
    cpu_load: float
    memory_used_mb: int
    health: NodeHealth
    created_at: datetime


class AuditEntryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    actor_id: uuid.UUID | None
    actor_type: str
    action: str
    target_type: str
    target_id: str | None
    outcome: str
    ip_address: str | None
    request_id: str | None
    data: dict[str, Any]


class AgentSortLiteral(BaseModel):
    sort: Literal["created_at", "published_at", "reputation", "name", "completed_tasks"] = (
        "created_at"
    )
