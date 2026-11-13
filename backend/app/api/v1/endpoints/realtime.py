from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.events import event_bus
from app.core.security import TokenError, create_token, decode_token
from app.db.models.user import User
from app.services import auth as auth_service
from app.services import gateway

router = APIRouter(tags=["realtime"])


@router.post("/ws/ticket", summary="Одноразовый тикет для WebSocket (TTL 60 с)")
async def issue_ws_ticket(user: CurrentUser) -> dict:
    """Короткоживущий тикет для браузерного WebSocket.

    Браузер не может слать заголовок Authorization при открытии WS, поэтому
    вместо access-токена в query-параметре (он утёк бы в логи и историю)
    клиент сначала получает здесь одноразовый тикет и передаёт его как
    `?ticket=...`. Access-токен в query не принимается.
    """
    ticket, expires_at, _jti = create_token(
        str(user.id), token_type="ws", ttl_seconds=settings.ws_ticket_ttl_seconds
    )
    return {"ticket": ticket, "expires_in": settings.ws_ticket_ttl_seconds}


@router.websocket("/ws/tasks/{task_id}")
async def task_events_socket(
    websocket: WebSocket,
    task_id: uuid.UUID,
    ticket: Annotated[str | None, Query()] = None,
) -> None:
    """Поток событий задания.

    Аутентификация: одноразовый WS-тикет в query (`?ticket=...`) из
    `POST /api/v1/ws/ticket`. Access-токен в query не принимается.
    """
    await websocket.accept()
    if not ticket:
        await websocket.close(code=4401, reason="ticket required")
        return

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            payload = decode_token(ticket, expected_type="ws")
            user = await auth_service.get_user_by_id(session, uuid.UUID(payload["sub"]))
        except (TokenError, ValueError, KeyError):
            await websocket.close(code=4401, reason="invalid ticket")
            return
        if user is None or not user.is_active:
            await websocket.close(code=4401, reason="invalid user")
            return
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


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_socket(
    websocket: WebSocket,
    conversation_id: uuid.UUID,
    ticket: Annotated[str | None, Query()] = None,
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
    if not ticket:
        await websocket.close(code=4401, reason="ticket required")
        return

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            payload = decode_token(ticket, expected_type="ws")
            user = await auth_service.get_user_by_id(session, uuid.UUID(payload["sub"]))
        except (TokenError, ValueError, KeyError):
            await websocket.close(code=4401, reason="invalid ticket")
            return
        if user is None or not user.is_active:
            await websocket.close(code=4401, reason="invalid user")
            return
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


@router.get("/ws/info", summary="Состояние шины событий")
async def ws_info() -> dict:
    return {"subscribers": event_bus.subscriber_count}


__all__ = ["router", "DbSession", "User"]
