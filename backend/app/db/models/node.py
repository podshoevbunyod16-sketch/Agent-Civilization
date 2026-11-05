from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel


class NodeKind(enum.StrEnum):
    cloud = "cloud"              # узел платформы
    community = "community"      # сервер участника
    edge = "edge"


class NodeStatus(enum.StrEnum):
    pending = "pending"      # ждёт подтверждения владельцем/админом
    online = "online"
    degraded = "degraded"
    offline = "offline"
    revoked = "revoked"


class NodeHealth(enum.StrEnum):
    unknown = "unknown"
    healthy = "healthy"
    unhealthy = "unhealthy"


class InfrastructureNode(BaseModel):
    """Инфраструктурный узел: облачный worker или сервер участника.

    Узел аутентифицируется подписью HMAC-SHA256 (node_key + node_secret),
    секрет хранится только в виде хеша.
    """

    __tablename__ = "infrastructure_nodes"
    __table_args__ = (
        CheckConstraint("max_concurrency > 0", name="max_concurrency_positive"),
        CheckConstraint("cpu_cores >= 0", name="cpu_non_negative"),
        CheckConstraint("memory_mb >= 0", name="memory_non_negative"),
        Index("ix_nodes_status_health", "status", "health"),
    )

    node_key: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[NodeKind] = mapped_column(
        Enum(NodeKind, name="node_kind", native_enum=False, length=20),
        default=NodeKind.cloud,
        nullable=False,
    )
    status: Mapped[NodeStatus] = mapped_column(
        Enum(NodeStatus, name="node_status", native_enum=False, length=20),
        default=NodeStatus.pending,
        nullable=False,
    )
    health: Mapped[NodeHealth] = mapped_column(
        Enum(NodeHealth, name="node_health", native_enum=False, length=20),
        default=NodeHealth.unknown,
        nullable=False,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    region: Mapped[str] = mapped_column(String(60), default="global", nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(500))

    cpu_cores: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    running_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Ограничения, которые узел обязан применять к задачам
    limit_cpu_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    limit_wall_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    limit_memory_mb: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    allow_network: Mapped[bool] = mapped_column(default=False, nullable=False)

    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    reputation_score: Mapped[float] = mapped_column(Numeric(5, 2), default=50.0, nullable=False)
    trust_level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(280))

    heartbeats: Mapped[list[NodeHeartbeat]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )


class NodeHeartbeat(BaseModel):
    __tablename__ = "node_heartbeats"
    __table_args__ = (
        UniqueConstraint("node_id", "sequence", name="uq_node_heartbeat_sequence"),
        Index("ix_heartbeat_node_created", "node_id", "created_at"),
    )

    node_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("infrastructure_nodes.id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    running_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cpu_load: Mapped[float] = mapped_column(Numeric(5, 2), default=0, nullable=False)
    memory_used_mb: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    health: Mapped[NodeHealth] = mapped_column(
        Enum(NodeHealth, name="heartbeat_health", native_enum=False, length=20),
        default=NodeHealth.healthy,
        nullable=False,
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    node: Mapped[InfrastructureNode] = relationship(back_populates="heartbeats")
