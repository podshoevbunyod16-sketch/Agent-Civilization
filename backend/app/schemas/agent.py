from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.agent import AgentStatus, EntrypointType
from app.schemas.auth import UserSummary
from app.schemas.common import CAPABILITY_KEY_RE, SLUG_RE


# ── Capabilities ───────────────────────────────────────────────────────
class CapabilityCreate(BaseModel):
    key: str = Field(min_length=2, max_length=80)
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    category: str = Field(default="general", max_length=60)

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        value = value.strip().lower()
        if not CAPABILITY_KEY_RE.match(value):
            raise ValueError("Ключ возможности: латиница в нижнем регистре, цифры, '.', '_', '-'")
        return value


class CapabilityPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    description: str
    category: str
    is_active: bool


class AgentCapabilityIn(BaseModel):
    capability_key: str = Field(min_length=2, max_length=80)
    declared_level: int = Field(default=1, ge=0, le=5)
    note: str | None = Field(default=None, max_length=280)

    @field_validator("capability_key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        return value.strip().lower()


class AgentCapabilityPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    capability_key: str
    declared_level: int
    verified_level: int | None = None
    verified_at: datetime | None = None
    note: str | None = None


# ── Versions ───────────────────────────────────────────────────────────
class ResourceLimits(BaseModel):
    max_cpu_seconds: int = Field(default=60, ge=1, le=3600)
    max_wall_seconds: int = Field(default=120, ge=1, le=7200)
    max_memory_mb: int = Field(default=512, ge=64, le=32768)
    allow_network: bool = False
    max_output_bytes: int = Field(default=65536, ge=1024, le=10_000_000)


class AgentVersionCreate(BaseModel):
    changelog: str = Field(default="", max_length=5000)
    entrypoint_type: EntrypointType = EntrypointType.http
    entrypoint_config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    publish: bool = False

    @model_validator(mode="after")
    def _validate_entrypoint(self) -> AgentVersionCreate:
        cfg = self.entrypoint_config
        if self.entrypoint_type is EntrypointType.http:
            url = str(cfg.get("url", ""))
            if not url.startswith(("http://", "https://")):
                raise ValueError("entrypoint_config.url должен начинаться с http:// или https://")
            # SSRF: endpoint агента вызывается backend'ом, значит он не должен
            # вести во внутреннюю сеть (метаданные облака, API, БД).
            from app.core.config import settings
            from app.core.net_guard import BlockedAddressError, validate_url

            try:
                validate_url(
                    url, allow_private=settings.ssrf_allow_private, allow_unresolved=True
                )
            except BlockedAddressError as exc:
                raise ValueError(f"entrypoint_config.url недоступен: {exc}") from exc
        elif self.entrypoint_type is EntrypointType.python:
            if not str(cfg.get("code", "")).strip():
                raise ValueError("entrypoint_config.code обязателен для python-агента")
            if len(str(cfg.get("code"))) > 200_000:
                raise ValueError("entrypoint_config.code слишком большой")
        elif self.entrypoint_type is EntrypointType.docker:
            image = str(cfg.get("image", ""))
            if not image or " " in image:
                raise ValueError("entrypoint_config.image обязателен для docker-агента")
        elif self.entrypoint_type is EntrypointType.mcp:
            if not str(cfg.get("server_url", "")).startswith(("http://", "https://", "stdio:")):
                raise ValueError("entrypoint_config.server_url обязателен для mcp-агента")
        for key in self.required_capabilities:
            if not CAPABILITY_KEY_RE.match(key):
                raise ValueError(f"Некорректный ключ возможности: {key}")
        return self


class AgentVersionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    changelog: str
    entrypoint_type: EntrypointType
    entrypoint_config: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_capabilities: list[str]
    resource_limits: dict[str, Any]
    published_at: datetime | None
    deprecated_at: datetime | None
    created_at: datetime


class AgentVersionPublicSafe(AgentVersionPublic):
    """Публичное представление: скрывает внутренние данные entrypoint (код/секреты)."""

    @classmethod
    def from_model(cls, version: Any) -> AgentVersionPublicSafe:
        config = dict(version.entrypoint_config or {})
        sanitized: dict[str, Any] = {}
        if "url" in config:
            sanitized["url"] = config["url"]
        if "image" in config:
            sanitized["image"] = config["image"]
        if "server_url" in config:
            sanitized["server_url"] = config["server_url"]
        if "runtime" in config:
            sanitized["runtime"] = config["runtime"]
        return cls.model_validate(
            {
                "id": version.id,
                "version": version.version,
                "changelog": version.changelog,
                "entrypoint_type": version.entrypoint_type,
                "entrypoint_config": sanitized,
                "input_schema": version.input_schema,
                "output_schema": version.output_schema,
                "required_capabilities": version.required_capabilities,
                "resource_limits": version.resource_limits,
                "published_at": version.published_at,
                "deprecated_at": version.deprecated_at,
                "created_at": version.created_at,
            }
        )


# ── Agents ─────────────────────────────────────────────────────────────
class AgentCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=10, max_length=280)
    description: str = Field(default="", max_length=20000)
    category: str = Field(default="general", max_length=60)
    tags: list[str] = Field(default_factory=list, max_length=20)
    homepage_url: str | None = Field(default=None, max_length=500)
    repository_url: str | None = Field(default=None, max_length=500)
    accent_color: str = Field(default="#6ee7b7", max_length=9)
    capabilities: list[AgentCapabilityIn] = Field(default_factory=list, max_length=32)
    metadata_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not SLUG_RE.match(value):
            raise ValueError("slug: 3-64 символа, латиница/цифры/дефис, без дефиса на краях")
        return value

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        cleaned = []
        for tag in value:
            tag = tag.strip().lower()
            if not tag:
                continue
            if len(tag) > 32:
                raise ValueError("Тег не длиннее 32 символов")
            if tag not in cleaned:
                cleaned.append(tag)
        return cleaned

    @field_validator("accent_color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        import re

        if not re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", value):
            raise ValueError("accent_color должен быть HEX-цветом")
        return value

    @field_validator("capabilities")
    @classmethod
    def _unique_caps(cls, value: list[AgentCapabilityIn]) -> list[AgentCapabilityIn]:
        seen = set()
        for item in value:
            if item.capability_key in seen:
                raise ValueError(f"Дублирующаяся возможность: {item.capability_key}")
            seen.add(item.capability_key)
        return value


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    summary: str | None = Field(default=None, min_length=10, max_length=280)
    description: str | None = Field(default=None, max_length=20000)
    category: str | None = Field(default=None, max_length=60)
    tags: list[str] | None = Field(default=None, max_length=20)
    homepage_url: str | None = Field(default=None, max_length=500)
    repository_url: str | None = Field(default=None, max_length=500)
    accent_color: str | None = Field(default=None, max_length=9)
    metadata_json: dict[str, Any] | None = None
    change_note: str = Field(default="", max_length=500)


class AgentCapabilitiesReplace(BaseModel):
    capabilities: list[AgentCapabilityIn] = Field(max_length=32)


class AgentPublishRequest(BaseModel):
    publish: bool = True
    note: str = Field(default="", max_length=500)


class AgentSuspendRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=280)


class AgentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    summary: str
    description: str
    status: AgentStatus
    category: str
    tags: list[str]
    homepage_url: str | None
    repository_url: str | None
    accent_color: str
    current_version: int
    is_verified: bool
    reputation_score: float
    completed_tasks: int
    failed_tasks: int
    owner: UserSummary
    capabilities: list[AgentCapabilityPublic] = Field(default_factory=list)
    versions: list[AgentVersionPublicSafe] = Field(default_factory=list)
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class AgentListItem(BaseModel):
    """Облегчённое представление для каталога (без версий и описаний)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    summary: str
    status: AgentStatus
    category: str
    tags: list[str]
    accent_color: str
    current_version: int
    is_verified: bool
    reputation_score: float
    completed_tasks: int
    failed_tasks: int
    owner: UserSummary
    capabilities: list[AgentCapabilityPublic] = Field(default_factory=list)
    published_at: datetime | None
    created_at: datetime


class AgentSortField(str, __import__("enum").Enum):
    created_at = "created_at"
    published_at = "published_at"
    reputation = "reputation"
    name = "name"
    completed_tasks = "completed_tasks"


class AgentSearchParams(BaseModel):
    q: str | None = Field(default=None, max_length=200)
    status: AgentStatus | Literal["any"] | None = None
    category: str | None = Field(default=None, max_length=60)
    capability: list[str] = Field(default_factory=list)
    owner_id: uuid.UUID | None = None
    min_reputation: float | None = Field(default=None, ge=0, le=100)
    verified_only: bool = False
    sort: AgentSortField = AgentSortField.created_at
    order: Literal["asc", "desc"] = "desc"
