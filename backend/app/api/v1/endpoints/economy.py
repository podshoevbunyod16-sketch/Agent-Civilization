"""API внутренней экономики: счета, журнал, бюджеты, награды (этап 6).

Расчётные единицы ACU — внутренний учёт вклада, не криптовалюта и не деньги.
Все изменяющие операции пишут двойную запись и попадают в аудит.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.api.deps import (
    Ctx,
    CurrentUser,
    DbSession,
    VerifiedUser,
    rate_limit_critical,
    rate_limit_write,
)
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models.economy import (
    Account,
    AccountType,
    RewardGrant,
    RewardRule,
    TaskBudget,
)
from app.schemas.economy import (
    AccountRead,
    AccountStatement,
    BudgetRead,
    EconomyStats,
    LedgerVerification,
    MoneyRead,
    RewardGrantRead,
    RewardRuleRead,
    RewardRuleUpdate,
    TransactionRead,
    TransactionWithImpact,
    TransferRequest,
    WithdrawRequest,
)
from app.services import audit as audit_service
from app.services import economy as economy_service

router = APIRouter(prefix="/economy", tags=["economy"])


@router.get(
    "/me",
    response_model=MoneyRead,
    summary="Мой баланс и заработок агентов",
)
async def my_money(session: DbSession, user: CurrentUser) -> MoneyRead:
    data = await economy_service.user_economy(session, user.id)
    # Счёт создаётся при первом обращении — фиксируем, иначе он откатится
    # вместе с завершением запроса и следующий вызов создаст его заново.
    await session.commit()
    return MoneyRead(**data)


@router.get(
    "/accounts",
    response_model=list[AccountRead],
    summary="Мои счета",
)
async def my_accounts(session: DbSession, user: CurrentUser) -> list[AccountRead]:
    from app.db.models.agent import Agent

    owned_agents = select(Agent.id).where(Agent.owner_id == user.id)
    query = (
        select(Account)
        .where(
            (Account.owner_id == user.id)
            | (Account.agent_id.in_(owned_agents))
            | (Account.type == AccountType.treasury)
        )
        .order_by(Account.created_at)
    )
    if not user.is_admin:
        query = query.where(Account.type != AccountType.treasury)
    accounts = list((await session.execute(query)).scalars().all())
    await session.commit()
    return [AccountRead.model_validate(account) for account in accounts]


@router.get(
    "/accounts/{account_id}/statement",
    response_model=AccountStatement,
    summary="Выписка по счёту",
)
async def account_statement(
    account_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AccountStatement:
    account = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalars().unique().one_or_none()
    if account is None:
        raise NotFoundError("Счёт не найден", code="account_not_found")
    owns = account.owner_id == user.id
    if account.agent_id is not None:
        from app.db.models.agent import Agent

        agent = (
            await session.execute(select(Agent).where(Agent.id == account.agent_id))
        ).scalars().unique().one_or_none()
        owns = owns or (agent is not None and agent.owner_id == user.id)
    if not owns and not user.is_admin:
        raise PermissionDeniedError("Чужой счёт", code="account_forbidden")
    data = await economy_service.account_statement(session, account, limit=limit)
    return AccountStatement(**data)


@router.get(
    "/transactions",
    response_model=list[TransactionWithImpact],
    summary="История операций",
)
async def transactions(
    session: DbSession,
    user: CurrentUser,
    account_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TransactionWithImpact]:
    rows = await economy_service.transactions_page(
        session, actor=user, account_id=account_id, limit=limit
    )
    return [
        TransactionWithImpact(
            transaction=TransactionRead.model_validate(transaction), impact=impact
        )
        for transaction, impact in rows
    ]


@router.post(
    "/transfer",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Перевод между счетами",
    dependencies=[Depends(rate_limit_write)],
)
async def transfer(
    data: TransferRequest, session: DbSession, user: VerifiedUser, ctx: Ctx
) -> TransactionRead:
    transaction = await economy_service.transfer(
        session,
        actor=user,
        from_account_id=data.from_account_id,
        to_account_id=data.to_account_id,
        amount_units=data.amount_units,
        comment=data.comment,
        idempotency_key=data.idempotency_key,
    )
    await audit_service.record(
        session,
        action="economy.transfer",
        target_type="transaction",
        target_id=transaction.id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"amount": data.amount_units, "from": str(data.from_account_id)},
    )
    await session.commit()
    return TransactionRead.model_validate(transaction)


@router.post(
    "/withdraw",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Вывести заработок агента на свой счёт",
    dependencies=[Depends(rate_limit_critical)],
)
async def withdraw(
    data: WithdrawRequest, session: DbSession, user: VerifiedUser, ctx: Ctx
) -> TransactionRead:
    transaction = await economy_service.withdraw_agent_earnings(
        session, actor=user, agent_id=data.agent_id, amount_units=data.amount_units
    )
    await audit_service.record(
        session,
        action="economy.withdraw",
        target_type="agent",
        target_id=data.agent_id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"amount": data.amount_units},
    )
    await session.commit()
    return TransactionRead.model_validate(transaction)


@router.post(
    "/transactions/{transaction_id}/reverse",
    response_model=TransactionRead,
    summary="Отменить операцию обратной транзакцией (админ)",
)
async def reverse(
    transaction_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
    reason: Annotated[str, Query(max_length=280)] = "решение администратора",
) -> TransactionRead:
    if not user.is_admin:
        raise PermissionDeniedError(
            "Отмена операции доступна админу", code="transaction_forbidden"
        )
    transaction = await economy_service.reverse_transaction(
        session, transaction_id, reason=reason, initiated_by_id=user.id
    )
    await audit_service.record(
        session,
        action="economy.reverse",
        target_type="transaction",
        target_id=transaction_id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data={"reason": reason},
    )
    await session.commit()
    return TransactionRead.model_validate(transaction)


@router.get(
    "/tasks/{task_id}/budget",
    response_model=BudgetRead,
    summary="Бюджет задания",
)
async def task_budget(
    task_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> BudgetRead:
    from app.services import gateway

    task = await gateway.get_task(session, task_id)
    if task.requester_id != user.id and not user.is_admin:
        raise PermissionDeniedError("Чужое задание", code="task_forbidden")
    budget = (
        await session.execute(select(TaskBudget).where(TaskBudget.task_id == task.id))
    ).scalars().unique().one_or_none()
    if budget is None:
        raise NotFoundError("У задания нет бюджета", code="budget_not_found")
    return BudgetRead.model_validate(budget)


@router.get(
    "/rules",
    response_model=list[RewardRuleRead],
    summary="Правила наград",
)
async def rules(session: DbSession, user: CurrentUser) -> list[RewardRuleRead]:
    rows = list(
        (
            await session.execute(select(RewardRule).order_by(RewardRule.created_at))
        ).scalars().all()
    )
    return [RewardRuleRead.model_validate(row) for row in rows]


@router.patch(
    "/rules/{rule_id}",
    response_model=RewardRuleRead,
    summary="Изменить правило наград (админ)",
)
async def update_rule(
    rule_id: uuid.UUID,
    data: RewardRuleUpdate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> RewardRuleRead:
    if not user.is_admin:
        raise PermissionDeniedError("Изменение правил доступно админу", code="rule_forbidden")
    rule = (
        await session.execute(select(RewardRule).where(RewardRule.id == rule_id))
    ).scalars().unique().one_or_none()
    if rule is None:
        raise NotFoundError("Правило не найдено", code="rule_not_found")
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(rule, field, value)
    await audit_service.record(
        session,
        action="economy.rule.update",
        target_type="reward_rule",
        target_id=rule.id,
        actor_id=user.id,
        request_id=ctx.request_id,
        data=changes,
    )
    await session.commit()
    return RewardRuleRead.model_validate(rule)


@router.get(
    "/rewards",
    response_model=list[RewardGrantRead],
    summary="Начисления наград",
)
async def rewards(
    session: DbSession,
    user: CurrentUser,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RewardGrantRead]:
    from app.db.models.agent import Agent

    query = select(RewardGrant)
    if not user.is_admin:
        query = query.where(
            RewardGrant.agent_id.in_(select(Agent.id).where(Agent.owner_id == user.id))
        )
    if agent_id is not None:
        query = query.where(RewardGrant.agent_id == agent_id)
    query = query.order_by(RewardGrant.created_at.desc()).limit(limit)
    rows = list((await session.execute(query)).scalars().all())
    return [RewardGrantRead.model_validate(row) for row in rows]


@router.get(
    "/stats",
    response_model=EconomyStats,
    summary="Сводка экономики",
)
async def stats(session: DbSession, user: CurrentUser) -> EconomyStats:
    return EconomyStats(**await economy_service.economy_stats(session))


@router.post(
    "/verify",
    response_model=LedgerVerification,
    summary="Сверка журнала: инвариант двойной записи",
)
async def verify(session: DbSession, user: CurrentUser) -> LedgerVerification:
    """Доступно всем авторизованным: прозрачность журнала — часть доверия.

    Проверяет, что каждая транзакция сходится и кэш балансов совпадает с
    суммой проводок. Расхождение требует обратной транзакции, а не правки.
    """
    report = await economy_service.verify_ledger(session)
    return LedgerVerification(**report)


__all__ = ["router"]
