"""API оркестрации: планы, шаги, зависимости, запуск (этап 4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import Ctx, CurrentUser, DbSession, Paging
from app.db.models.orchestration import PlanStatus
from app.schemas.common import Page
from app.schemas.orchestration import (
    PlanAdoptRequest,
    PlanAssignmentResult,
    PlanCreate,
    PlanDetail,
    PlanEventPublic,
    PlanProgress,
    PlanPublic,
    PlanRunResult,
    PlanStepPublic,
    StepActionResult,
    StepAssignmentPublic,
)
from app.services import audit as audit_service
from app.services import orchestration as orchestration_service

router = APIRouter(tags=["orchestration"])


async def _detail(session, plan, *, with_events: bool = True) -> PlanDetail:
    steps = await orchestration_service.plan_steps(session, plan.id)
    events = (
        await orchestration_service.plan_events(session, plan.id, limit=100)
        if with_events
        else []
    )
    return PlanDetail(
        **PlanPublic.model_validate(plan).model_dump(),
        steps=[PlanStepPublic.model_validate(step) for step in steps],
        events=events,
    )


@router.post(
    "/plans",
    response_model=PlanDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Создать план составного задания",
)
async def create_plan(
    data: PlanCreate, session: DbSession, user: CurrentUser, ctx: Ctx
) -> PlanDetail:
    plan = await orchestration_service.create_plan(
        session, requester_id=user.id, data=data
    )
    if data.planner_agent_id is None:
        await orchestration_service.assign_steps(session, plan)
        if data.auto_start:
            await orchestration_service.run_plan(session, plan, actor="user")
    await audit_service.record(
        session,
        action="plan.create",
        target_type="plan",
        target_id=plan.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={
            "steps": len(data.steps),
            "planner_agent_id": str(data.planner_agent_id) if data.planner_agent_id else None,
            "auto_start": data.auto_start,
        },
    )
    await session.commit()
    return await _detail(session, plan)


@router.get("/plans", response_model=Page[PlanPublic], summary="Мои планы")
async def list_plans(
    session: DbSession,
    user: CurrentUser,
    paging: Paging,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(draft|planned|running|succeeded|failed|cancelled)$"),
    ] = None,
) -> Page[PlanPublic]:
    plan_status = PlanStatus(status_filter) if status_filter else None
    items, total = await orchestration_service.list_plans(
        session,
        requester_id=user.id,
        status=plan_status,
        offset=paging.offset,
        limit=paging.limit,
        is_admin=user.is_admin,
    )
    return Page.build(
        [PlanPublic.model_validate(item) for item in items],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.get("/plans/stats", summary="Статистика оркестрации")
async def plan_stats(session: DbSession, user: CurrentUser) -> dict:
    return await orchestration_service.plan_stats(session, user.id)


@router.get("/plans/{plan_id}", response_model=PlanDetail, summary="План с шагами и событиями")
async def get_plan(plan_id: uuid.UUID, session: DbSession, user: CurrentUser) -> PlanDetail:
    plan = await orchestration_service.get_plan(session, plan_id)
    orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
    return await _detail(session, plan)


@router.get(
    "/plans/{plan_id}/progress",
    response_model=PlanProgress,
    summary="Прогресс выполнения плана",
)
async def get_progress(
    plan_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> PlanProgress:
    plan = await orchestration_service.get_plan(session, plan_id)
    orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
    return await orchestration_service.plan_progress(session, plan)


@router.get(
    "/plans/{plan_id}/events",
    response_model=list[PlanEventPublic],
    summary="Журнал событий плана",
)
async def get_events(
    plan_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(100, ge=1, le=500),
) -> list[PlanEventPublic]:
    plan = await orchestration_service.get_plan(session, plan_id)
    orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
    return await orchestration_service.plan_events(session, plan.id, limit=limit)


@router.post(
    "/plans/{plan_id}/assign",
    response_model=PlanAssignmentResult,
    summary="Подобрать исполнителей по возможностям",
)
async def assign(
    plan_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> PlanAssignmentResult:
    plan = await orchestration_service.get_plan(session, plan_id)
    orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
    assigned, blocked, details = await orchestration_service.assign_steps(
        session, plan, force=True
    )
    await audit_service.record(
        session,
        action="plan.assign",
        target_type="plan",
        target_id=plan.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"assigned": assigned, "blocked": blocked},
    )
    await session.commit()
    return PlanAssignmentResult(
        plan_id=plan.id,
        assigned=assigned,
        blocked=blocked,
        steps=[StepAssignmentPublic.model_validate(item) for item in details],
    )


@router.post(
    "/plans/{plan_id}/adopt",
    response_model=PlanDetail,
    summary="Принять декомпозицию от агента-планировщика",
)
async def adopt(
    plan_id: uuid.UUID,
    data: PlanAdoptRequest,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> PlanDetail:
    plan = await orchestration_service.get_plan(session, plan_id)
    await orchestration_service.adopt_planner_output(session, plan, data, actor_id=user.id)
    await orchestration_service.assign_steps(session, plan)
    await audit_service.record(
        session,
        action="plan.adopt",
        target_type="plan",
        target_id=plan.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"steps": len(data.steps)},
    )
    await session.commit()
    return await _detail(session, plan)


@router.post(
    "/plans/{plan_id}/run",
    response_model=PlanRunResult,
    summary="Запустить готовые шаги (идемпотентно)",
)
async def run(
    plan_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
    limit: int | None = Query(default=None, ge=1, le=20),
) -> PlanRunResult:
    plan = await orchestration_service.get_plan(session, plan_id)
    orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
    outcome = await orchestration_service.run_plan(
        session, plan, limit=limit, actor=f"user:{user.id}"
    )
    await audit_service.record(
        session,
        action="plan.run",
        target_type="plan",
        target_id=plan.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"launched": outcome["launched"]},
    )
    await session.commit()
    steps = await orchestration_service.plan_steps(session, plan.id)
    failed = [step.key for step in steps if step.status.value == "failed"]
    blocked = [step.key for step in steps if step.status.value in {"blocked", "skipped"}]
    return PlanRunResult(
        plan_id=plan.id,
        status=plan.status,
        launched=outcome["launched"],
        completed=[step.key for step in steps if step.status.value == "succeeded"],
        failed=failed,
        blocked=blocked,
        progress=outcome["progress"],
    )


@router.post(
    "/plans/{plan_id}/steps/{step_key}/retry",
    response_model=StepActionResult,
    summary="Повторить неудавшийся шаг",
)
async def retry_step(
    plan_id: uuid.UUID,
    step_key: str,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> StepActionResult:
    plan = await orchestration_service.get_plan(session, plan_id)
    orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
    step = await orchestration_service.retry_step(session, plan, step_key)
    await audit_service.record(
        session,
        action="plan.step_retry",
        target_type="plan",
        target_id=plan.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"step": step_key},
    )
    await session.commit()
    return StepActionResult(
        step=PlanStepPublic.model_validate(step), task_id=step.task_id, status=plan.status
    )


@router.post(
    "/plans/{plan_id}/cancel",
    response_model=PlanPublic,
    summary="Отменить план",
)
async def cancel(
    plan_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
    reason: str = Query(default="", max_length=280),
) -> PlanPublic:
    plan = await orchestration_service.get_plan(session, plan_id)
    orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
    await orchestration_service.cancel_plan(session, plan, reason=reason)
    await audit_service.record(
        session,
        action="plan.cancel",
        target_type="plan",
        target_id=plan.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"reason": reason},
    )
    await session.commit()
    return PlanPublic.model_validate(plan)


__all__ = ["router"]
