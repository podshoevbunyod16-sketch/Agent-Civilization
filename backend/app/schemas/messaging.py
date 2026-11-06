from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.messaging import ConversationStatus, DeliveryStrategy, MessageKind, MessageStatus


class ConversationCreate(BaseModel):
    subject: str = Field(default="", max_length=200)
    participant_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    task_id: uuid.UUID | None = None

    @field_validator("participant_ids")
    @classmethod
    def _unique(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen


class MessageCreate(BaseModel):
    """Отправка A2A-сообщения агенту."""

    recipient_agent_id: uuid.UUID
    kind: MessageKind = MessageKind.request
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=120)
    conversation_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    strategy: DeliveryStrategy | None = Field(
        default=None, description="По умолчанию выбирается по типу агента-получателя"
    )
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    wait_for_response: bool = Field(
        default=False, description="Дождаться синхронного ответа агента (request/response)"
    )


class MessageReply(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    kind: MessageKind = MessageKind.response


class AgentMessagePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    sender_agent_id: uuid.UUID | None
    recipient_agent_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    kind: MessageKind
    strategy: DeliveryStrategy
    status: MessageStatus
    correlation_id: str | None
    payload: dict[str, Any]
    response_payload: dict[str, Any]
    error: dict[str, Any] | None
    error_message: str | None
    attempts: int
    max_attempts: int
    latency_ms: int | None
    delivered_at: datetime | None
    created_at: datetime


class ConversationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject: str
    status: ConversationStatus
    participant_ids: list[str]
    task_id: uuid.UUID | None
    message_count: int
    last_message_at: datetime | None
    created_at: datetime


class ConversationDetail(ConversationPublic):
    messages: list[AgentMessagePublic] = Field(default_factory=list)


class McpServerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    server_url: str = Field(min_length=8, max_length=500)
    transport: Literal["http", "stdio"] = "http"
    auth_token: str | None = Field(default=None, max_length=500)

    @field_validator("server_url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://", "stdio:")):
            raise ValueError("server_url должен начинаться с http://, https:// или stdio:")
        # SSRF: адрес проверяется по резолву DNS, а не по строковым префиксам
        # (этап 10, A #7). `stdio:` — транспорт узла, сетевого адреса нет.
        if not value.startswith("stdio:"):
            from app.core.config import settings
            from app.core.net_guard import BlockedAddressError, validate_url

            try:
                validate_url(
                    value,
                    allow_private=settings.ssrf_allow_private,
                    allow_unresolved=True,
                )
            except BlockedAddressError as exc:
                raise ValueError(f"Адрес недоступен для внешнего вызова: {exc}") from exc
        return value


class McpToolPublic(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class McpServerPublic(BaseModel):
    """Публичное представление: auth_token никогда не возвращается."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    server_url: str
    transport: str
    protocol_version: str
    tools: list[dict[str, Any]]
    tools_synced_at: datetime | None
    status: str
    created_at: datetime
    has_auth_token: bool = False


class McpToolCall(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class CapabilityMatch(BaseModel):
    """Результат обнаружения (discovery) исполнителя по возможностям."""

    agent_id: uuid.UUID
    slug: str
    name: str
    status: str
    reputation_score: float
    completed_tasks: int
    matched_capabilities: list[str]
    missing_capabilities: list[str]
    score: float
    entrypoint_type: str


class DiscoveryResponse(BaseModel):
    required_capabilities: list[str]
    matches: list[CapabilityMatch]


class MessageSendResult(BaseModel):
    message: AgentMessagePublic
    is_duplicate: bool = False
    conversation_id: uuid.UUID


class WaitResponse(BaseModel):
    message: AgentMessagePublic
    response: AgentMessagePublic | None = None


class HealthSummary(BaseModel):
    pending: int
    processing: int
    delivered: int
    failed: int
    expired: int


class QueueDrainResult(BaseModel):
    processed: int
    delivered: int
    failed: int
    retried: int
