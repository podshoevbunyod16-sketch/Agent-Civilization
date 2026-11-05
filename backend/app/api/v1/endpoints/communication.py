"""Публичный API коммуникации агентов (этап 3).

* диалоги и история сообщений;
* отправка A2A-сообщений с идемпотентностью;
* входящие сообщения агента (inbox) — для внешних агентов и отладки;
* обнаружение исполнителя по возможностям;
* MCP-серверы: регистрация, синхронизация инструментов, вызов инструмента.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    Ctx,
    CurrentUser,
    DbSession,
    OptionalUser,
    Paging,
)
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models.agent import Agent
from app.db.models.messaging import ConversationStatus, MessageStatus
from app.schemas.common import Page
from app.schemas.messaging import (
    AgentMessagePublic,
    ConversationCreate,
    ConversationDetail,
    ConversationPublic,
    DiscoveryResponse,
    McpServerCreate,
    McpServerPublic,
    McpToolCall,
    MessageCreate,
    MessageReply,
    MessageSendResult,
)
from app.services import agents as agent_service
from app.services import audit as audit_service
from app.services import messaging as messaging_service
from app.services import ratelimit

router = APIRouter(tags=["communication"])


# ── Обнаружение агентов ────────────────────────────────────────────────
@router.get(
    "/discovery/agents",
    response_model=DiscoveryResponse,
    summary="Найти исполнителя по возможностям",
    dependencies=[],
)
async def discover(
    session: DbSession,
    capabilities: Annotated[list[str], Query(min_length=1)],
    limit: int = Query(5, ge=1, le=50),
    exclude_agent_id: uuid.UUID | None = None,
    min_reputation: float = Query(0.0, ge=0, le=100),
) -> DiscoveryResponse:
    return await messaging_service.discover_agents(
        session,
        capabilities=capabilities,
        limit=limit,
        exclude_agent_id=exclude_agent_id,
        min_reputation=min_reputation,
    )


# ── Диалоги ────────────────────────────────────────────────────────────
@router.post(
    "/conversations",
    response_model=ConversationPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Создать диалог между агентами",
)
async def create_conversation(
    data: ConversationCreate, session: DbSession, user: CurrentUser, ctx: Ctx
) -> ConversationPublic:
    conversation = await messaging_service.create_conversation(
        session, actor_id=user.id, data=data
    )
    await audit_service.record(
        session,
        action="conversation.create",
        target_type="conversation",
        target_id=conversation.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"participants": conversation.participant_ids, "task_id": str(data.task_id or "")},
    )
    await session.commit()
    return ConversationPublic.model_validate(conversation)


@router.get("/conversations", response_model=Page[ConversationPublic], summary="Мои диалоги")
async def list_conversations(
    session: DbSession,
    user: CurrentUser,
    paging: Paging,
    agent_id: uuid.UUID | None = None,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(open|closed|archived)$")
    ] = None,
) -> Page[ConversationPublic]:
    items, total = await messaging_service.list_conversations(
        session,
        actor_id=user.id,
        agent_id=agent_id,
        status=ConversationStatus(status_filter) if status_filter else None,
        offset=paging.offset,
        limit=paging.limit,
    )
    return Page.build(
        [ConversationPublic.model_validate(item) for item in items],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def _load_conversation_for_user(session, user, conversation_id: uuid.UUID):
    conversation = await messaging_service.get_conversation(session, conversation_id)

    owned = list(
        (
            await session.execute(
                __import__("sqlalchemy").select(Agent.id).where(Agent.owner_id == user.id)
            )
        ).scalars()
    )
    owned_ids = {str(item) for item in owned}
    is_participant_owner = bool(owned_ids & set(conversation.participant_ids))
    if not (user.is_admin or is_participant_owner or conversation.created_by_id == user.id):
        raise PermissionDeniedError("Нет доступа к этому диалогу", code="conversation_forbidden")
    return conversation


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="Диалог с историей сообщений",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(200, le=500),
) -> ConversationDetail:
    conversation = await _load_conversation_for_user(session, user, conversation_id)
    messages = await messaging_service.conversation_messages(
        session, conversation.id, limit=limit
    )
    return ConversationDetail(
        **ConversationPublic.model_validate(conversation).model_dump(),
        messages=[AgentMessagePublic.model_validate(item) for item in messages],
    )


@router.post(
    "/conversations/{conversation_id}/close",
    response_model=ConversationPublic,
    summary="Закрыть диалог",
)
async def close_conversation(
    conversation_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> ConversationPublic:
    conversation = await _load_conversation_for_user(session, user, conversation_id)
    await messaging_service.close_conversation(session, conversation)
    await audit_service.record(
        session,
        action="conversation.close",
        target_type="conversation",
        target_id=conversation.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return ConversationPublic.model_validate(conversation)


# ── A2A-сообщения ──────────────────────────────────────────────────────
@router.post(
    "/agents/{agent_id}/messages",
    response_model=MessageSendResult,
    status_code=status.HTTP_201_CREATED,
    summary="Отправить A2A-сообщение от имени агента",
    dependencies=[],
)
async def send_message(
    agent_id: uuid.UUID,
    data: MessageCreate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> MessageSendResult:
    sender = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(sender, user)
    await ratelimit.check_rate_limit("messages", str(user.id), limit=120)

    message, conversation, is_duplicate = await messaging_service.send_message(
        session, sender_agent_id=sender.id, data=data
    )
    await audit_service.record(
        session,
        action="message.send",
        target_type="message",
        target_id=message.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={
            "sender": str(sender.id),
            "recipient": str(data.recipient_agent_id),
            "kind": message.kind.value,
            "strategy": message.strategy.value,
            "duplicate": is_duplicate,
        },
    )
    await session.commit()
    return MessageSendResult(
        message=AgentMessagePublic.model_validate(message),
        is_duplicate=is_duplicate,
        conversation_id=conversation.id,
    )


@router.post(
    "/agents/{agent_id}/messages/{message_id}/reply",
    response_model=MessageSendResult,
    status_code=status.HTTP_201_CREATED,
    summary="Ответить на входящее сообщение",
)
async def reply_to_message(
    agent_id: uuid.UUID,
    message_id: uuid.UUID,
    data: MessageReply,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> MessageSendResult:
    responder = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(responder, user)

    incoming = await messaging_service.get_message(session, message_id)
    if incoming.recipient_agent_id != responder.id:
        raise PermissionDeniedError(
            "Ответить можно только на сообщение, адресованное вашему агенту",
            code="not_message_recipient",
        )
    if incoming.sender_agent_id is None:
        raise NotFoundError("У сообщения нет отправителя", code="sender_missing")

    message, conversation, is_duplicate = await messaging_service.send_message(
        session,
        sender_agent_id=responder.id,
        data=MessageCreate(
            recipient_agent_id=incoming.sender_agent_id,
            kind=data.kind,
            payload=data.payload,
            parent_id=incoming.id,
            conversation_id=incoming.conversation_id,
            correlation_id=incoming.correlation_id,
        ),
    )
    await audit_service.record(
        session,
        action="message.reply",
        target_type="message",
        target_id=message.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"in_reply_to": str(incoming.id), "duplicate": is_duplicate},
    )
    await session.commit()
    return MessageSendResult(
        message=AgentMessagePublic.model_validate(message),
        is_duplicate=is_duplicate,
        conversation_id=conversation.id,
    )


@router.get(
    "/agents/{agent_id}/inbox",
    response_model=list[AgentMessagePublic],
    summary="Входящие сообщения агента",
)
async def agent_inbox(
    agent_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(pending|processing|delivered|failed|expired)$")
    ] = None,
    limit: int = Query(50, le=200),
) -> list[AgentMessagePublic]:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    messages = await messaging_service.inbox(
        session,
        agent_id=agent.id,
        status=MessageStatus(status_filter) if status_filter else None,
        limit=limit,
    )
    return [AgentMessagePublic.model_validate(item) for item in messages]


@router.get("/messages/stats", summary="Статистика доставки сообщений")
async def stats(session: DbSession) -> dict[str, int]:
    return await messaging_service.message_stats(session)


@router.get(
    "/messages/{message_id}",
    response_model=AgentMessagePublic,
    summary="Детали сообщения",
)
async def get_message(
    message_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> AgentMessagePublic:
    message = await messaging_service.get_message(session, message_id)
    for agent_id in (message.sender_agent_id, message.recipient_agent_id):
        if agent_id is None:
            continue
        try:
            agent = await agent_service.get_agent(session, agent_id)
        except NotFoundError:
            continue
        if agent.owner_id == user.id or user.is_admin:
            return AgentMessagePublic.model_validate(message)
    raise PermissionDeniedError("Нет доступа к этому сообщению", code="message_forbidden")


# ── MCP ────────────────────────────────────────────────────────────────
@router.put(
    "/agents/{agent_id}/mcp",
    response_model=McpServerPublic,
    summary="Зарегистрировать MCP-сервер для агента",
)
async def register_mcp(
    agent_id: uuid.UUID,
    data: McpServerCreate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> McpServerPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    server = await messaging_service.register_mcp_server(session, agent, data)
    await audit_service.record(
        session,
        action="mcp.register",
        target_type="mcp_server",
        target_id=server.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"agent_id": str(agent.id), "server_url": server.server_url},
    )
    await session.commit()
    return McpServerPublic.model_validate(messaging_service.public_mcp(server))


@router.get(
    "/agents/{agent_id}/mcp",
    response_model=McpServerPublic,
    summary="MCP-сервер агента",
)
async def get_mcp(
    agent_id: uuid.UUID, session: DbSession, viewer: OptionalUser
) -> McpServerPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_view(agent, viewer)
    server = await messaging_service.get_mcp_server(session, agent.id)
    if server is None:
        raise NotFoundError("MCP-сервер не зарегистрирован", code="mcp_server_missing")
    is_owner = viewer is not None and (viewer.is_admin or viewer.id == agent.owner_id)
    data = messaging_service.public_mcp(server)
    if not is_owner:
        # Посторонним не показываем список инструментов и факт наличия токена
        data = {**data, "tools": [], "has_auth_token": False}
    return McpServerPublic.model_validate(data)


@router.post(
    "/agents/{agent_id}/mcp/sync",
    response_model=McpServerPublic,
    summary="Синхронизировать список инструментов MCP",
)
async def sync_mcp(
    agent_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> McpServerPublic:
    """Синхронизация выполняется через message worker (сетевой доступ).

    Здесь только фиксируется запрос: backend не делает исходящих вызовов к
    недоверенным серверам. Инструменты обновляются воркером через
    `/internal/messages` (MCP tools/list).
    """
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    server = await messaging_service.get_mcp_server(session, agent.id)
    if server is None:
        raise NotFoundError("MCP-сервер не зарегистрирован", code="mcp_server_missing")

    from datetime import UTC, datetime

    server.tools_synced_at = datetime.now(UTC)
    await session.flush()
    await audit_service.record(
        session,
        action="mcp.sync_requested",
        target_type="mcp_server",
        target_id=server.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return McpServerPublic.model_validate(messaging_service.public_mcp(server))


@router.post(
    "/agents/{agent_id}/mcp/call",
    response_model=MessageSendResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Вызвать инструмент MCP через своего агента",
)
async def call_mcp_tool(
    agent_id: uuid.UUID,
    data: McpToolCall,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> MessageSendResult:
    """Вызов ставится в очередь доставки и выполняется воркером.

    Это позволяет backend оставаться изолированным от недоверенных
    MCP-серверов: сетевой вызов делает отдельный процесс с таймаутами.
    """
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    server = await messaging_service.get_mcp_server(session, agent.id)
    if server is None:
        raise NotFoundError("MCP-сервер не зарегистрирован", code="mcp_server_missing")

    from app.db.models.messaging import DeliveryStrategy, MessageKind
    from app.schemas.messaging import MessageCreate

    message, conversation, is_duplicate = await messaging_service.send_message(
        session,
        sender_agent_id=agent.id,
        data=MessageCreate(
            # Вызов инструмента — это событие для себя: диалог не нужен,
            # а ограничение «нельзя слать request самому себе» сохраняется.
            recipient_agent_id=agent.id,
            kind=MessageKind.event,
            strategy=DeliveryStrategy.mcp,
            payload={"tool": data.tool_name, "arguments": data.arguments},
        ),
    )
    await audit_service.record(
        session,
        action="mcp.tool_call",
        target_type="message",
        target_id=message.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"tool": data.tool_name, "agent_id": str(agent.id)},
    )
    await session.commit()
    return MessageSendResult(
        message=AgentMessagePublic.model_validate(message),
        is_duplicate=is_duplicate,
        conversation_id=conversation.id,
    )


__all__ = ["router"]
