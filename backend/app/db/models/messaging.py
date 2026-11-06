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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel


class ConversationStatus(enum.StrEnum):
    open = "open"
    closed = "closed"
    archived = "archived"


class MessageKind(enum.StrEnum):
    """Типы A2A-сообщений."""

    request = "request"      # запрос от агента к агенту
    response = "response"    # ответ на запрос
    event = "event"          # уведомление без ожидания ответа
    handoff = "handoff"      # передача задачи другому агенту
    error = "error"          # сообщение об ошибке маршрутизации/доставки


class DeliveryStrategy(enum.StrEnum):
    """Как сообщение попадает к получателю."""

    http = "http"          # внешний агент участника по HTTP
    sandbox = "sandbox"    # python-агент исполняется в песочнице
    mcp = "mcp"            # вызов инструмента MCP-сервера
    inbox = "inbox"        # только хранение (получатель заберёт сам)


class MessageStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    delivered = "delivered"
    failed = "failed"
    expired = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {MessageStatus.delivered, MessageStatus.failed, MessageStatus.expired}


class Conversation(BaseModel):
    """Диалог между агентами. Может быть привязан к заданию."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_last_message", "last_message_at"),)

    subject: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus, name="conversation_status", native_enum=False, length=20),
        default=ConversationStatus.open,
        nullable=False,
        index=True,
    )
    # Список id агентов-участников (строки UUID) — удобно для фильтрации по JSONB
    participant_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="conversation",
        lazy="raise",
        order_by="AgentMessage.created_at",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AgentMessage(BaseModel):
    """Сообщение A2A. Доставка — at-least-once с дедупликацией по idempotency_key."""

    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint(
            "sender_agent_id", "idempotency_key", name="uq_message_sender_idempotency"
        ),
        CheckConstraint("attempts >= 0", name="message_attempts_non_negative"),
        Index("ix_messages_delivery_queue", "status", "next_attempt_at"),
        Index("ix_messages_recipient_status", "recipient_agent_id", "status"),
        Index("ix_messages_correlation", "correlation_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    sender_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    recipient_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_messages.id", ondelete="SET NULL")
    )

    kind: Mapped[MessageKind] = mapped_column(
        Enum(MessageKind, name="message_kind", native_enum=False, length=20),
        default=MessageKind.request,
        nullable=False,
    )
    strategy: Mapped[DeliveryStrategy] = mapped_column(
        Enum(DeliveryStrategy, name="delivery_strategy", native_enum=False, length=20),
        default=DeliveryStrategy.inbox,
        nullable=False,
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status", native_enum=False, length=20),
        default=MessageStatus.pending,
        nullable=False,
    )

    correlation_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Аренда доставки: пока аренда жива, сообщение принадлежит одному воркеру.
    # Истёкшая аренда = «воркер умер» → сообщение доставляется повторно.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    conversation: Mapped[Conversation] = relationship(back_populates="messages", lazy="raise")


class McpServer(BaseModel):
    """Зарегистрированный MCP-сервер инструментов для агента."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_mcp_server_agent"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    server_url: Mapped[str] = mapped_column(String(500), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), default="http", nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(20), default="2024-11-05", nullable=False)
    # Токен доступа к MCP-серверу хранится отдельно и никогда не отдаётся наружу
    # Шифротекст AES-256-GCM (см. app/core/secrets.py), а не открытый токен:
    # версия ключа входит в строку, поэтому колонка с запасом на ротацию.
    auth_token: Mapped[str | None] = mapped_column(String(2000))
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    tools_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
