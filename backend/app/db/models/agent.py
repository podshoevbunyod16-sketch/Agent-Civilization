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
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel


class AgentStatus(enum.StrEnum):
    draft = "draft"
    published = "published"
    suspended = "suspended"
    archived = "archived"


class EntrypointType(enum.StrEnum):
    """Как система запускает агента. Провайдер-агностично."""

    http = "http"          # внешний endpoint участника (A2A/HTTP)
    python = "python"      # код агента в изолированной среде (sandbox)
    docker = "docker"      # образ в изолированном раннере
    mcp = "mcp"            # MCP-сервер с инструментами


class Agent(BaseModel):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "reputation_score >= 0 AND reputation_score <= 100", name="reputation_range"
        ),
        Index("ix_agents_status_published_at", "status", "published_at"),
    )

    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(String(280), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status", native_enum=False, length=20),
        default=AgentStatus.draft,
        nullable=False,
        index=True,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    category: Mapped[str] = mapped_column(String(60), default="general", nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(String(500))
    repository_url: Mapped[str | None] = mapped_column(String(500))
    accent_color: Mapped[str] = mapped_column(String(9), default="#6ee7b7", nullable=False)

    current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reputation_score: Mapped[float] = mapped_column(
        Numeric(5, 2), default=50.0, nullable=False
    )
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, nullable=False
    )

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship(back_populates="agents", lazy="joined")  # noqa: F821
    # passive_deletes=True: удаление дочерних строк выполняет PostgreSQL
    # (ON DELETE CASCADE). Без этого SQLAlchemy пытается обнулить agent_id
    # у версий/возможностей и падает на NOT NULL.
    versions: Mapped[list[AgentVersion]] = relationship(
        back_populates="agent",
        lazy="selectin",
        order_by="AgentVersion.version.desc()",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    capabilities: Mapped[list[AgentCapability]] = relationship(
        back_populates="agent",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AgentVersion(BaseModel):
    """Иммутабельная версия агента. Публикация фиксирует снимок конфигурации."""

    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_version"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    changelog: Mapped[str] = mapped_column(Text, default="", nullable=False)
    entrypoint_type: Mapped[EntrypointType] = mapped_column(
        Enum(EntrypointType, name="entrypoint_type", native_enum=False, length=20),
        default=EntrypointType.http,
        nullable=False,
    )
    entrypoint_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    required_capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    resource_limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent: Mapped[Agent] = relationship(back_populates="versions")

    @property
    def is_published(self) -> bool:
        return self.published_at is not None


class AgentCapability(BaseModel):
    """Связь агент ↔ возможность.

    Важно: `declared_level` заявляет разработчик, `verified_level`
    выставляется только системой проверки (этап 5).
    """

    __tablename__ = "agent_capabilities"
    __table_args__ = (
        UniqueConstraint("agent_id", "capability_key", name="uq_agent_capability"),
        CheckConstraint("declared_level >= 0 AND declared_level <= 5", name="declared_level_range"),
        CheckConstraint("verified_level >= 0 AND verified_level <= 5", name="verified_level_range"),
        Index("ix_agent_capabilities_key", "capability_key"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    capability_key: Mapped[str] = mapped_column(String(80), nullable=False)
    declared_level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    verified_level: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(280))

    agent: Mapped[Agent] = relationship(back_populates="capabilities")
