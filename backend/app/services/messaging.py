"""A2A-коммуникация агентов (этап 3).

Что обеспечивает модуль:

* **Диалоги** между агентами с привязкой к заданию (`conversations`).
* **Доставка сообщений** с политикой at-least-once: сообщение попадает в
  очередь (`pending`), воркер/шлюз пытается доставить его получателю, при
  неудаче планируется повтор с экспоненциальной задержкой, после исчерпания
  попыток — `failed`.
* **Идемпотентность**: повторная отправка с тем же `idempotency_key` от того же
  отправителя не создаёт дубль (уникальный индекс на уровне БД).
* **Стратегии доставки** выбираются по типу точки входа получателя:
  `http` (внешний агент участника), `sandbox` (python-агент в песочнице),
  `mcp` (инструмент MCP-сервера), `inbox` (получатель забирает сам).
* **Обнаружение агентов** (`discover_agents`): подбор исполнителя по требуемым
  возможностям с оценкой покрытия и репутации.
* **Защита от циклов**: цепочка сообщений (`correlation_id` + `parent_id`)
  ограничена по глубине, самовызов запрещён по умолчанию.

Сетевые вызовы (`http`) выполняются только из воркера/шлюза и только с
явного разрешения узла: backend никогда не исполняет недоверенный код.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.events import event_bus
from app.db.models.agent import Agent, AgentStatus, EntrypointType
from app.db.models.messaging import (
    AgentMessage,
    Conversation,
    ConversationStatus,
    DeliveryStrategy,
    McpServer,
    MessageKind,
    MessageStatus,
)
from app.db.models.task import Task
from app.schemas.messaging import (
    CapabilityMatch,
    ConversationCreate,
    DiscoveryResponse,
    McpServerCreate,
    MessageCreate,
)
from app.services import agents as agent_service

logger = logging.getLogger(__name__)

MAX_CHAIN_DEPTH = 12
MAX_RETRY_DELAY_SECONDS = 300


def _now() -> datetime:
    return datetime.now(UTC)


# ── Обнаружение агентов ────────────────────────────────────────────────
async def discover_agents(
    session: AsyncSession,
    *,
    capabilities: list[str],
    limit: int = 5,
    exclude_agent_id: uuid.UUID | None = None,
    min_reputation: float = 0.0,
) -> DiscoveryResponse:
    """Находит опубликованных агентов, покрывающих требуемые возможности.

    Оценка = покрытие возможностей (70%) + репутация (20%) + опыт (10%),
    поэтому агент с полным покрытием и хорошей историей выигрывает у
    «пустого» новичка, даже если тот заявил больше уровней.
    """
    required = [c.strip().lower() for c in capabilities if c.strip()]
    stmt = (
        select(Agent)
        .where(
            Agent.status == AgentStatus.published,
            Agent.reputation_score >= min_reputation,
        )
        .order_by(Agent.reputation_score.desc())
        .limit(200)
    )
    if exclude_agent_id:
        stmt = stmt.where(Agent.id != exclude_agent_id)
    candidates = (await session.execute(stmt)).scalars().unique().all()

    matches: list[CapabilityMatch] = []
    for agent in candidates:
        declared = {c.capability_key: c for c in agent.capabilities}
        matched = [key for key in required if key in declared]
        missing = [key for key in required if key not in declared]
        if not matched:
            continue
        coverage = len(matched) / len(required) if required else 1.0
        experience = min(1.0, agent.completed_tasks / 50)
        reputation = float(agent.reputation_score) / 100
        score = round(0.7 * coverage + 0.2 * reputation + 0.1 * experience, 4)
        version = next(
            (v for v in agent.versions if v.version == agent.current_version), None
        )
        matches.append(
            CapabilityMatch(
                agent_id=agent.id,
                slug=agent.slug,
                name=agent.name,
                status=agent.status.value,
                reputation_score=float(agent.reputation_score),
                completed_tasks=agent.completed_tasks,
                matched_capabilities=matched,
                missing_capabilities=missing,
                score=score,
                entrypoint_type=(
                    version.entrypoint_type.value if version else EntrypointType.http.value
                ),
            )
        )

    matches.sort(key=lambda item: (item.score, item.reputation_score), reverse=True)
    return DiscoveryResponse(required_capabilities=required, matches=matches[:limit])


# ── Диалоги ────────────────────────────────────────────────────────────
async def get_conversation(session: AsyncSession, conversation_id: uuid.UUID) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError("Диалог не найден", code="conversation_not_found")
    return conversation


def ensure_participant(conversation: Conversation, agent_id: uuid.UUID) -> None:
    if str(agent_id) not in conversation.participant_ids:
        raise PermissionDeniedError(
            "Агент не является участником диалога", code="not_a_participant"
        )


async def create_conversation(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    data: ConversationCreate,
) -> Conversation:
    if data.task_id:
        task = (
            await session.execute(select(Task).where(Task.id == data.task_id))
        ).scalar_one_or_none()
        if task is None:
            raise NotFoundError("Задание не найдено", code="task_not_found")
        if task.requester_id != actor_id:
            raise PermissionDeniedError("Задание принадлежит другому пользователю")

    for agent_id in data.participant_ids:
        agent = await agent_service.get_agent(session, agent_id)
        if agent.status is not AgentStatus.published:
            raise ValidationError(
                f"Агент {agent.slug} не опубликован", code="agent_not_published"
            )

    conversation = Conversation(
        subject=data.subject,
        participant_ids=[str(agent_id) for agent_id in data.participant_ids],
        task_id=data.task_id,
        created_by_id=actor_id,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def list_conversations(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    status: ConversationStatus | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Conversation], int]:
    """Пользователь видит диалоги своих агентов и диалоги по своим заданиям."""
    owned_agent_ids = [
        str(item)
        for item in (
            await session.execute(select(Agent.id).where(Agent.owner_id == actor_id))
        ).scalars()
    ]
    owned_task_ids = list(
        (
            await session.execute(select(Task.id).where(Task.requester_id == actor_id))
        ).scalars()
    )

    if not owned_agent_ids and not owned_task_ids:
        return [], 0

    visibility = []
    if owned_agent_ids:
        visibility.append(
            func.jsonb_exists_any(Conversation.participant_ids, owned_agent_ids)
        )
    if owned_task_ids:
        visibility.append(Conversation.task_id.in_(owned_task_ids))

    filters = [visibility[0]] if len(visibility) == 1 else [visibility[0] | visibility[1]]
    if agent_id:
        filters.append(func.jsonb_exists(Conversation.participant_ids, str(agent_id)))
    if status:
        filters.append(Conversation.status == status)

    stmt = select(Conversation).where(*filters)
    count_stmt = select(func.count()).select_from(Conversation).where(*filters)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows), total


async def conversation_messages(
    session: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 200
) -> list[AgentMessage]:
    rows = (
        await session.execute(
            select(AgentMessage)
            .where(AgentMessage.conversation_id == conversation_id)
            .order_by(AgentMessage.created_at)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def close_conversation(
    session: AsyncSession, conversation: Conversation
) -> Conversation:
    conversation.status = ConversationStatus.closed
    await session.flush()
    return conversation


# ── Отправка сообщений ─────────────────────────────────────────────────
def _strategy_for(agent: Agent, version_entrypoint: EntrypointType | None) -> DeliveryStrategy:
    if version_entrypoint is EntrypointType.python:
        return DeliveryStrategy.sandbox
    if version_entrypoint is EntrypointType.mcp:
        return DeliveryStrategy.mcp
    if version_entrypoint is EntrypointType.http:
        return DeliveryStrategy.http
    if version_entrypoint is EntrypointType.docker:
        return DeliveryStrategy.inbox
    return DeliveryStrategy.inbox


async def _resolve_chain_depth(session: AsyncSession, parent_id: uuid.UUID | None) -> int:
    depth = 0
    current = parent_id
    seen: set[uuid.UUID] = set()
    while current and depth < MAX_CHAIN_DEPTH + 1:
        if current in seen:  # повреждённая цепочка
            return MAX_CHAIN_DEPTH + 1
        seen.add(current)
        parent = (
            await session.execute(select(AgentMessage).where(AgentMessage.id == current))
        ).scalar_one_or_none()
        if parent is None:
            break
        depth += 1
        current = parent.parent_id
    return depth


def _auto_subject(sender: Agent, recipient: Agent, payload: dict[str, Any]) -> str:
    """Тема диалога, если отправитель её не задал.

    Пустая тема делает список диалогов бесполезным, поэтому по умолчанию
    используется понятная пара «отправитель → получатель».
    """
    explicit = payload.get("subject") if isinstance(payload, dict) else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:200]
    return f"{sender.name} → {recipient.name}"[:200]


async def send_message(
    session: AsyncSession,
    *,
    sender_agent_id: uuid.UUID,
    data: MessageCreate,
) -> tuple[AgentMessage, Conversation, bool]:
    """Ставит сообщение в очередь доставки.

    Возвращает (message, conversation, is_duplicate).
    """
    sender = await agent_service.get_agent(session, sender_agent_id)
    if sender.status is not AgentStatus.published:
        raise ValidationError("Отправлять сообщения может только опубликованный агент",
                              code="agent_not_published")

    recipient = await agent_service.get_agent(session, data.recipient_agent_id)
    if recipient.status is not AgentStatus.published:
        raise ValidationError("Агент-получатель не опубликован", code="recipient_not_available")
    if recipient.id == sender.id and data.kind is not MessageKind.event:
        raise ValidationError("Агент не может отправлять request самому себе",
                              code="self_message_forbidden")

    if data.parent_id:
        parent = (
            await session.execute(
                select(AgentMessage).where(AgentMessage.id == data.parent_id)
            )
        ).scalar_one_or_none()
        if parent is None:
            raise NotFoundError("Родительское сообщение не найдено", code="message_not_found")
        if parent.recipient_agent_id != sender.id:
            raise PermissionDeniedError(
                "Ответ возможен только на сообщение, адресованное вам", code="not_message_recipient"
            )
        depth = await _resolve_chain_depth(session, parent.id)
        if depth >= MAX_CHAIN_DEPTH:
            raise ValidationError(
                f"Превышена максимальная глубина цепочки ({MAX_CHAIN_DEPTH})",
                code="chain_depth_exceeded",
            )

    # Идемпотентность: тот же ключ от того же отправителя → возвращаем существующее
    if data.idempotency_key:
        existing = (
            await session.execute(
                select(AgentMessage).where(
                    AgentMessage.sender_agent_id == sender.id,
                    AgentMessage.idempotency_key == data.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            conversation = await get_conversation(session, existing.conversation_id)
            return existing, conversation, True

    version = next(
        (v for v in recipient.versions if v.version == recipient.current_version), None
    )
    entrypoint = version.entrypoint_type if version else None
    strategy = data.strategy or _strategy_for(recipient, entrypoint)
    if strategy is DeliveryStrategy.mcp and version is not None:
        mcp = (
            await session.execute(
                select(McpServer).where(McpServer.agent_id == recipient.id)
            )
        ).scalar_one_or_none()
        if mcp is None:
            raise ValidationError(
                "У агента-получателя нет зарегистрированного MCP-сервера",
                code="mcp_server_missing",
            )

    conversation: Conversation | None = None
    if data.conversation_id:
        conversation = await get_conversation(session, data.conversation_id)
        if conversation.status is not ConversationStatus.open:
            raise ValidationError("Диалог закрыт", code="conversation_closed")
    else:
        conversation = Conversation(
            subject=_auto_subject(sender, recipient, data.payload),
            participant_ids=[str(sender.id), str(recipient.id)],
            status=ConversationStatus.open,
        )
        session.add(conversation)
        await session.flush()

    for participant in (str(sender.id), str(recipient.id)):
        if participant not in conversation.participant_ids:
            conversation.participant_ids = [*conversation.participant_ids, participant]

    message = AgentMessage(
        conversation_id=conversation.id,
        sender_agent_id=sender.id,
        recipient_agent_id=recipient.id,
        parent_id=data.parent_id,
        kind=data.kind,
        strategy=strategy,
        status=MessageStatus.pending,
        correlation_id=data.correlation_id or uuid.uuid4().hex,
        idempotency_key=data.idempotency_key,
        payload=data.payload,
        max_attempts=data.max_attempts,
        next_attempt_at=_now(),
        expires_at=_now() + timedelta(seconds=data.timeout_seconds),
    )
    session.add(message)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "Сообщение с таким idempotency_key уже отправлено",
            code="duplicate_message",
        ) from exc

    conversation.message_count += 1
    conversation.last_message_at = message.created_at or _now()
    await session.flush()
    await _publish_message_event(message, "message.created")
    return message, conversation, False


async def _publish_message_event(message: AgentMessage, event_type: str) -> None:
    """Уведомляет подписчиков WebSocket о новом сообщении/статусе.

    Публикация не должна ломать доставку, поэтому все ошибки шины
    проглатываются: статус в БД — источник истины.
    """
    try:
        await event_bus.publish(
            f"conversation:{message.conversation_id}",
            {
                "event_type": event_type,
                "message_id": str(message.id),
                "kind": message.kind.value,
                "status": message.status.value,
                "sender_agent_id": str(message.sender_agent_id)
                if message.sender_agent_id
                else None,
                "recipient_agent_id": str(message.recipient_agent_id)
                if message.recipient_agent_id
                else None,
                "payload": message.payload,
                "attempts": message.attempts,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            },
        )
    except Exception:  # pragma: no cover - шина событий не критична
        logger.warning("message_event_publish_failed", exc_info=True)


async def get_message(session: AsyncSession, message_id: uuid.UUID) -> AgentMessage:
    message = (
        await session.execute(select(AgentMessage).where(AgentMessage.id == message_id))
    ).scalar_one_or_none()
    if message is None:
        raise NotFoundError("Сообщение не найдено", code="message_not_found")
    return message


async def inbox(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    status: MessageStatus | None = None,
    limit: int = 50,
) -> list[AgentMessage]:
    """Входящие сообщения агента (для внешних агентов и отладки)."""
    stmt = select(AgentMessage).where(AgentMessage.recipient_agent_id == agent_id)
    if status:
        stmt = stmt.where(AgentMessage.status == status)
    rows = (
        await session.execute(stmt.order_by(AgentMessage.created_at.desc()).limit(limit))
    ).scalars().all()
    return list(rows)


# ── Очередь доставки (забирает воркер) ─────────────────────────────────
# Пока сообщение «в работе», воркер арендует его на это время.
# Если воркер умер, аренда истекает и сообщение уходит повторно —
# это и даёт гарантию at-least-once без вечных блокировок.
CLAIM_LEASE_SECONDS = 120


async def claim_deliveries(
    session: AsyncSession, *, limit: int = 10
) -> list[AgentMessage]:
    """Забирает пачку сообщений к доставке.

    `FOR UPDATE SKIP LOCKED` позволяет безопасно запускать несколько воркеров:
    каждый берёт своё сообщение и не блокирует остальные. Сообщение в статусе
    `processing` отдаётся повторно только когда истекла аренда.
    """
    now = _now()
    lease_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
    stmt = (
        select(AgentMessage)
        .where(
            AgentMessage.status.in_([MessageStatus.pending, MessageStatus.processing]),
            (AgentMessage.next_attempt_at.is_(None)) | (AgentMessage.next_attempt_at <= now),
            (AgentMessage.locked_until.is_(None)) | (AgentMessage.locked_until <= now),
        )
        .order_by(AgentMessage.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    messages = list((await session.execute(stmt)).scalars().all())
    claimed: list[AgentMessage] = []
    for message in messages:
        if message.expires_at and message.expires_at < now:
            message.status = MessageStatus.expired
            message.error = {"type": "Timeout", "message": "Сообщение истекло до доставки"}
            message.locked_until = None
            continue
        message.status = MessageStatus.processing
        message.attempts += 1
        message.locked_until = lease_until
        claimed.append(message)
    await session.flush()
    return claimed


async def mark_delivered(
    session: AsyncSession,
    message: AgentMessage,
    *,
    response_payload: dict[str, Any] | None = None,
    latency_ms: int | None = None,
) -> AgentMessage:
    message.status = MessageStatus.delivered
    message.delivered_at = _now()
    message.response_payload = response_payload or {}
    message.latency_ms = latency_ms
    message.error = None
    message.error_message = None
    message.locked_until = None
    await session.flush()
    await _publish_message_event(message, "message.delivered")
    return message


async def mark_failed(
    session: AsyncSession,
    message: AgentMessage,
    *,
    error: dict[str, Any],
) -> AgentMessage:
    """Планирует повтор с экспоненциальной задержкой или помечает failed."""
    message.error = error
    message.error_message = str(error.get("message", ""))[:2000]
    if message.attempts >= message.max_attempts:
        message.status = MessageStatus.failed
        message.next_attempt_at = None
    else:
        message.status = MessageStatus.pending
        delay = min(2 ** message.attempts, MAX_RETRY_DELAY_SECONDS)
        message.next_attempt_at = _now() + timedelta(seconds=delay)
    message.locked_until = None
    await session.flush()
    await _publish_message_event(message, "message.failed")
    return message


async def expire_messages(session: AsyncSession) -> int:
    now = _now()
    rows = list(
        (
            await session.execute(
                select(AgentMessage).where(
                    AgentMessage.status.in_(
                        [MessageStatus.pending, MessageStatus.processing]
                    ),
                    AgentMessage.expires_at.is_not(None),
                    AgentMessage.expires_at < now,
                )
            )
        ).scalars().all()
    )
    for message in rows:
        message.status = MessageStatus.expired
        message.error = {"type": "Timeout", "message": "Истёк срок доставки"}
    await session.flush()
    return len(rows)


async def message_stats(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(AgentMessage.status, func.count()).group_by(AgentMessage.status)
        )
    ).all()
    stats = {status.value: 0 for status in MessageStatus}
    for status, count in rows:
        key = status.value if hasattr(status, "value") else str(status)
        stats[key] = int(count)
    return stats


# ── MCP ────────────────────────────────────────────────────────────────
async def register_mcp_server(
    session: AsyncSession, agent: Agent, data: McpServerCreate
) -> McpServer:
    existing = (
        await session.execute(select(McpServer).where(McpServer.agent_id == agent.id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.name = data.name
        existing.server_url = data.server_url
        existing.transport = data.transport
        if data.auth_token is not None:
            existing.auth_token = data.auth_token
        existing.status = "active"
        await session.flush()
        return existing

    server = McpServer(
        agent_id=agent.id,
        name=data.name,
        server_url=data.server_url,
        transport=data.transport,
        auth_token=data.auth_token,
    )
    session.add(server)
    await session.flush()
    return server


async def get_mcp_server(session: AsyncSession, agent_id: uuid.UUID) -> McpServer | None:
    return (
        await session.execute(select(McpServer).where(McpServer.agent_id == agent_id))
    ).scalar_one_or_none()


def public_mcp(server: McpServer) -> dict[str, Any]:
    return {
        "id": server.id,
        "agent_id": server.agent_id,
        "name": server.name,
        "server_url": server.server_url,
        "transport": server.transport,
        "protocol_version": server.protocol_version,
        "tools": server.tools or [],
        "tools_synced_at": server.tools_synced_at,
        "status": server.status,
        "created_at": server.created_at,
        "has_auth_token": bool(server.auth_token),
    }


def mcp_endpoint(server: McpServer) -> str:
    """URL JSON-RPC эндпоинта MCP.

    Используется ровно тот адрес, который указал владелец агента:
    MCP-серверы не обязаны следовать единому суффиксу пути.
    """
    return server.server_url
