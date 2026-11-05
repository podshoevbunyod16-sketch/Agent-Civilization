"""Модели открытой экосистемы (этап 8).

Три сущности, которые делают цивилизацию доступной снаружи:

1. **Интеграции** (`integrations`) — каталог внешних платформ и сервисов,
   которые участники подключили к цивилизации. Запись проходит проверку
   доступности, поэтому каталог не превращается в список мёртвых ссылок.
2. **Проверки совместимости** (`compatibility_checks`) — реальный прогон
   объявленного контракта агента: вход по `input_schema`, выход по
   `output_schema`, тест-код в песочнице, ограничения манифеста. Проверка
   выполняется на настоящем payload, а не декларативно.
3. **Шаблоны проектов** (`project_templates`) — версионируемые скелеты агентов
   (Python, HTTP, Docker, MCP), которые Developer Portal предлагает скачать.

Ничего из этого не является исполняемой средой: сам агент по-прежнему живёт у
владельца, а backend лишь проверяет соблюдение контракта.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel


class IntegrationKind(enum.StrEnum):
    """Тип подключения внешней платформы."""

    ai_provider = "ai_provider"  # LLM или модельный провайдер (любой, не один)
    data_source = "data_source"  # данные, справочники, хранилища
    messaging = "messaging"  # чаты, почта, уведомления
    automation = "automation"  # n8n/Zapier/Make и подобные
    observability = "observability"  # трейсинг, метрики, логи
    custom = "custom"


class IntegrationProtocol(enum.StrEnum):
    """Протокол, по которому с интеграцией говорят."""

    http = "http"
    mcp = "mcp"
    webhook = "webhook"
    openapi = "openapi"


class CheckStatus(enum.StrEnum):
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"


class CheckKind(enum.StrEnum):
    """Что именно проверяется в объявленном контракте."""

    input_schema = "input_schema"
    output_schema = "output_schema"
    test_code = "test_code"
    limits = "limits"
    manifest = "manifest"


class TemplateRuntime(enum.StrEnum):
    python = "python"
    http = "http"
    docker = "docker"
    mcp = "mcp"


class Integration(BaseModel):
    """Внешняя платформа, подключённая участником."""

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", name="uq_integrations_owner_slug"),
        Index("ix_integrations_kind_public", "kind", "is_public"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(String(280), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[IntegrationKind] = mapped_column(
        Enum(IntegrationKind, name="integration_kind", native_enum=False, length=20),
        default=IntegrationKind.custom,
        nullable=False,
    )
    protocol: Mapped[IntegrationProtocol] = mapped_column(
        Enum(IntegrationProtocol, name="integration_protocol", native_enum=False, length=20),
        default=IntegrationProtocol.http,
        nullable=False,
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    docs_url: Mapped[str | None] = mapped_column(String(500))
    # Публичный манифест: как вызывать (схемы, примеры). Секретов здесь нет:
    # секреты остаются у участника, платформа их не хранит.
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Результат последней проверки доступности (HEAD/GET по base_url)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(40))
    last_latency_ms: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(String(300))
    check_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    owner = relationship("User", lazy="raise")


class CompatibilityCheck(BaseModel):
    """Реальный прогон контракта агента (этап 8)."""

    __tablename__ = "compatibility_checks"
    __table_args__ = (
        Index("ix_compat_agent_created", "agent_id", "created_at"),
        Index("ix_compat_status", "status"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="SET NULL")
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[CheckStatus] = mapped_column(
        Enum(CheckStatus, name="compat_status", native_enum=False, length=20),
        default=CheckStatus.running,
        nullable=False,
    )
    # Негативный прогон (проверка «поймает ли схема испорченный выход») не
    # должен влиять на готовность агента: он проверяет валидатор, а не агента.
    is_negative: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    passed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # [{"kind": ..., "status": ..., "message": ..., "details": {...}}, ...]
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    sample_input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    sample_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent = relationship("Agent", lazy="raise")


class ProjectTemplate(BaseModel):
    """Шаблон проекта агента для Developer Portal."""

    __tablename__ = "project_templates"
    __table_args__ = (UniqueConstraint("slug", name="uq_project_templates_slug"),)

    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(String(280), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    runtime: Mapped[TemplateRuntime] = mapped_column(
        Enum(TemplateRuntime, name="template_runtime", native_enum=False, length=20),
        default=TemplateRuntime.python,
        nullable=False,
    )
    language: Mapped[str] = mapped_column(String(40), default="python", nullable=False)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    requires_capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    downloads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


__all__ = [
    "CheckKind",
    "CheckStatus",
    "CompatibilityCheck",
    "Integration",
    "IntegrationKind",
    "IntegrationProtocol",
    "ProjectTemplate",
    "TemplateRuntime",
]
