"""Внутренний API доставки A2A-сообщений (для message worker).

Доступ защищён сервисным токеном (`X-Service-Token` == `INTERNAL_SERVICE_TOKEN`).
Если токен не настроен, API отключён (403) — fail-closed.

Почему отдельный API, а не прямой доступ воркера к БД:

* воркеру не нужны учётные данные базы;
* все переходы статусов проходят через один код с аудитом;
* воркер можно масштабировать независимо от API и от узлов исполнения.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body
from sqlalchemy import select

from app.api.deps import DbSession, ServiceAuth
from app.core.errors import NotFoundError, ValidationError
from app.db.models.agent import Agent, AgentVersion
from app.db.models.messaging import AgentMessage, MessageStatus
from app.schemas.common import Message
from app.services import messaging as messaging_service

router = APIRouter(prefix="/internal/messages", tags=["internal"])


async def _assignment_payload(session: DbSession, message: AgentMessage) -> dict[str, Any]:
    """Всё, что нужно воркеру для доставки: точка входа, лимиты, MCP-сервер."""
    if message.recipient_agent_id is None:
        raise NotFoundError("У сообщения нет получателя", code="recipient_missing")

    recipient = (
        await session.execute(select(Agent).where(Agent.id == message.recipient_agent_id))
    ).scalars().unique().one_or_none()
    if recipient is None:
        raise NotFoundError("Агент-получатель не найден", code="agent_not_found")

    version = (
        await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == recipient.id,
                AgentVersion.version == recipient.current_version,
            )
        )
    ).scalar_one_or_none()

    mcp_data: dict[str, Any] | None = None
    if message.strategy.value == "mcp":
        server = await messaging_service.get_mcp_server(session, recipient.id)
        if server is not None:
            mcp_data = {
                "server_url": server.server_url,
                # Секрет расшифровывается только здесь — на пути к воркеру,
                # доставляющему вызов. Наружу через публичный API он не уходит.
                "auth_token": messaging_service.decrypt_mcp_token(server.auth_token),
                "protocol_version": server.protocol_version,
            }

    return {
        "agent_id": str(recipient.id),
        "slug": recipient.slug,
        "entrypoint_type": version.entrypoint_type.value if version else "inbox",
        "entrypoint_config": (version.entrypoint_config if version else {}) or {},
        "resource_limits": (version.resource_limits if version else {}) or {},
        "mcp": mcp_data,
    }


@router.post(
    "/claim",
    summary="Забрать сообщения к доставке",
    dependencies=[],
)
async def claim_messages(
    session: DbSession,
    _: ServiceAuth,
    limit: int = Body(default=10, ge=1, le=100, embed=True),
) -> dict[str, Any]:
    messages = await messaging_service.claim_deliveries(session, limit=limit)
    payload = []
    for message in messages:
        payload.append(
            {
                "id": str(message.id),
                "conversation_id": str(message.conversation_id),
                "sender_agent_id": str(message.sender_agent_id)
                if message.sender_agent_id
                else None,
                "recipient_agent_id": str(message.recipient_agent_id)
                if message.recipient_agent_id
                else None,
                "kind": message.kind.value,
                "strategy": message.strategy.value,
                "payload": message.payload,
                "correlation_id": message.correlation_id,
                "attempt": message.attempts,
                "max_attempts": message.max_attempts,
                "expires_at": message.expires_at.isoformat() if message.expires_at else None,
                "assignment": await _assignment_payload(session, message),
            }
        )
    await session.commit()
    return {"messages": payload}


@router.post(
    "/{message_id}/result",
    summary="Сообщить результат доставки",
)
async def report_delivery(
    message_id: uuid.UUID,
    session: DbSession,
    _: ServiceAuth,
    success: bool = Body(...),
    response_payload: dict[str, Any] = Body(default_factory=dict),
    error: dict[str, Any] | None = Body(default=None),
    latency_ms: int | None = Body(default=None),
) -> dict[str, Any]:
    message = await messaging_service.get_message(session, message_id)
    if message.status.is_terminal:
        raise ValidationError("Сообщение уже завершено", code="message_terminal")

    if success:
        await messaging_service.mark_delivered(
            session, message, response_payload=response_payload, latency_ms=latency_ms
        )
        retry_scheduled = False
        status = message.status.value
    else:
        unknown = error or {"type": "DeliveryError", "message": "неизвестная ошибка"}
        await messaging_service.mark_failed(session, message, error=unknown)
        retry_scheduled = message.status is MessageStatus.pending
        status = message.status.value
    await session.commit()
    return {
        "message_id": str(message.id),
        "status": status,
        "attempts": message.attempts,
        "retry_scheduled": retry_scheduled,
    }


@router.post("/expire", summary="Пометить истёкшие сообщения", response_model=Message)
async def expire(session: DbSession, _: ServiceAuth) -> Message:
    count = await messaging_service.expire_messages(session)
    await session.commit()
    return Message(detail=f"expired={count}")
