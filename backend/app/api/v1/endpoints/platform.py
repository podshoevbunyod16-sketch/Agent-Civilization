from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession, Paging
from app.schemas.common import Page
from app.schemas.node import AuditEntryPublic
from app.services import audit as audit_service
from app.services import gateway
from app.services import nodes as node_service

router = APIRouter(tags=["platform"])


@router.get("/audit", response_model=Page[AuditEntryPublic], summary="Журнал аудита")
async def audit_log(
    session: DbSession,
    user: CurrentUser,
    paging: Paging,
    actor_id: uuid.UUID | None = None,
    action: Annotated[str | None, Query(max_length=80)] = None,
    target_type: Annotated[str | None, Query(max_length=40)] = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    all_entries: bool = False,
) -> Page[AuditEntryPublic]:
    # Не-админ видит только свои записи
    if not (user.is_admin and all_entries):
        actor_id = user.id
    entries, total = await audit_service.list_entries(
        session,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        offset=paging.offset,
        limit=paging.limit,
    )
    return Page.build(
        [AuditEntryPublic.model_validate(e) for e in entries],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.get("/platform/stats", summary="Сводная статистика цивилизации")
async def platform_stats(session: DbSession) -> dict:
    from sqlalchemy import func, select

    from app.db.models.agent import Agent, AgentStatus
    from app.db.models.user import User

    agent_rows = (
        await session.execute(select(Agent.status, func.count()).group_by(Agent.status))
    ).all()
    agents_by_status = {
        (s.value if hasattr(s, "value") else str(s)): int(c) for s, c in agent_rows
    }
    total_users = int((await session.execute(select(func.count()).select_from(User))).scalar_one())
    task_stats = await gateway.task_stats(session)
    node_stats = await node_service.node_stats(session)
    return {
        "users": total_users,
        "agents": {
            "total": sum(agents_by_status.values()),
            "by_status": agents_by_status,
            "published": agents_by_status.get(AgentStatus.published.value, 0),
        },
        "tasks": task_stats,
        "nodes": node_stats,
    }
