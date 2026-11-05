from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.deps import DbSession
from app.core.events import event_bus
from app.core.security import TokenError, decode_token
from app.db.models.user import User
from app.services import auth as auth_service
from app.services import gateway

router = APIRouter(tags=["realtime"])


@router.websocket("/ws/tasks/{task_id}")
async def task_events_socket(
    websocket: WebSocket,
    task_id: uuid.UUID,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Поток событий задания.

    Аутентификация: access-токен в query (`?token=...`), так как
    браузерный WebSocket API не поддерживает произвольные заголовки.
    """
    await websocket.accept()
    if not token:
        await websocket.close(code=4401, reason="token required")
        return

    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            payload = decode_token(token, expected_type="access")
            user = await auth_service.get_user_by_id(session, uuid.UUID(payload["sub"]))
        except (TokenError, ValueError, KeyError):
            await websocket.close(code=4401, reason="invalid token")
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


@router.get("/ws/info", summary="Состояние шины событий")
async def ws_info() -> dict:
    return {"subscribers": event_bus.subscriber_count}


__all__ = ["router", "DbSession", "User"]
