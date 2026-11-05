"""API доверия: проверки результатов, репутация, отзывы, апелляции (этап 5)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.api.deps import Ctx, CurrentUser, DbSession, rate_limit_default
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models.agent import Agent, AgentCapability
from app.db.models.trust import (
    AnomalySignal,
    AnomalyStatus,
    Appeal,
    AppealStatus,
    ReputationSource,
    TaskVerdict,
    Verdict,
)
from app.schemas.trust import (
    AgentTrustProfile,
    AnomalyRead,
    AnomalyResolve,
    AnomalyScanResult,
    AppealCreate,
    AppealRead,
    AppealResolve,
    CapabilityTrust,
    HumanVerificationRequest,
    PeerVerificationRequest,
    RatingSummary,
    ReputationBreakdown,
    ReputationEventRead,
    ReviewCreate,
    ReviewRead,
    RunChecksRequest,
    TrustOverview,
    VerdictRead,
    VerificationRead,
    VerificationResult,
)
from app.services import audit as audit_service
from app.services import gateway
from app.services import trust as trust_service

router = APIRouter(tags=["trust"])


def _can_read_task(task, user) -> bool:
    return task.requester_id == user.id or user.is_admin


async def current_verdict(session, task_id: uuid.UUID) -> TaskVerdict | None:
    return (
        await session.execute(select(TaskVerdict).where(TaskVerdict.task_id == task_id))
    ).scalars().unique().one_or_none()


# ── Проверки результата ────────────────────────────────────────────────
@router.post(
    "/tasks/{task_id}/verifications",
    response_model=VerificationResult,
    status_code=status.HTTP_201_CREATED,
    summary="Запустить автоматические проверки результата",
    dependencies=[Depends(rate_limit_default)],
)
async def run_checks(
    task_id: uuid.UUID,
    data: RunChecksRequest,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> VerificationResult:
    task = await gateway.get_task(session, task_id)
    if not _can_read_task(task, user):
        raise PermissionDeniedError(
            "Проверить результат может владелец задания или админ",
            code="verification_forbidden",
        )
    checks = await trust_service.run_automatic_checks(session, task, data.model_dump())
    await audit_service.record(
        session,
        action="trust.verify.auto",
        target_type="task",
        target_id=task.id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"checks": len(checks)},
    )
    all_checks = await trust_service.list_verifications(session, task.id)
    verdict = await current_verdict(session, task.id)
    response = VerificationResult(
        checks=[VerificationRead.model_validate(item) for item in all_checks],
        verdict=VerdictRead.model_validate(verdict) if verdict is not None else None,
    )
    await session.commit()
    return response


@router.post(
    "/tasks/{task_id}/verifications/peer",
    response_model=VerificationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Независимая проверка результата другим агентом",
    dependencies=[Depends(rate_limit_default)],
)
async def peer_verification(
    task_id: uuid.UUID,
    data: PeerVerificationRequest,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> VerificationRead:
    task = await gateway.get_task(session, task_id)
    if not _can_read_task(task, user):
        raise PermissionDeniedError(
            "Запросить независимую проверку может владелец задания",
            code="verification_forbidden",
        )
    verification = await trust_service.request_peer_verification(
        session,
        task,
        verifier_agent_id=data.verifier_agent_id,
        requester_id=user.id,
    )
    await audit_service.record(
        session,
        action="trust.verify.peer",
        target_type="task",
        target_id=task.id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"verifier_agent_id": str(data.verifier_agent_id)},
    )
    await session.commit()
    return VerificationRead.model_validate(verification)


@router.post(
    "/tasks/{task_id}/verifications/human",
    response_model=VerificationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Ручная проверка результата",
    dependencies=[Depends(rate_limit_default)],
)
async def human_verification(
    task_id: uuid.UUID,
    data: HumanVerificationRequest,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> VerificationRead:
    task = await gateway.get_task(session, task_id)
    verification = await trust_service.submit_human_verification(
        session, task, actor=user, passed=data.passed, comment=data.comment
    )
    await audit_service.record(
        session,
        action="trust.verify.human",
        target_type="task",
        target_id=task.id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"passed": data.passed},
    )
    await session.commit()
    return VerificationRead.model_validate(verification)


@router.get(
    "/tasks/{task_id}/verifications",
    response_model=list[VerificationRead],
    summary="Проверки задания",
)
async def list_checks(
    task_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> list[VerificationRead]:
    task = await gateway.get_task(session, task_id)
    if not _can_read_task(task, user):
        raise PermissionDeniedError("Нет доступа к заданию", code="task_forbidden")
    checks = await trust_service.list_verifications(session, task.id)
    return [VerificationRead.model_validate(item) for item in checks]


@router.get(
    "/tasks/{task_id}/verdict",
    response_model=VerdictRead,
    summary="Вердикт по заданию",
)
async def get_verdict(
    task_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> VerdictRead:
    task = await gateway.get_task(session, task_id)
    if not _can_read_task(task, user):
        raise PermissionDeniedError("Нет доступа к заданию", code="task_forbidden")
    verdict = await current_verdict(session, task.id)
    if verdict is None:
        raise NotFoundError("Вердикт ещё не вынесен", code="verdict_not_found")
    return VerdictRead.model_validate(verdict)


# ── Отзывы ─────────────────────────────────────────────────────────────
@router.post(
    "/tasks/{task_id}/review",
    response_model=ReviewRead,
    status_code=status.HTTP_201_CREATED,
    summary="Оставить отзыв о результате",
    dependencies=[Depends(rate_limit_default)],
)
async def create_review(
    task_id: uuid.UUID,
    data: ReviewCreate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> ReviewRead:
    task = await gateway.get_task(session, task_id)
    review = await trust_service.create_review(
        session,
        task,
        actor=user,
        rating=data.rating,
        comment=data.comment,
        confirm_result=data.confirm_result,
    )
    await audit_service.record(
        session,
        action="trust.review.create",
        target_type="agent",
        target_id=task.agent_id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"task_id": str(task.id), "rating": data.rating},
    )
    await session.commit()
    return ReviewRead.model_validate(review)


# ── Репутация и профиль доверия ────────────────────────────────────────
@router.get(
    "/agents/{agent_id}/reputation",
    response_model=ReputationBreakdown,
    summary="Объяснение репутации агента",
)
async def reputation(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> ReputationBreakdown:
    return await _breakdown(session, agent_id)


@router.get(
    "/agents/{agent_id}/rating",
    response_model=RatingSummary,
    summary="Сводка отзывов об агенте",
)
async def rating(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> RatingSummary:
    return RatingSummary(**await trust_service.agent_rating_summary(session, agent_id))


@router.get(
    "/agents/{agent_id}/trust",
    response_model=AgentTrustProfile,
    summary="Профиль доверия агента",
)
async def agent_trust(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> AgentTrustProfile:
    from sqlalchemy import func

    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")

    capabilities = list(
        (
            await session.execute(
                select(AgentCapability).where(AgentCapability.agent_id == agent_id)
            )
        ).scalars().all()
    )
    verdict_counts = {verdict.value: 0 for verdict in Verdict}
    for verdict, count in (
        await session.execute(
            select(TaskVerdict.verdict, func.count())
            .where(TaskVerdict.agent_id == agent_id)
            .group_by(TaskVerdict.verdict)
        )
    ).all():
        verdict_counts[verdict.value] = int(count)

    open_anomalies = int(
        (
            await session.execute(
                select(func.count())
                .select_from(AnomalySignal)
                .where(
                    AnomalySignal.agent_id == agent_id,
                    AnomalySignal.status == AnomalyStatus.open,
                )
            )
        ).scalar_one()
    )
    open_appeals = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Appeal)
                .where(Appeal.agent_id == agent_id, Appeal.status == AppealStatus.open)
            )
        ).scalar_one()
    )
    summary = await trust_service.agent_rating_summary(session, agent_id)

    def _trust_level(declared: int, verified: int | None) -> str:
        if verified is None:
            return "declared"
        if verified >= declared:
            return "verified"
        return "unconfirmed"

    return AgentTrustProfile(
        agent_id=agent.id,
        agent_name=agent.name,
        reputation_score=float(agent.reputation_score),
        is_verified=bool(agent.is_verified),
        completed_tasks=agent.completed_tasks,
        failed_tasks=agent.failed_tasks,
        rating=RatingSummary(**summary),
        verdicts=verdict_counts,
        capabilities=[
            CapabilityTrust(
                id=capability.id,
                key=capability.capability_key,
                name=capability.capability_key,
                declared_level=capability.declared_level,
                verified_level=capability.verified_level,
                verified_at=capability.verified_at,
                trust_level=_trust_level(
                    capability.declared_level, capability.verified_level
                ),
            )
            for capability in capabilities
        ],
        open_anomalies=open_anomalies,
        open_appeals=open_appeals,
        reputation=await _breakdown(session, agent_id),
    )


async def _breakdown(session, agent_id) -> ReputationBreakdown:
    data = await trust_service.reputation_breakdown(session, agent_id)
    events = [ReputationEventRead(**event) for event in data["events"]]
    return ReputationBreakdown(**{**data, "events": events})


@router.post(
    "/agents/{agent_id}/verification/refresh",
    response_model=list[CapabilityTrust],
    summary="Пересчитать проверенные возможности агента",
)
async def refresh_capabilities(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> list[CapabilityTrust]:
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    if agent.owner_id != user.id and not user.is_admin:
        raise PermissionDeniedError(
            "Пересчёт доступен владельцу агента", code="capability_forbidden"
        )
    capabilities = await trust_service.refresh_verified_capabilities(session, agent_id)
    await audit_service.record(
        session,
        action="trust.capabilities.refresh",
        target_type="agent",
        target_id=agent_id,
        actor_id=user.id,
        request_id=ctx.request_id,
    )
    await session.commit()
    return [
        CapabilityTrust(
            id=capability.id,
            key=capability.capability_key,
            name=capability.capability_key,
            declared_level=capability.declared_level,
            verified_level=capability.verified_level,
            verified_at=capability.verified_at,
            trust_level=(
                "verified"
                if capability.verified_level is not None
                and capability.verified_level >= capability.declared_level
                else ("declared" if capability.verified_level is None else "unconfirmed")
            ),
        )
        for capability in capabilities
    ]


# ── Аномалии ───────────────────────────────────────────────────────────
@router.post(
    "/agents/{agent_id}/anomalies/scan",
    response_model=AnomalyScanResult,
    summary="Найти подозрительные паттерны агента",
    dependencies=[Depends(rate_limit_default)],
)
async def scan_anomalies(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> AnomalyScanResult:
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    if agent.owner_id != user.id and not user.is_admin:
        raise PermissionDeniedError(
            "Сканирование доступно владельцу агента", code="anomaly_forbidden"
        )
    found = await trust_service.detect_anomalies(session, agent_id)
    await audit_service.record(
        session,
        action="trust.anomalies.scan",
        target_type="agent",
        target_id=agent_id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"found": len(found)},
    )
    await session.commit()
    return AnomalyScanResult(
        agent_id=agent_id, found=[AnomalyRead.model_validate(item) for item in found]
    )


@router.get(
    "/anomalies",
    response_model=list[AnomalyRead],
    summary="Список сигналов аномалий",
)
async def list_anomalies(
    session: DbSession,
    user: CurrentUser,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[AnomalyStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AnomalyRead]:
    if agent_id is not None:
        agent = (
            await session.execute(select(Agent).where(Agent.id == agent_id))
        ).scalars().unique().one_or_none()
        if agent is None:
            raise NotFoundError("Агент не найден", code="agent_not_found")
        if agent.owner_id != user.id and not user.is_admin:
            raise PermissionDeniedError("Нет доступа к аномалиям агента", code="anomaly_forbidden")
    elif not user.is_admin:
        raise PermissionDeniedError(
            "Общий список аномалий доступен админу; укажите agent_id",
            code="anomaly_forbidden",
        )
    items = await trust_service.list_anomalies(
        session, agent_id=agent_id, status=status_filter, limit=limit
    )
    return [AnomalyRead.model_validate(item) for item in items]


@router.post(
    "/anomalies/{anomaly_id}/resolve",
    response_model=AnomalyRead,
    summary="Подтвердить или отклонить аномалию (админ)",
)
async def resolve_anomaly(
    anomaly_id: uuid.UUID,
    data: AnomalyResolve,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AnomalyRead:
    anomaly = await trust_service.resolve_anomaly(
        session, anomaly_id, actor=user, confirm=data.confirm
    )
    await audit_service.record(
        session,
        action="trust.anomaly.resolve",
        target_type="agent",
        target_id=anomaly.agent_id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"confirm": data.confirm},
    )
    await session.commit()
    return AnomalyRead.model_validate(anomaly)


# ── Апелляции ──────────────────────────────────────────────────────────
@router.post(
    "/appeals",
    response_model=AppealRead,
    status_code=status.HTTP_201_CREATED,
    summary="Подать апелляцию",
    dependencies=[Depends(rate_limit_default)],
)
async def create_appeal(
    data: AppealCreate, session: DbSession, user: CurrentUser, ctx: Ctx
) -> AppealRead:
    appeal = await trust_service.open_appeal(
        session,
        actor=user,
        agent_id=data.agent_id,
        reason=data.reason,
        task_id=data.task_id,
        verification_id=data.verification_id,
        anomaly_id=data.anomaly_id,
        evidence=data.evidence,
    )
    await audit_service.record(
        session,
        action="trust.appeal.open",
        target_type="agent",
        target_id=data.agent_id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"appeal_id": str(appeal.id)},
    )
    await session.commit()
    return AppealRead.model_validate(appeal)


@router.get("/appeals", response_model=list[AppealRead], summary="Список апелляций")
async def list_appeals(
    session: DbSession,
    user: CurrentUser,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[AppealStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AppealRead]:
    items = await trust_service.list_appeals(
        session, actor=user, agent_id=agent_id, status=status_filter, limit=limit
    )
    return [AppealRead.model_validate(item) for item in items]


@router.post(
    "/appeals/{appeal_id}/resolve",
    response_model=AppealRead,
    summary="Рассмотреть апелляцию (админ)",
)
async def resolve_appeal(
    appeal_id: uuid.UUID,
    data: AppealResolve,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AppealRead:
    appeal = await trust_service.resolve_appeal(
        session, appeal_id, actor=user, accept=data.accept, resolution=data.resolution
    )
    await audit_service.record(
        session,
        action="trust.appeal.resolve",
        target_type="agent",
        target_id=appeal.agent_id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"accept": data.accept},
    )
    await session.commit()
    return AppealRead.model_validate(appeal)


# ── Обзор ──────────────────────────────────────────────────────────────
@router.get(
    "/trust/overview",
    response_model=TrustOverview,
    summary="Сводка по доверию и верификации",
)
async def overview(session: DbSession, user: CurrentUser) -> TrustOverview:
    return TrustOverview(**await trust_service.trust_overview(session))


@router.get(
    "/trust/sources",
    response_model=list[dict],
    summary="Справочник источников репутации",
)
async def sources() -> list[dict]:
    return [
        {
            "source": source.value,
            "weight": trust_service.SOURCE_WEIGHTS[source],
            "description": {
                ReputationSource.task_success: "Успешно выполненное задание",
                ReputationSource.task_failure: "Проваленное задание",
                ReputationSource.verification_passed: "Проверенный результат",
                ReputationSource.verification_failed: "Опровергнутый результат",
                ReputationSource.review: "Отзыв владельца задания",
                ReputationSource.anomaly_penalty: "Подтверждённая аномалия",
                ReputationSource.appeal: "Решение по апелляции",
                ReputationSource.manual: "Корректировка админом",
            }[source],
        }
        for source in ReputationSource
    ]


__all__ = ["router"]
