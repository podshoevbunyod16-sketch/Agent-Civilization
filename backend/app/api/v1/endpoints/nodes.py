from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    AdminUser,
    Ctx,
    CurrentUser,
    DbSession,
    Paging,
    SignedNode,
    rate_limit_default,
)
from app.core.config import settings
from app.db.models.node import NodeKind, NodeStatus
from app.schemas.common import Page
from app.schemas.node import (
    HeartbeatRequest,
    HeartbeatResponse,
    NodeCredentials,
    NodeHeartbeatPublic,
    NodePublic,
    NodeRegisterRequest,
    NodeUpdate,
    TaskClaimRequest,
    TaskClaimResponse,
    TaskProgressRequest,
    TaskPublic,
    TaskResultRequest,
    TaskResultResponse,
)
from app.services import audit as audit_service
from app.services import gateway
from app.services import nodes as node_service

# Маршруты /nodes/me/* живут в отдельном роутере и подключаются ПЕРЕД
# параметрическими /nodes/{node_id}, иначе FastAPI пытается разобрать
# строку "me" как UUID и возвращает 422/401 с чужим обработчиком.
router = APIRouter(tags=["infrastructure"])
self_router = APIRouter(tags=["infrastructure"])


# ── Управление узлами (пользователь) ──────────────────────────────────
@router.post(
    "/nodes",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать инфраструктурный узел",
)
async def register_node(
    data: NodeRegisterRequest, session: DbSession, user: CurrentUser, ctx: Ctx
) -> dict:
    await node_service.uniqueness_check_name(session, user.id, data.name)
    node, secret = await node_service.register_node(session, user, data)
    await audit_service.record(
        session,
        action="node.register",
        target_type="node",
        target_id=node.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"node_key": node.node_key, "kind": node.kind.value},
    )
    await session.commit()
    return {
        "node": NodePublic.model_validate(node).model_dump(mode="json"),
        "credentials": NodeCredentials(node_key=node.node_key, node_secret=secret).model_dump(),
    }


@router.get("/nodes", response_model=Page[NodePublic], summary="Список узлов")
async def list_nodes(
    session: DbSession,
    user: CurrentUser,
    paging: Paging,
    mine: bool = True,
    status_filter: Annotated[
        Literal["pending", "online", "degraded", "offline", "revoked"] | None,
        Query(alias="status"),
    ] = None,
    kind: NodeKind | None = None,
    only_available: bool = False,
) -> Page[NodePublic]:
    owner_id = user.id if (mine or not user.is_admin) else None
    items, total = await node_service.list_nodes(
        session,
        owner_id=owner_id,
        status=NodeStatus(status_filter) if status_filter else None,
        kind=kind,
        only_available=only_available,
        offset=paging.offset,
        limit=paging.limit,
    )
    return Page.build(
        [NodePublic.model_validate(n) for n in items],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.get("/nodes/{node_id}", response_model=NodePublic, summary="Детали узла")
async def get_node(node_id: uuid.UUID, session: DbSession, user: CurrentUser) -> NodePublic:
    node = await node_service.get_node(session, node_id)
    node_service.ensure_can_manage(node, user)
    return NodePublic.model_validate(node)


@router.patch("/nodes/{node_id}", response_model=NodePublic, summary="Обновить узел")
async def update_node(
    node_id: uuid.UUID,
    data: NodeUpdate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> NodePublic:
    node = await node_service.get_node(session, node_id)
    node_service.ensure_can_manage(node, user)
    await node_service.update_node(session, node, data)
    await audit_service.record(
        session,
        action="node.update",
        target_type="node",
        target_id=node.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"fields": sorted(data.model_dump(exclude_unset=True).keys())},
    )
    await session.commit()
    return NodePublic.model_validate(node)


@router.post(
    "/nodes/{node_id}/rotate-secret",
    response_model=NodeCredentials,
    summary="Ротация секрета узла",
)
async def rotate_secret(
    node_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> NodeCredentials:
    node = await node_service.get_node(session, node_id)
    node_service.ensure_can_manage(node, user)
    node_key, secret = await node_service.rotate_node_secret(session, node)
    await audit_service.record(
        session,
        action="node.rotate_secret",
        target_type="node",
        target_id=node.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"node_key": node_key},
    )
    await session.commit()
    return NodeCredentials(node_key=node_key, node_secret=secret)


@router.post(
    "/nodes/{node_id}/revoke", response_model=NodePublic, summary="Отозвать узел"
)
async def revoke_node(
    node_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
    reason: str = Query(default="revoked by owner", max_length=280),
) -> NodePublic:
    node = await node_service.get_node(session, node_id)
    node_service.ensure_can_manage(node, user)
    await node_service.revoke_node(session, node, user, reason)
    await audit_service.record(
        session,
        action="node.revoke",
        target_type="node",
        target_id=node.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"reason": reason},
    )
    await session.commit()
    return NodePublic.model_validate(node)


