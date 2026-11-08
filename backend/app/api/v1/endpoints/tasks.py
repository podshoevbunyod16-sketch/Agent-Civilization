from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Ctx, CurrentUser, DbSession, Paging, rate_limit_default
from app.core.errors import PermissionDeniedError
from app.db.models.task import TaskStatus
from app.schemas.common import Page
from app.schemas.node import (
    TaskCancelRequest,
    TaskEventPublic,
    TaskPublic,
    TaskSubmitRequest,
)
from app.services import audit as audit_service
from app.services import gateway

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post(
    "",
    response_model=TaskPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Отправить задание агенту",
    dependencies=[Depends(rate_limit_default)],
)
async def submit_task(
    data: TaskSubmitRequest, session: DbSession, user: CurrentUser, ctx: Ctx
) -> TaskPublic:
    task = await gateway.submit_task(session, user.id, data)
    await audit_service.record(
        session,
        action="task.submit",
        target_type="task",
        target_id=task.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"agent_id": str(task.agent_id), "priority": task.priority},
    )
    await session.commit()
    return TaskPublic.model_validate(task)


@router.get("/stats", summary="Статистика очереди задач")
async def stats(session: DbSession, user: CurrentUser) -> dict:
    return await gateway.task_stats(session)


@router.get("", response_model=Page[TaskPublic], summary="Мои задания")
async def list_tasks(
    session: DbSession,
    user: CurrentUser,
    paging: Paging,
    status_filter: Annotated[
        Literal["queued", "leased", "running", "succeeded", "failed", "cancelled", "expired"]
        | None,
        Query(alias="status"),
    ] = None,
    agent_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    all_tasks: bool = False,
) -> Page[TaskPublic]:
    requester_id = None if (all_tasks and user.is_admin) else user.id
    items, total = await gateway.list_tasks(
        session,
        requester_id=requester_id,
        agent_id=agent_id,
        node_id=node_id,
        status=TaskStatus(status_filter) if status_filter else None,
        offset=paging.offset,
        limit=paging.limit,
    )
    return Page.build(
        [TaskPublic.model_validate(t) for t in items],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.get("/{task_id}", response_model=TaskPublic, summary="Детали задания")
async def get_task(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> TaskPublic:
    task = await gateway.get_task(session, task_id)
    if task.requester_id != user.id and not user.is_admin:
        from sqlalchemy import select

        from app.db.models.agent import Agent

        agent = (
            await session.execute(select(Agent).where(Agent.id == task.agent_id))
        ).scalar_one_or_none()
        if agent is None or agent.owner_id != user.id:
            raise PermissionDeniedError("Нет доступа к этому заданию")
    return TaskPublic.model_validate(task)


@router.get(
    "/{task_id}/events",
    response_model=list[TaskEventPublic],
    summary="События жизненного цикла задания",
)
async def get_events(
    task_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> list[TaskEventPublic]:
    task = await gateway.get_task(session, task_id)
    if task.requester_id != user.id and not user.is_admin:
        raise PermissionDeniedError("Нет доступа к этому заданию")
    events = await gateway.task_events(session, task_id)
    return [TaskEventPublic.model_validate(e) for e in events]


@router.post("/{task_id}/cancel", response_model=TaskPublic, summary="Отменить задание")
async def cancel_task(
    task_id: uuid.UUID,
    data: TaskCancelRequest,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> TaskPublic:
    task = await gateway.get_task(session, task_id)
    await gateway.cancel_task(session, task, user.id, data.reason)
    await audit_service.record(
        session,
        action="task.cancel",
        target_type="task",
        target_id=task.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"reason": data.reason},
    )
    await session.commit()
    return TaskPublic.model_validate(task)


@router.post(
    "/{task_id}/run-local",
    summary="Локальное выполнение python-агента в песочнице (dev/тесты)",
)
async def run_local(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> dict:
    """Запускает python-агента текущей версии в изолированной песочнице.

    В production недоверенный код выполняется только на узлах-воркерах;
    этот эндпоинт нужен для smoke-проверки агента разработчиком.
    """
    from app.core.config import settings

    if settings.environment not in {"development", "test"}:
        raise PermissionDeniedError(
            "Локальный запуск доступен только в development/test; "
            "в production задания исполняют узлы",
            code="run_local_disabled",
        )
    task = await gateway.get_task(session, task_id)
    if task.requester_id != user.id and not user.is_admin:
        raise PermissionDeniedError("Нет доступа к этому заданию")
    if settings.sandbox_backend == "noop":
        from app.core.errors import ValidationError

        raise ValidationError("Песочница отключена", code="sandbox_disabled")
    result = await gateway.run_inline_subprocess(session, task)
    return result
