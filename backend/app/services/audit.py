"""Журнал аудита: все критические операции пишутся в append-only таблицу.

Запись идёт в той же транзакции, что и операция: либо оба изменения
сохраняются, либо ни одного (at-least-once при коммите).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditLog

logger = logging.getLogger(__name__)


async def record(
    session: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: str | uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    actor_type: str = "user",
    outcome: str = "success",
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        actor_id=actor_id,
        actor_type=actor_type,
        outcome=outcome,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
        request_id=request_id,
        data=data or {},
    )
    session.add(entry)
    await session.flush()
    logger.info(
        "audit",
        extra={
            "action": action,
            "target_type": target_type,
            "target_id": entry.target_id,
            "actor_id": str(actor_id) if actor_id else None,
            "outcome": outcome,
        },
    )
    return entry


async def list_entries(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)
    filters = []
    if actor_id:
        filters.append(AuditLog.actor_id == actor_id)
    if action:
        filters.append(AuditLog.action == action)
    if target_type:
        filters.append(AuditLog.target_type == target_type)
    if target_id:
        filters.append(AuditLog.target_id == target_id)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return list(rows), total
