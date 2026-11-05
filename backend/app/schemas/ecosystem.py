"""Схемы API открытой экосистемы (этап 8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.ecosystem import (
    CheckStatus,
    IntegrationKind,
    IntegrationProtocol,
    TemplateRuntime,
)
from app.schemas.common import CAPABILITY_KEY_RE, SLUG_RE

IntegrationKindLiteral = Literal[
    "ai_provider", "data_source", "messaging", "automation", "observability", "custom"
]
IntegrationProtocolLiteral = Literal["http", "mcp", "webhook", "openapi"]


def _check_slug(value: str) -> str:
    if not SLUG_RE.match(value or ""):
        raise ValueError(
            "slug: строчные латинские буквы, цифры и дефис, 3–64 символа, "
            "начинается и заканчивается буквой или цифрой"
        )
    return value


class CompatibilityCheckRequest(BaseModel):
    """Запрос проверки совместимости. Пример входа можно задать явно."""

    sample_input: dict[str, Any] | None = Field(
        default=None, description="Пример входа; по умолчанию берётся из манифеста версии"
    )
    imperfect_output: dict[str, Any] | None = Field(
        default=None,
        description="Негативный тест: заранее заданный выход, который схема должна отклонить",
    )


class CompatibilityCheckItem(BaseModel):
    kind: str
    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CompatibilityCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_id: uuid.UUID | None
    requested_by: uuid.UUID
    status: CheckStatus
    is_negative: bool = False
    score: float
    passed_count: int
    failed_count: int
    checks: list[dict[str, Any]]
    sample_input: dict[str, Any]
    sample_output: dict[str, Any] | None
    error: str | None
    duration_ms: float | None
    finished_at: datetime | None
    created_at: datetime


class IntegrationCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=2, max_length=120)
    summary: str = Field(default="", max_length=280)
    description: str = Field(default="", max_length=4000)
    kind: IntegrationKindLiteral = "custom"
    protocol: IntegrationProtocolLiteral = "http"
    base_url: str = Field(min_length=8, max_length=500)
    docs_url: str | None = Field(default=None, max_length=500)
    manifest: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    is_public: bool = True

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return _check_slug(value)

    @field_validator("capabilities")
    @classmethod
    def _capabilities(cls, value: list[str]) -> list[str]:
        for item in value:
            if not CAPABILITY_KEY_RE.match(item):
                raise ValueError(f"некорректный ключ возможности: {item}")
        return value


class IntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    summary: str | None = Field(default=None, max_length=280)
    description: str | None = Field(default=None, max_length=4000)
    kind: IntegrationKindLiteral | None = None
    protocol: IntegrationProtocolLiteral | None = None
    base_url: str | None = Field(default=None, max_length=500)
    docs_url: str | None = Field(default=None, max_length=500)
    manifest: dict[str, Any] | None = None
    capabilities: list[str] | None = Field(default=None, max_length=50)
    is_public: bool | None = None

    @field_validator("capabilities")
    @classmethod
    def _capabilities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for item in value:
            if not CAPABILITY_KEY_RE.match(item):
                raise ValueError(f"некорректный ключ возможности: {item}")
        return value


class IntegrationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    slug: str
    name: str
    summary: str
    description: str
    kind: IntegrationKind
    protocol: IntegrationProtocol
    base_url: str
    docs_url: str | None
    manifest: dict[str, Any]
    capabilities: list[str]
    is_public: bool
    is_verified: bool
    last_checked_at: datetime | None
    last_status: str | None
    last_latency_ms: float | None
    last_error: str | None
    check_count: int
    failure_count: int
    created_at: datetime


class TemplateFile(BaseModel):
    path: str
    language: str
    content: str


class TemplateRead(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    summary: str
    description: str
    runtime: TemplateRuntime
    language: str
    files: list[TemplateFile]
    manifest: dict[str, Any]
    requires_capabilities: list[str]
    downloads: int
    created_at: datetime


class PortalAgentItem(BaseModel):
    agent_id: uuid.UUID
    name: str
    slug: str
    status: str
    current_version: int
    last_check: dict[str, Any] | None
    last_negative_check: dict[str, Any] | None = None


class PortalOverview(BaseModel):
    agents: list[PortalAgentItem]
    agents_total: int
    agents_ready: int
    templates: int
    integrations: int


__all__ = [
    "CompatibilityCheckItem",
    "CompatibilityCheckRead",
    "CompatibilityCheckRequest",
    "IntegrationCreate",
    "IntegrationKindLiteral",
    "IntegrationProtocolLiteral",
    "IntegrationRead",
    "IntegrationUpdate",
    "PortalAgentItem",
    "PortalOverview",
    "TemplateFile",
    "TemplateRead",
]
