"""Execution Gateway + Task Engine (этапы 2 и 4, базовая часть).

Инварианты:
  * Задание может выполняться только на узле, прошедшем heartbeat.
  * Аренда (lease) выдаётся на ограниченное время; истёкшая аренда
    возвращает задачу в очередь (at-least-once), а результат от старой
    аренды отвергается (fencing по lease_token).
  * Все переходы статусов пишутся в task_events (append-only).
  * Результат хешируется (sha256) — основа для проверки и trust-слоя.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.db.models.agent import AgentStatus, AgentVersion, EntrypointType
from app.db.models.node import InfrastructureNode, NodeStatus
from app.db.models.task import Task, TaskEvent, TaskStatus
from app.schemas.node import (
    TaskAssignment,
    TaskClaimRequest,
    TaskProgressRequest,
    TaskResultRequest,
    TaskSubmitRequest,
)
from app.services import agents as agent_service
from app.services import nodes as node_service


def _now() -> datetime:
    return datetime.now(UTC)


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def _event(
    session: AsyncSession,
    task: Task,
    event_type: str,
    *,
    actor: str = "system",
    message: str = "",
    data: dict[str, Any] | None = None,
) -> TaskEvent:
    sequence = int(
        (
            await session.execute(
                select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
                    TaskEvent.task_id == task.id
                )
            )
        ).scalar_one()
    ) + 1
    event = TaskEvent(
        task_id=task.id,
        sequence=sequence,
        event_type=event_type,
        actor=actor,
        message=message[:2000],
        data=data or {},
    )
    session.add(event)
    await session.flush()
    return event


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> Task:
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise NotFoundError("Задание не найдено", code="task_not_found")
    return task


async def submit_task(
    session: AsyncSession,
    requester_id: uuid.UUID,
    data: TaskSubmitRequest,
) -> Task:
    agent = await agent_service.get_agent(session, data.agent_id)
    if agent.status is not AgentStatus.published:
        raise ValidationError(
            "Задания принимаются только для опубликованных агентов", code="agent_not_published"
        )
    version = next((v for v in agent.versions if v.version == agent.current_version), None)
    if version is None:
        raise ValidationError("У агента нет опубликованной версии", code="agent_without_version")

    limits = version.resource_limits or {}
    # Дедлайн не может превышать лимит времени агента: мягко ограничиваем,
    # чтобы клиент не получал ошибку из-за значений по умолчанию.
    agent_timeout = int(limits.get("max_wall_seconds") or data.timeout_seconds)
    effective_timeout = min(data.timeout_seconds, agent_timeout)
    if data.budget_units is not None and float(data.budget_units) < 0:
        raise ValidationError("budget_units должен быть неотрицательным")

    # Узлы, подходящие по возможностям, должны существовать, иначе задача
    # будет вечно висеть в очереди — сообщаем сразу.
    node = await node_service.select_node_for_task(
        session, required_capabilities=version.required_capabilities or []
    )

    task = Task(
        requester_id=requester_id,
        agent_id=agent.id,
        agent_version_id=version.id,
        node_id=node.id if node else None,
        status=TaskStatus.queued,
        priority=data.priority,
        max_attempts=data.max_attempts,
        input_payload=data.input_payload,
        idempotency_key=data.idempotency_key,
        deadline_at=_now() + timedelta(seconds=effective_timeout),
        budget_units=data.budget_units,
        trace_id=secrets.token_hex(16),
    )
    session.add(task)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "Задание с таким idempotency_key уже существует", code="duplicate_idempotency_key"
        ) from exc

    await _event(
        session,
        task,
        "task.queued",
        actor="user",
        message="Задание поставлено в очередь",
        data={"agent_id": str(agent.id), "node_id": str(node.id) if node else None},
    )
    if node is not None:
        await _event(
            session,
            task,
            "task.assigned",
            actor="system",
            message=f"Предварительно назначен узел {node.name}",
            data={"node_id": str(node.id)},
        )
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    status: TaskStatus | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Task], int]:
    stmt = select(Task)
    count_stmt = select(func.count()).select_from(Task)
    filters = []
    if requester_id:
        filters.append(Task.requester_id == requester_id)
    if agent_id:
        filters.append(Task.agent_id == agent_id)
    if node_id:
        filters.append(Task.node_id == node_id)
    if status:
        filters.append(Task.status == status)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(Task.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return list(rows), total


async def task_events(
    session: AsyncSession, task_id: uuid.UUID, limit: int = 200
) -> list[TaskEvent]:
    rows = (
        await session.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.sequence)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def cancel_task(
    session: AsyncSession, task: Task, actor_id: uuid.UUID, reason: str = ""
) -> Task:
    if task.requester_id != actor_id:
        raise PermissionDeniedError("Отменить задание может только его автор")
    if task.status.is_terminal:
        raise ValidationError("Задание уже завершено", code="task_terminal")
    task.status = TaskStatus.cancelled
    task.finished_at = _now()
    await _event(
        session, task, "task.cancelled", actor="user", message=reason or "Задание отменено"
    )
    await session.flush()
    return task


# ── Очередь для узлов (pull-модель) ────────────────────────────────────
async def claim_tasks(
    session: AsyncSession, node: InfrastructureNode, data: TaskClaimRequest
) -> list[TaskAssignment]:
    """Узел забирает задания. Используется FOR UPDATE SKIP LOCKED —
    безопасно при любом количестве воркеров и реплик."""
    if node.status is NodeStatus.revoked:
        raise PermissionDeniedError("Узел отозван", code="node_revoked")

    free_slots = max(0, node.max_concurrency - node.running_tasks)
    limit = min(data.max_tasks, free_slots)
    if limit == 0:
        return []

    lease_token = secrets.token_hex(24)
    lease_expires = _now() + timedelta(seconds=settings.worker_lease_seconds)

    # Кандидаты: свои (preassigned) + свободные задания
    stmt = (
        select(Task)
        .where(
            Task.status == TaskStatus.queued,
            (Task.node_id == node.id) | (Task.node_id.is_(None)),
        )
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(limit * 4)
        .with_for_update(skip_locked=True)
    )
    candidates = list((await session.execute(stmt)).scalars().all())

    assignments: list[TaskAssignment] = []
    for task in candidates:
        if len(assignments) >= limit:
            break
        if task.deadline_at and task.deadline_at < _now():
            task.status = TaskStatus.expired
            task.finished_at = _now()
            await _event(session, task, "task.expired", message="Дедлайн истёк до назначения")
            continue

        agent = await agent_service.get_agent(session, task.agent_id)
        version = (
            await session.execute(
                select(AgentVersion).where(AgentVersion.id == task.agent_version_id)
            )
        ).scalar_one_or_none() if task.agent_version_id else None
        if agent.status is not AgentStatus.published or version is None:
            task.status = TaskStatus.failed
            task.finished_at = _now()
            task.error = {"type": "AgentUnavailable", "message": "Агент недоступен"}
            await _event(session, task, "task.failed", message="Агент недоступен")
            continue

        try:
            node_service.ensure_node_accepts(node, version.resource_limits or {})
        except ValidationError:
            # Узел слишком мал для задачи — вернём в общую очередь
            if task.node_id == node.id:
                task.node_id = None
                await _event(
                    session,
                    task,
                    "task.requeued",
                    message="Узел не соответствует лимитам задаче",
                )
            continue

        task.status = TaskStatus.leased
        task.node_id = node.id
        task.lease_token = lease_token
        task.lease_expires_at = lease_expires
        task.attempts += 1
        task.started_at = _now()

        assignments.append(
            TaskAssignment(
                task_id=task.id,
                agent_id=task.agent_id,
                agent_version_id=task.agent_version_id,
                entrypoint_type=version.entrypoint_type.value,
                entrypoint_config=agent_service.pick_entrypoint_config(
                    version, for_execution=True
                ),
                input_payload=task.input_payload,
                resource_limits=version.resource_limits or {},
                lease_token=lease_token,
                lease_expires_at=lease_expires,
                deadline_at=task.deadline_at,
                attempt=task.attempts,
                trace_id=task.trace_id,
            )
        )
        await _event(
            session,
            task,
            "task.leased",
            message=f"Задание выдано узлу {node.name}",
            data={"node_id": str(node.id), "attempt": task.attempts},
        )

    await session.flush()
    return assignments


async def _get_leased_task(
    session: AsyncSession, node: InfrastructureNode, task_id: uuid.UUID, lease_token: str
) -> Task:
    task = await get_task(session, task_id)
    if task.node_id != node.id:
        raise PermissionDeniedError("Задание выдано другому узлу", code="task_not_yours")
    if not task.lease_token or not secrets.compare_digest(task.lease_token, lease_token):
        raise ValidationError("Недействительная аренда (fencing)", code="invalid_lease")
    if task.status not in {TaskStatus.leased, TaskStatus.running}:
        raise ValidationError("Задание не в работе", code="task_not_running")
    if task.lease_expires_at and task.lease_expires_at < _now():
        raise ValidationError("Аренда истекла", code="lease_expired")
    return task


async def report_progress(
    session: AsyncSession,
    node: InfrastructureNode,
    task_id: uuid.UUID,
    data: TaskProgressRequest,
) -> Task:
    task = await _get_leased_task(session, node, task_id, data.lease_token)
    if task.status is TaskStatus.leased:
        task.status = TaskStatus.running
    await _event(
        session,
        task,
        "task.progress",
        actor=f"node:{node.node_key}",
        message=data.message,
        data=data.data,
    )
    # Продлеваем аренду на время работы
    task.lease_expires_at = _now() + timedelta(seconds=settings.worker_lease_seconds)
    await session.flush()
    return task


async def complete_task(
    session: AsyncSession,
    node: InfrastructureNode,
    task_id: uuid.UUID,
    data: TaskResultRequest,
) -> tuple[Task, bool]:
    task = await _get_leased_task(session, node, task_id, data.lease_token)
    will_retry = False

    if data.success:
        task.status = TaskStatus.succeeded
        task.output_payload = data.output or {}
        task.result_hash = canonical_hash(data.output or {})
        task.error = None
        task.finished_at = _now()
        task.lease_token = None
        task.lease_expires_at = None
        task.cost_units = data.cost_units
        await _event(
            session,
            task,
            "task.succeeded",
            actor=f"node:{node.node_key}",
            message="Задание выполнено",
            data={"result_hash": task.result_hash, "metrics": data.metrics},
        )
        await _update_agent_counters(session, task.agent_id, success=True, task=task)
    else:
        error = {
            "type": "AgentError",
            "message": (data.error_message or "Агент завершился с ошибкой")[:2000],
            "metrics": data.metrics,
        }
        if task.attempts < task.max_attempts:
            will_retry = True
            task.status = TaskStatus.queued
            task.node_id = None
            task.lease_token = None
            task.lease_expires_at = None
            task.error = error
            await _event(
                session,
                task,
                "task.retry_scheduled",
                actor=f"node:{node.node_key}",
                message=f"Повторная попытка {task.attempts + 1}/{task.max_attempts}",
                data=error,
            )
        else:
            task.status = TaskStatus.failed
            task.error = error
            task.finished_at = _now()
            task.lease_token = None
            task.lease_expires_at = None
            await _event(
                session,
                task,
                "task.failed",
                actor=f"node:{node.node_key}",
                message=error["message"],
                data=error,
            )
            await _update_agent_counters(session, task.agent_id, success=False, task=task)

    node.running_tasks = max(0, node.running_tasks - 1)
    await session.flush()
    return task, will_retry


async def _update_agent_counters(
    session: AsyncSession, agent_id: uuid.UUID, *, success: bool, task: Task | None = None
) -> None:
    """Счётчики + репутационный вклад задания.

    Сама репутация вычисляется в `services/trust.py` из `reputation_events`
    (этап 5): одно задание — один вклад, идемпотентно по task_id. Здесь
    только счётчики и вызов хука.
    """
    agent = await agent_service.get_agent(session, agent_id)
    if success:
        agent.completed_tasks += 1
    else:
        agent.failed_tasks += 1
    await session.flush()

    if task is not None:
        from app.services import trust as trust_service

        await trust_service.on_task_finished(session, task, success=success)


async def expire_leases(session: AsyncSession) -> int:
    """Возврат задач с истёкшей арендой в очередь. Вызывается воркером."""
    expired = list(
        (
            await session.execute(
                select(Task).where(
                    Task.status.in_([TaskStatus.leased, TaskStatus.running]),
                    Task.lease_expires_at.is_not(None),
                    Task.lease_expires_at < _now(),
                )
            )
        ).scalars().all()
    )
    requeued = 0
    for task in expired:
        if task.attempts < task.max_attempts:
            task.status = TaskStatus.queued
            task.node_id = None
            task.lease_token = None
            task.lease_expires_at = None
            task.error = {"type": "LeaseExpired", "message": "Узел не завершил задачу вовремя"}
            await _event(session, task, "task.retry_scheduled", message="Аренда истекла")
            requeued += 1
        else:
            task.status = TaskStatus.expired
            task.finished_at = _now()
            task.lease_token = None
            task.lease_expires_at = None
            await _event(session, task, "task.expired", message="Все попытки исчерпаны")
            await _update_agent_counters(session, task.agent_id, success=False, task=task)
    await session.flush()
    return requeued


async def task_stats(session: AsyncSession) -> dict[str, Any]:
    rows = (
        await session.execute(select(Task.status, func.count()).group_by(Task.status))
    ).all()
    by_status = {
        (status.value if hasattr(status, "value") else str(status)): int(count)
        for status, count in rows
    }
    oldest = (
        await session.execute(
            select(func.min(Task.created_at)).where(Task.status == TaskStatus.queued)
        )
    ).scalar_one_or_none()
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "oldest_queued_at": oldest.isoformat() if oldest else None,
    }


async def run_inline_subprocess(
    session: AsyncSession, task: Task
) -> dict[str, Any]:
    """Локальное выполнение python-агента через песочницу.

    Используется ТОЛЬКО для тестов и локальной разработки, файлом
    ``SANDBOX_BACKEND=subprocess``. В production задания уходят на узлы.
    """
    from app.sandbox import SandboxRunner, SandboxUnavailableError

    version = (
        await session.execute(select(AgentVersion).where(AgentVersion.id == task.agent_version_id))
    ).scalar_one_or_none()
    if version is None:
        raise ValidationError("У агента нет версии", code="version_not_found")
    if version.entrypoint_type is not EntrypointType.python:
        raise ValidationError(
            "Локальное исполнение поддерживается только для python-агентов",
            code="unsupported_entrypoint",
        )
    runner = SandboxRunner.from_limits(version.resource_limits or {})
    code = str((version.entrypoint_config or {}).get("code", ""))
    try:
        result = await runner.run_python(code, task.input_payload)
    except SandboxUnavailableError as exc:  # pragma: no cover
        raise ValidationError(str(exc), code="sandbox_unavailable") from exc
    return result.to_dict()


async def table_row_counts(session: AsyncSession) -> dict[str, int]:
    """Диагностика для /health/ready и админ-панели."""
    tables = ("users", "agents", "agent_versions", "infrastructure_nodes", "tasks", "audit_log")
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(
            (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        )
    return counts
