"""Realtime-каналы (WebSocket): события заданий и сообщения диалогов.

Аутентификация (этап 10, A #3): браузерный WebSocket API не умеет
произвольные заголовки, поэтому раньше клиент передавал access-токен в
query-строке (`?token=`). Query-строка попадает в журналы прокси и
access-логи, а сам токен живёт часами. Теперь:

1. клиент получает **одноразовый тикет** обычным HTTPS-запросом
   `POST /ws/ticket` (заголовок `Authorization` работает);
2. тикет действует 30 секунд, привязан к пользователю и сессии,
   гасится при первом использовании (Redis `SETNX`);
3. `?token=` поддерживается только для access-токенов, выданных до
   этапа 10 (без `sid`), — такие токены доживают свой срок, после
   перевыпуска клиент обязан перейти на тикеты. Для новых токенов
   query-строка отклоняется.

Дополнительно: лимит одновременных соединений на пользователя и
проверка членства/владения для каждого канала.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.api.deps import Ctx, CurrentUser, DbSession, rate_limit_critical
from app.core import redis_client
from app.core.config import settings
from app.core.events import event_bus
from app.core.security import TokenError, create_token, decode_token
from app.db.models.user import User
from app.schemas.common import Message
from app.services import audit as audit_service
from app.services import auth as auth_service
from app.services import gateway

router = APIRouter(tags=["realtime"])

WS_TICKET_TTL_SECONDS = 30
WS_MAX_CONNECTIONS_PER_USER = 10
_connections: dict[str, int] = {}


def _acquire_slot(user_id: str) -> bool:
    current = _connections.get(user_id, 0)
    if current >= WS_MAX_CONNECTIONS_PER_USER:
        return False
    _connections[user_id] = current + 1
    return True


def _release_slot(user_id: str) -> None:
    current = _connections.get(user_id, 0)
    if current <= 1:
        _connections.pop(user_id, None)
    else:
        _connections[user_id] = current - 1


@router.post(
    "/ws/ticket",
    response_model=dict,
    dependencies=[Depends(rate_limit_critical)],
    summary="Одноразовый тикет для WebSocket",
)
async def issue_ws_ticket(
    session: DbSession, user: CurrentUser, ctx: Ctx
) -> dict[str, object]:
    """Тикет живёт 30 секунд и гасится при подключении.

    В тикет кладутся только `sub` и `sid`: он не даёт доступа к HTTP API —
    проверка типа токена (`type == "ws"`) отклонит его в `get_current_user`.
    """
    ticket, _, _ = create_token(
        str(user.id),
        "ws",
        ttl_seconds=WS_TICKET_TTL_SECONDS,
        extra_claims={"sid": getattr(ctx, "session_id", None) or ctx.request_id},
    )
    await audit_service.record(
        session,
        action="ws.ticket_issued",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return {
        "ticket": ticket,
        "expires_in": WS_TICKET_TTL_SECONDS,
        "usage": "Подключение: wss://<host>/api/v1/ws/tasks/<id>?ticket=<ticket>",
    }


async def _authenticate_socket(
    websocket: WebSocket, *, token: str | None, ticket: str | None
) -> tuple[User, str] | None:
    """Возвращает (user, session_id) либо закрывает соединение с кодом."""
    if not token and not ticket:
        await websocket.close(code=4401, reason="ticket required")
        return None
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            if ticket:
                payload = decode_token(ticket, expected_type="ws")
                # Одноразовость: тикет гасится через Redis. Если Redis
                # недоступен, разрешаем (тикет уже короткоживущий и
                # привязан к пользователю) — соединение не должно падать
                # из-за инфраструктуры.
                used = await redis_client.consume_once(
                    f"ws:{payload['jti']}", WS_TICKET_TTL_SECONDS * 2
                )
                if used is False:
                    await websocket.close(code=4401, reason="ticket already used")
                    return None
                user = await auth_service.get_user_by_id(
                    session, uuid.UUID(payload["sub"])
                )
                session_id = payload.get("sid") or ""
            else:
                payload = decode_token(token or "", expected_type="access")
                if payload.get("sid"):
                    # Новые токены в query-строке не принимаем: для них есть тикет.
                    await websocket.close(code=4401, reason="use POST /ws/ticket")
                    return None
                user = await auth_service.get_user_by_id(
                    session, uuid.UUID(payload["sub"])
                )
                session_id = ""
        except (TokenError, ValueError, KeyError):
            await websocket.close(code=4401, reason="invalid ticket")
            return None
        if user is None or not user.is_active:
            await websocket.close(code=4401, reason="invalid user")
            return None
    return user, session_id


@router.websocket("/ws/tasks/{task_id}")
async def task_events_socket(
    websocket: WebSocket,
    task_id: uuid.UUID,
    ticket: Annotated[str | None, Query()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Поток событий задания (тикет из `POST /ws/ticket`)."""
    await websocket.accept()
    identity = await _authenticate_socket(websocket, token=token, ticket=ticket)
    if identity is None:
        return
    user, _session_id = identity
    if not _acquire_slot(str(user.id)):
        await websocket.close(code=4429, reason="too many connections")
        return

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            task = await gateway.get_task(session, task_id)
        except Exception:
            await websocket.close(code=4404, reason="task not found")
            return
        if task.requester_id != user.id and not user.is_admin:
            await websocket.close(code=4403, reason="forbidden")
            return
        history = await gateway.task_events(session, task_id, limit=50)

    await websocket.send_json(
        {
            "type": "snapshot",
            "task_id": str(task_id),
            "events": [
                {
                    "sequence": e.sequence,
                    "event_type": e.event_type,
                    "actor": e.actor,
                    "message": e.message,
                    "data": e.data,
                    "created_at": e.created_at.isoformat(),
                }
                for e in history
            ],
        }
    )

    topic = f"task:{task_id}"
    queue = await event_bus.subscribe(topic)
    try:
        while True:
            event = await queue.get()
            await websocket.send_json({"type": "event", "task_id": str(task_id), **event})
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover
        pass
    finally:
        await event_bus.unsubscribe(topic, queue)
        _release_slot(str(user.id))


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_socket(
    websocket: WebSocket,
    conversation_id: uuid.UUID,
    ticket: Annotated[str | None, Query()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Поток сообщений диалога в реальном времени.

    Доступ проверяется по владению агентом-участником диалога. События
    публикуются в шину при отправке и при изменении статуса доставки,
    поэтому поток работает и при нескольких репликах API (Redis Pub/Sub).
    """
    from sqlalchemy import select

    from app.db.models.agent import Agent
    from app.services import messaging as messaging_service

    await websocket.accept()
    identity = await _authenticate_socket(websocket, token=token, ticket=ticket)
    if identity is None:
        return
    user, _session_id = identity
    if not _acquire_slot(str(user.id)):
        await websocket.close(code=4429, reason="too many connections")
        return

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            conversation = await messaging_service.get_conversation(session, conversation_id)
        except Exception:
            await websocket.close(code=4404, reason="conversation not found")
            return
        owned = {
            str(item)
            for item in (
                await session.execute(select(Agent.id).where(Agent.owner_id == user.id))
            ).scalars()
        }
        if not (
            user.is_admin
            or conversation.created_by_id == user.id
            or owned & set(conversation.participant_ids)
        ):
            await websocket.close(code=4403, reason="forbidden")
            return
        history = await messaging_service.conversation_messages(
            session, conversation_id, limit=50
        )

    await websocket.send_json(
        {
            "type": "snapshot",
            "conversation_id": str(conversation_id),
            "messages": [
                {
                    "id": str(item.id),
                    "kind": item.kind.value,
                    "status": item.status.value,
                    "sender_agent_id": str(item.sender_agent_id) if item.sender_agent_id else None,
                    "recipient_agent_id": str(item.recipient_agent_id)
                    if item.recipient_agent_id
                    else None,
                    "payload": item.payload,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in history
            ],
        }
    )

    topic = f"conversation:{conversation_id}"
    queue = await event_bus.subscribe(topic)
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(
                {"type": "event", "conversation_id": str(conversation_id), **event}
            )
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover
        pass
    finally:
        await event_bus.unsubscribe(topic, queue)
        _release_slot(str(user.id))


@router.get("/ws/info", summary="Состояние шины событий")
async def ws_info() -> dict:
    return {
        "subscribers": event_bus.subscriber_count,
        "connections": sum(_connections.values()),
        "max_connections_per_user": WS_MAX_CONNECTIONS_PER_USER,
        "ticket_ttl_seconds": WS_TICKET_TTL_SECONDS,
    }


__all__ = ["router", "DbSession", "User", "Message", "settings"]