@router.post(
    "/nodes/{node_id}/verify",
    response_model=NodePublic,
    summary="Верифицировать узел (админ)",
)
async def verify_node(
    node_id: uuid.UUID, session: DbSession, admin: AdminUser, ctx: Ctx
) -> NodePublic:
    node = await node_service.get_node(session, node_id)
    await node_service.admin_verify_node(session, node, admin)
    await audit_service.record(
        session,
        action="node.verify",
        target_type="node",
        target_id=node.id,
        actor_id=admin.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return NodePublic.model_validate(node)


@router.get(
    "/nodes/{node_id}/heartbeats",
    response_model=list[NodeHeartbeatPublic],
    summary="История heartbeat узла",
)
async def node_heartbeats(
    node_id: uuid.UUID, session: DbSession, user: CurrentUser, limit: int = Query(50, le=500)
) -> list[NodeHeartbeatPublic]:
    node = await node_service.get_node(session, node_id)
    node_service.ensure_can_manage(node, user)
    rows = await node_service.node_heartbeats(session, node.id, limit=limit)
    return [NodeHeartbeatPublic.model_validate(r) for r in rows]


# ── Эндпоинты для самих узлов (подписанные запросы) ───────────────────


@self_router.get(
    "/nodes/me",
    response_model=NodePublic,
    summary="Профиль узла (подписанный запрос)",
    dependencies=[Depends(rate_limit_default)],
)
async def node_me(node: SignedNode) -> NodePublic:
    return NodePublic.model_validate(node)


@self_router.post(
    "/nodes/me/verify",
    response_model=NodePublic,
    summary="Подтвердить владение секретом (первый запуск)",
)
async def node_verify(session: DbSession, node: SignedNode) -> NodePublic:
    await node_service.verify_node_ownership(session, node)
    await session.commit()
    return NodePublic.model_validate(node)


@self_router.post(
    "/nodes/me/heartbeat",
    response_model=HeartbeatResponse,
    summary="Heartbeat узла",
)
async def node_heartbeat(
    data: HeartbeatRequest, session: DbSession, node: SignedNode
) -> HeartbeatResponse:
    await node_service.record_heartbeat(session, node, data)
    await session.commit()
    return HeartbeatResponse(
        status=node.status,
        server_time=datetime.now(UTC),
        lease_seconds=settings.worker_lease_seconds,
    )


@self_router.post(
    "/nodes/me/tasks/claim",
    response_model=TaskClaimResponse,
    summary="Забрать задания из очереди (Execution Gateway)",
)
async def claim_tasks(
    data: TaskClaimRequest, session: DbSession, node: SignedNode
) -> TaskClaimResponse:
    assignments = await gateway.claim_tasks(session, node, data)
    await session.commit()
    return TaskClaimResponse(assignments=assignments)


@self_router.post(
    "/nodes/me/tasks/{task_id}/progress",
    response_model=TaskPublic,
    summary="Сообщить о прогрессе выполнения",
)
async def task_progress(
    task_id: uuid.UUID,
    data: TaskProgressRequest,
    session: DbSession,
    node: SignedNode,
) -> TaskPublic:
    task = await gateway.report_progress(session, node, task_id, data)
    await session.commit()
    return TaskPublic.model_validate(task)


@self_router.post(
    "/nodes/me/tasks/{task_id}/result",
    response_model=TaskResultResponse,
    summary="Передать результат выполнения",
)
async def task_result(
    task_id: uuid.UUID,
    data: TaskResultRequest,
    session: DbSession,
    node: SignedNode,
    ctx: Ctx,
) -> TaskResultResponse:
    task, will_retry = await gateway.complete_task(session, node, task_id, data)
    await audit_service.record(
        session,
        action="task.result",
        target_type="task",
        target_id=task.id,
        actor_type="node",
        outcome="success" if data.success else "failure",
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={
            "node_id": str(node.id),
            "status": task.status.value,
            "attempts": task.attempts,
            "result_hash": task.result_hash,
        },
    )
    await session.commit()
    return TaskResultResponse(
        task_id=task.id, status=task.status.value, will_retry=will_retry
    )
