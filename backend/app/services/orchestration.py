"""Оркестрация составных заданий: декомпозиция, зависимости, запуск (этап 4).

Ключевые решения:

* план — DAG шагов; исполнитель шага выбирается по возможностям
  (`messaging.discover_agents`, тот же скоринг, что и в A2A-обнаружении);
* шаг исполняется обычным заданием (`tasks`) через существующий runtime,
  поэтому лимиты, песочница, аренда и fencing наследуются без новой логики;
* синхронизация статусов идёт от задачи к шагу: `STEPS` обновляются
  идемпотентно, повторный вызов не ломает состояние;
* декомпозицию может предложить агент-планировщик (A2A): его ответ
  валидируется как DAG, а шаги создаются только после явного принятия.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.db.models.agent import AgentStatus
from app.db.models.messaging import MessageKind
from app.db.models.orchestration import (
    TERMINAL_PLAN_STATUSES,
    TERMINAL_STEP_STATUSES,
    FailurePolicy,
    PlanEvent,
    PlanStatus,
    PlanStep,
    StepStatus,
    TaskPlan,
)
from app.db.models.task import Task, TaskStatus
from app.schemas.messaging import MessageCreate
from app.schemas.node import TaskSubmitRequest
from app.schemas.orchestration import (
    PlanAdoptRequest,
    PlanCreate,
    PlanEventPublic,
    PlannerOutput,
    PlanProgress,
    PlanStepCreate,
)
from app.services import agents as agent_service
from app.services import gateway
from app.services import messaging as messaging_service

logger = logging.getLogger(__name__)

MAX_STEPS_PER_PLAN = 20
TASK_TO_STEP_STATUS = {
    TaskStatus.queued: StepStatus.queued,
    TaskStatus.leased: StepStatus.running,
    TaskStatus.running: StepStatus.running,
    TaskStatus.succeeded: StepStatus.succeeded,
    TaskStatus.failed: StepStatus.failed,
    TaskStatus.cancelled: StepStatus.failed,
    TaskStatus.expired: StepStatus.failed,
}


def _now() -> datetime:
    return datetime.now(UTC)


async def _event(
    session: AsyncSession,
    plan: TaskPlan,
    event_type: str,
    *,
    message: str = "",
    step_key: str | None = None,
    actor: str = "system",
    data: dict[str, Any] | None = None,
) -> PlanEvent:
    """Append-only событие плана с монотонной последовательностью."""
    last = (
        await session.execute(
            select(func.coalesce(func.max(PlanEvent.sequence), 0)).where(
                PlanEvent.plan_id == plan.id
            )
        )
    ).scalar_one()
    event = PlanEvent(
        plan_id=plan.id,
        sequence=int(last) + 1,
        event_type=event_type,
        step_key=step_key,
        actor=actor,
        message=message[:2000],
        data=data or {},
    )
    session.add(event)
    await session.flush()
    return event


# ── Чтение ─────────────────────────────────────────────────────────────
async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> TaskPlan:
    plan = (
        await session.execute(select(TaskPlan).where(TaskPlan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        raise NotFoundError("План не найден", code="plan_not_found")
    return plan


async def plan_steps(session: AsyncSession, plan_id: uuid.UUID) -> list[PlanStep]:
    return list(
        (
            await session.execute(
                select(PlanStep)
                .where(PlanStep.plan_id == plan_id)
                .order_by(PlanStep.position, PlanStep.key)
            )
        ).scalars().all()
    )


async def plan_events(
    session: AsyncSession, plan_id: uuid.UUID, *, limit: int = 100
) -> list[PlanEventPublic]:
    rows = (
        await session.execute(
            select(PlanEvent)
            .where(PlanEvent.plan_id == plan_id)
            .order_by(PlanEvent.sequence.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [PlanEventPublic.model_validate(row) for row in reversed(rows)]


def ensure_plan_access(plan: TaskPlan, actor_id: uuid.UUID, *, is_admin: bool = False) -> None:
    if is_admin or plan.requester_id == actor_id:
        return
    raise PermissionDeniedError("Нет доступа к этому плану", code="plan_forbidden")


async def list_plans(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    status: PlanStatus | None = None,
    offset: int = 0,
    limit: int = 20,
    is_admin: bool = False,
) -> tuple[list[TaskPlan], int]:
    filters = []
    if not is_admin:
        filters.append(TaskPlan.requester_id == requester_id)
    if status is not None:
        filters.append(TaskPlan.status == status)
    stmt = select(TaskPlan).where(*filters)
    count_stmt = select(func.count()).select_from(TaskPlan).where(*filters)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(TaskPlan.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return list(rows), total


# ── Создание плана ─────────────────────────────────────────────────────
def _validate_dag(steps: list[PlanStepCreate]) -> None:
    """Проверка, что граф зависимостей ацикличен (алгоритм Кана)."""
    keys = {step.key for step in steps}
    indegree = {step.key: 0 for step in steps}
    children: dict[str, list[str]] = {step.key: [] for step in steps}
    for step in steps:
        for dep in step.depends_on:
            if dep not in keys:
                raise ValidationError(
                    f"Шаг {step.key} зависит от неизвестного шага {dep}",
                    code="unknown_dependency",
                )
            indegree[step.key] += 1
            children[dep].append(step.key)

    queue = [key for key, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        key = queue.pop()
        visited += 1
        for child in children[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(steps):
        raise ValidationError(
            "Граф шагов содержит цикл", code="cyclic_dependencies"
        )


CAPABILITY_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


async def _validate_capability_keys(keys: list[str]) -> None:
    """Проверяем только формат ключа, а не наличие в таксономии.

    Реестр разрешает объявлять новые возможности (см. `_ensure_capabilities_exist`),
    поэтому требование «возможность уже известна» сделало бы планы
    неработоспособными на чистой базе. Отсутствие исполнителя для валидной
    возможности — это не ошибка запроса, а состояние шага `blocked`.
    """
    invalid = [key for key in keys if not CAPABILITY_KEY_PATTERN.match(key)]
    if invalid:
        raise ValidationError(
            "Некорректный ключ возможности (ожидается формат category.action): "
            + ", ".join(invalid),
            code="invalid_capability_key",
            details={"invalid": invalid},
        )


async def create_plan(
    session: AsyncSession, *, requester_id: uuid.UUID, data: PlanCreate
) -> TaskPlan:
    if data.planner_agent_id is not None:
        return await create_plan_via_planner(
            session, requester_id=requester_id, data=data
        )

    assert data.steps, "PlanCreate гарантирует наличие steps или planner_agent_id"
    _validate_dag(data.steps)
    for step in data.steps:
        await _validate_capability_keys(step.required_capabilities)
    # Явно указанный агент должен существовать и быть опубликованным
    for step in data.steps:
        if step.agent_id is not None:
            agent = await agent_service.get_agent(session, step.agent_id)
            if agent.status is not AgentStatus.published:
                raise ValidationError(
                    f"Агент {step.agent_slug if hasattr(step, 'agent_slug') else ''}"
                    " не опубликован",
                    code="agent_not_published",
                    details={"step": step.key, "agent_id": str(agent.id)},
                )

    plan = TaskPlan(
        requester_id=requester_id,
        title=data.title,
        goal=data.goal,
        status=PlanStatus.draft,
        max_parallelism=data.max_parallelism,
        failure_policy=data.failure_policy,
        step_timeout_seconds=data.step_timeout_seconds,
        trace_id=uuid.uuid4().hex,
    )
    session.add(plan)
    await session.flush()

    for position, step_data in enumerate(data.steps):
        session.add(
            PlanStep(
                plan_id=plan.id,
                key=step_data.key,
                name=step_data.name,
                position=position,
                required_capabilities=step_data.required_capabilities,
                depends_on=step_data.depends_on,
                input_payload=step_data.input_payload,
                agent_id=step_data.agent_id,
                max_attempts=step_data.max_attempts,
                status=StepStatus.pending,
            )
        )
    await session.flush()

    await _event(
        session,
        plan,
        "plan.created",
        actor="user",
        message=f"План «{plan.title}» создан, шагов: {len(data.steps)}",
        data={"steps": len(data.steps)},
    )
    await _refresh_readiness(session, plan)
    return plan


async def create_plan_via_planner(
    session: AsyncSession, *, requester_id: uuid.UUID, data: PlanCreate
) -> TaskPlan:
    """Ставит запрос декомпозиции агенту-планировщику через A2A."""
    planner = await agent_service.get_agent(session, data.planner_agent_id)  # type: ignore[arg-type]
    if planner.status is not AgentStatus.published:
        raise ValidationError("Агент-планировщик не опубликован", code="agent_not_published")

    plan = TaskPlan(
        requester_id=requester_id,
        title=data.title,
        goal=data.goal,
        status=PlanStatus.draft,
        max_parallelism=data.max_parallelism,
        failure_policy=data.failure_policy,
        step_timeout_seconds=data.step_timeout_seconds,
        planner_agent_id=planner.id,
        trace_id=uuid.uuid4().hex,
    )
    session.add(plan)
    await session.flush()

    message, _, is_duplicate = await messaging_service.send_message(
        session,
        sender_agent_id=planner.id,
        data=MessageCreate(
            recipient_agent_id=planner.id,
            kind=MessageKind.event,
            idempotency_key=f"plan-{plan.id}",
            payload={
                "task": "decompose",
                "plan_id": str(plan.id),
                "title": plan.title,
                "goal": plan.goal,
                "contract": (
                "PlannerOutput {steps: [{key, name, required_capabilities, "
                "depends_on, input}]}"
            ),
            },
        ),
    )
    plan.planner_message_id = message.id
    await session.flush()
    await _event(
        session,
        plan,
        "plan.decomposition_requested",
        actor="user",
        message=f"Декомпозиция запрошена у агента {planner.name}",
        data={"planner_agent_id": str(planner.id), "message_id": str(message.id),
              "duplicate": is_duplicate},
    )
    return plan


async def adopt_planner_output(
    session: AsyncSession,
    plan: TaskPlan,
    data: PlanAdoptRequest,
    *,
    actor_id: uuid.UUID,
) -> TaskPlan:
    """Принимает шаги, предложенные агентом-планировщиком."""
    ensure_plan_access(plan, actor_id)
    if plan.status is not PlanStatus.draft:
        raise ConflictError("Шаги можно добавить только в черновик плана", code="plan_not_draft")
    existing = await plan_steps(session, plan.id)
    if existing:
        raise ConflictError("У плана уже есть шаги", code="plan_has_steps")

    _validate_dag(data.steps)
    for step in data.steps:
        await _validate_capability_keys(step.required_capabilities)

    for position, step_data in enumerate(data.steps):
        session.add(
            PlanStep(
                plan_id=plan.id,
                key=step_data.key,
                name=step_data.name,
                position=position,
                required_capabilities=step_data.required_capabilities,
                depends_on=step_data.depends_on,
                input_payload=step_data.input_payload,
                agent_id=step_data.agent_id,
                max_attempts=step_data.max_attempts,
                status=StepStatus.pending,
            )
        )
    await session.flush()
    await _event(
        session,
        plan,
        "plan.decomposition_adopted",
        actor="user",
        message=f"Принята декомпозиция: шагов {len(data.steps)}",
        data={"steps": len(data.steps), "reason": data.reason or ""},
    )
    await _refresh_readiness(session, plan)
    return plan


def parse_planner_output(payload: dict[str, Any]) -> PlannerOutput:
    """Разбор ответа агента-планировщика с понятными ошибками."""
    try:
        return PlannerOutput.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise ValidationError(
            "Ответ планировщика не соответствует контракту",
            code="invalid_planner_output",
            details={"error": str(exc)[:1000]},
        ) from exc


# ── Подбор исполнителей ────────────────────────────────────────────────
async def assign_steps(
    session: AsyncSession, plan: TaskPlan, *, force: bool = False
) -> tuple[int, int, list[dict[str, Any]]]:
    """Подбирает агентов по возможностям для шагов без исполнителя.

    Возвращает (назначено, заблокировано, детали по шагам).
    """
    steps = await plan_steps(session, plan.id)
    desired = {step.key: set(step.required_capabilities) for step in steps}
    assigned = 0
    blocked = 0
    details: list[dict[str, Any]] = []

    for step in steps:
        if step.agent_id is not None and not force:
            continue
        if step.status in {StepStatus.succeeded, StepStatus.running}:
            continue
        if not step.required_capabilities:
            # Шаг без требований: берём любого опубликованного агента нельзя —
            # исполнитель должен быть определён явно.
            if step.agent_id is None:
                step.status = StepStatus.blocked
                step.note = "Не указан агент и не заданы возможности"
                blocked += 1
                details.append(
                    {
                        "step_key": step.key,
                        "applied": False,
                        "reason": "Не указан агент и не заданы возможности",
                    }
                )
            continue

        ranking = await messaging_service.discover_agents(
            session,
            capabilities=step.required_capabilities,
            limit=10,
        )
        # Учитываем «наследственные» требования: агент шага должен уметь
        # и то, что умеют его зависимости — иначе результат не сойдётся.
        candidates = [
            match for match in ranking.matches if not match.missing_capabilities
        ]
        if not candidates:
            step.status = StepStatus.blocked
            step.note = (
                "Нет агента с нужными возможностями: "
                + ", ".join(step.required_capabilities)
            )
            blocked += 1
            details.append(
                {
                    "step_key": step.key,
                    "applied": False,
                    "missing_capabilities": sorted(
                        {
                            cap
                            for match in ranking.matches
                            for cap in match.missing_capabilities
                        }
                    ),
                    "reason": "Нет подходящего агента",
                }
            )
            continue

        best = candidates[0]
        step.agent_id = best.agent_id
        step.match_score = best.score
        step.status = StepStatus.pending
        step.note = None
        assigned += 1
        details.append(
            {
                "step_key": step.key,
                "applied": True,
                "agent_id": best.agent_id,
                "agent_slug": best.slug,
                "agent_name": best.name,
                "score": best.score,
                "matched_capabilities": best.matched_capabilities,
                "missing_capabilities": best.missing_capabilities,
            }
        )
    await session.flush()

    if plan.status is PlanStatus.draft and assigned:
        plan.status = PlanStatus.planned
        await _event(
            session,
            plan,
            "plan.assigned",
            message=f"Назначено исполнителей: {assigned}, заблокировано: {blocked}",
            data={"assigned": assigned, "blocked": blocked},
        )
    await _refresh_readiness(session, plan)
    logger.debug("plan_assigned plan=%s steps=%s desired=%s", plan.id, len(steps), desired)
    return assigned, blocked, details


# ── Готовность и запуск ────────────────────────────────────────────────
async def _refresh_readiness(session: AsyncSession, plan: TaskPlan) -> None:
    """pending ↔ ready по состоянию зависимостей; блокировка при провале."""
    steps = await plan_steps(session, plan.id)
    by_key = {step.key: step for step in steps}
    for step in steps:
        if step.status in TERMINAL_STEP_STATUSES or step.status in {
            StepStatus.queued,
            StepStatus.running,
        }:
            continue
        deps = [by_key[key] for key in step.depends_on if key in by_key]
        failed_deps = [
            dep
            for dep in deps
            if dep.status in {StepStatus.failed, StepStatus.skipped, StepStatus.blocked}
        ]
        if failed_deps:
            if plan.failure_policy is FailurePolicy.fail_plan:
                step.status = StepStatus.skipped
                step.note = "Зависимости не выполнены: " + ", ".join(d.key for d in failed_deps)
            else:
                step.status = StepStatus.skipped
                step.note = "Пропущен: зависимости не выполнены"
            continue
        if all(dep.status is StepStatus.succeeded for dep in deps):
            if step.status is StepStatus.pending:
                step.status = StepStatus.ready
        elif step.status is StepStatus.ready:
            step.status = StepStatus.pending
    await session.flush()


async def _launch_step(
    session: AsyncSession, plan: TaskPlan, step: PlanStep
) -> Task | None:
    if step.agent_id is None:
        return None
    agent = await agent_service.get_agent(session, step.agent_id)
    if agent.status is not AgentStatus.published:
        step.status = StepStatus.blocked
        step.note = "Агент-исполнитель больше не доступен"
        await _event(
            session,
            plan,
            "step.blocked",
            step_key=step.key,
            message=f"Шаг {step.key}: агент недоступен",
        )
        return None

    all_steps = await plan_steps(session, plan.id)
    merged = _merge_inputs(step, plan, all_steps)
    step.resolved_input = merged
    task = await gateway.submit_task(
        session,
        requester_id=plan.requester_id,
        data=TaskSubmitRequest(
            agent_id=step.agent_id,
            input_payload=merged,
            priority=5,
            max_attempts=step.max_attempts,
            timeout_seconds=plan.step_timeout_seconds,
        ),
    )
    step.task_id = task.id
    step.status = StepStatus.queued
    step.attempts += 1
    step.started_at = _now()
    step.note = None
    await session.flush()
    await _event(
        session,
        plan,
        "step.launched",
        step_key=step.key,
        message=f"Шаг {step.key} запущен заданием {task.id}",
        data={"task_id": str(task.id), "agent_id": str(step.agent_id)},
    )
    return task


def _merge_inputs(
    step: PlanStep, plan: TaskPlan, all_steps: list[PlanStep]
) -> dict[str, Any]:
    """Вход шага = цель плана + собственный вход + результаты зависимостей.

    Результаты зависимостей берутся напрямую из шагов плана (а не из
    `step.output_payload`): шаг считается готовым только когда все
    зависимости успешно завершены, и к этому моменту их результаты финальны.
    """
    payload: dict[str, Any] = {
        "plan": {"id": str(plan.id), "title": plan.title, "goal": plan.goal},
        "step": {"key": step.key, "name": step.name},
    }
    if step.input_payload:
        payload["input"] = step.input_payload
    by_key = {item.key: item for item in all_steps}
    outputs = {
        key: by_key[key].output_payload
        for key in step.depends_on
        if key in by_key and by_key[key].output_payload
    }
    if outputs:
        payload["depends_on_outputs"] = outputs
    return payload


async def run_plan(
    session: AsyncSession,
    plan: TaskPlan,
    *,
    limit: int | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    """Запускает готовые шаги с учётом max_parallelism.

    Один вызов = одна порция работы: запустить доступные шаги, обновить
    статусы по задачам, пересчитать готовность. Идемпотентно.
    """
    if plan.status in TERMINAL_PLAN_STATUSES:
        raise ConflictError("План уже завершён", code="plan_finished")

    await sync_plan(session, plan)

    if plan.status is PlanStatus.draft:
        # Если шаги уже назначены — переводим в planned
        steps = await plan_steps(session, plan.id)
        if steps and all(step.agent_id for step in steps):
            plan.status = PlanStatus.planned
    if plan.status is PlanStatus.planned:
        plan.status = PlanStatus.running
        plan.started_at = plan.started_at or _now()
        await _event(session, plan, "plan.started", message="План запущен", actor=actor)

    steps = await plan_steps(session, plan.id)
    running = sum(1 for step in steps if step.status in {StepStatus.queued, StepStatus.running})
    capacity = max(0, plan.max_parallelism - running)
    if limit is not None:
        capacity = min(capacity, limit)

    launched: list[str] = []
    for step in steps:
        if capacity <= 0:
            break
        if step.status is not StepStatus.ready:
            continue
        task = await _launch_step(session, plan, step)
        if task is not None:
            launched.append(step.key)
            capacity -= 1

    await _refresh_readiness(session, plan)
    await sync_plan(session, plan)
    await _maybe_finish(session, plan)
    progress = await plan_progress(session, plan)
    return {
        "launched": launched,
        "progress": progress,
    }


async def sync_plan(session: AsyncSession, plan: TaskPlan) -> list[str]:
    """Переносит статусы задач в шаги и подтягивает результаты зависимостей."""
    steps = await plan_steps(session, plan.id)
    changed: list[str] = []
    by_key = {step.key: step for step in steps}
    task_ids = [step.task_id for step in steps if step.task_id]
    tasks: dict[uuid.UUID, Task] = {}
    if task_ids:
        rows = (
            await session.execute(select(Task).where(Task.id.in_(task_ids)))
        ).scalars().all()
        tasks = {task.id: task for task in rows}

    for step in steps:
        if step.task_id is None:
            continue
        task = tasks.get(step.task_id)
        if task is None:
            continue
        new_status = TASK_TO_STEP_STATUS.get(task.status)
        if new_status is None:
            continue
        if new_status is StepStatus.succeeded:
            step.output_payload = task.output_payload or {}
            step.error = None
        elif new_status is StepStatus.failed:
            step.error = task.error or {"type": "TaskFailed", "message": "Задание не выполнено"}
        if step.status is not new_status:
            step.status = new_status
            changed.append(step.key)
            if new_status in TERMINAL_STEP_STATUSES:
                step.finished_at = task.finished_at or _now()
                if step.started_at:
                    step.duration_ms = int(
                        (step.finished_at - step.started_at).total_seconds() * 1000
                    )
                await _event(
                    session,
                    plan,
                    f"step.{new_status.value}",
                    step_key=step.key,
                    message=f"Шаг {step.key}: {new_status.value}",
                    data={
                        "task_id": str(task.id),
                        "error": step.error,
                        "duration_ms": step.duration_ms,
                    },
                )

    # Результаты зависимостей попадают во вход шага перед запуском
    for step in steps:
        if step.status is not StepStatus.ready:
            continue
        outputs = {
            key: by_key[key].output_payload
            for key in step.depends_on
            if key in by_key and by_key[key].status is StepStatus.succeeded
        }
        if outputs:
            step.output_payload = outputs

    await _refresh_readiness(session, plan)
    await session.flush()
    return changed


async def _maybe_finish(session: AsyncSession, plan: TaskPlan) -> None:
    steps = await plan_steps(session, plan.id)
    if not steps:
        return
    statuses = {step.status for step in steps}
    if any(status in {StepStatus.pending, StepStatus.ready, StepStatus.queued,
                      StepStatus.running} for status in statuses):
        return

    if all(status is StepStatus.succeeded for status in statuses):
        plan.status = PlanStatus.succeeded
    elif any(status is StepStatus.succeeded for status in statuses):
        plan.status = PlanStatus.failed
    else:
        plan.status = PlanStatus.failed
    plan.finished_at = _now()
    if plan.error is None and plan.status is PlanStatus.failed:
        plan.error = {
            "type": "PlanFailed",
            "message": "Не все шаги плана выполнены",
            "steps": {
                step.key: step.status.value
                for step in steps
                if step.status is not StepStatus.succeeded
            },
        }
    await session.flush()
    await _event(
        session,
        plan,
        f"plan.{plan.status.value}",
        message=f"План завершён со статусом {plan.status.value}",
        data={"steps": len(steps)},
    )


async def plan_progress(
    session: AsyncSession, plan: TaskPlan, *, sync: bool = True
) -> PlanProgress:
    if sync:
        await sync_plan(session, plan)
        await _maybe_finish(session, plan)
    steps = await plan_steps(session, plan.id)
    by_status: dict[str, int] = {}
    for step in steps:
        by_status[step.status.value] = by_status.get(step.status.value, 0) + 1
    finished = sum(
        1 for step in steps if step.status in TERMINAL_STEP_STATUSES
    )
    total = len(steps)
    return PlanProgress(
        plan_id=plan.id,
        status=plan.status,
        total=total,
        by_status=by_status,
        ready=by_status.get(StepStatus.ready.value, 0),
        running=by_status.get(StepStatus.queued.value, 0)
        + by_status.get(StepStatus.running.value, 0),
        finished=finished,
        percent=round(finished / total * 100, 1) if total else 0.0,
    )


async def retry_step(
    session: AsyncSession, plan: TaskPlan, step_key: str
) -> PlanStep:
    """Повтор неудавшегося шага: сбрасывает его в ready (или pending)."""
    # Статус шага мог отставать от задачи (узел отчитался об ошибке) —
    # подтягиваем его, иначе повтор отклонялся бы как «шаг ещё выполняется».
    await sync_plan(session, plan)
    steps = await plan_steps(session, plan.id)
    step = next((item for item in steps if item.key == step_key), None)
    if step is None:
        raise NotFoundError("Шаг не найден", code="step_not_found")
    if step.status not in {StepStatus.failed, StepStatus.skipped, StepStatus.blocked}:
        raise ValidationError(
            "Повторить можно только неудавшийся или пропущенный шаг", code="step_not_retryable"
        )
    if step.attempts >= step.max_attempts:
        raise ConflictError(
            "Исчерпаны попытки шага", code="step_attempts_exhausted",
            details={"attempts": step.attempts, "max_attempts": step.max_attempts},
        )
    step.status = StepStatus.pending
    step.error = None
    step.note = "Запрошен повтор шага"
    step.task_id = None
    step.finished_at = None
    step.duration_ms = None
    if step.agent_id is None:
        assigned, _, _ = await assign_steps(session, plan)
        logger.debug("retry_reassigned plan=%s assigned=%s", plan.id, assigned)
    await session.flush()
    if plan.status in TERMINAL_PLAN_STATUSES:
        plan.status = PlanStatus.running
        plan.finished_at = None
        plan.error = None
    await _refresh_readiness(session, plan)
    await _event(
        session,
        plan,
        "step.retry_requested",
        step_key=step.key,
        actor="user",
        message=f"Запрошен повтор шага {step.key}",
    )
    return step


async def cancel_plan(
    session: AsyncSession, plan: TaskPlan, *, reason: str = ""
) -> TaskPlan:
    if plan.status in TERMINAL_PLAN_STATUSES:
        raise ConflictError("План уже завершён", code="plan_finished")
    steps = await plan_steps(session, plan.id)
    for step in steps:
        if step.status in TERMINAL_STEP_STATUSES:
            continue
        step.status = StepStatus.skipped
        step.note = f"План отменён: {reason}"[:500]
        if step.task_id:
            task = (
                await session.execute(select(Task).where(Task.id == step.task_id))
            ).scalar_one_or_none()
            if task is not None and task.status in {
                TaskStatus.queued,
                TaskStatus.leased,
                TaskStatus.running,
            }:
                task.status = TaskStatus.cancelled
                task.finished_at = _now()
                task.error = {"type": "PlanCancelled", "message": reason[:500] or "План отменён"}
    plan.status = PlanStatus.cancelled
    plan.finished_at = _now()
    await session.flush()
    await _event(
        session,
        plan,
        "plan.cancelled",
        actor="user",
        message=f"План отменён: {reason}"[:1000],
    )
    return plan


async def plan_stats(session: AsyncSession, requester_id: uuid.UUID) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(TaskPlan.status, func.count())
            .where(TaskPlan.requester_id == requester_id)
            .group_by(TaskPlan.status)
        )
    ).all()
    by_status = {status.value: int(count) for status, count in rows}
    steps = (
        await session.execute(
            select(PlanStep.status, func.count())
            .join(TaskPlan, TaskPlan.id == PlanStep.plan_id)
            .where(TaskPlan.requester_id == requester_id)
            .group_by(PlanStep.status)
        )
    ).all()
    return {
        "plans": by_status,
        "plans_total": sum(by_status.values()),
        "steps": {status.value: int(count) for status, count in steps},
    }


__all__ = [
    "MAX_STEPS_PER_PLAN",
    "adopt_planner_output",
    "assign_steps",
    "cancel_plan",
    "create_plan",
    "create_plan_via_planner",
    "ensure_plan_access",
    "get_plan",
    "list_plans",
    "parse_planner_output",
    "plan_events",
    "plan_progress",
    "plan_stats",
    "plan_steps",
    "retry_step",
    "run_plan",
    "sync_plan",
]
