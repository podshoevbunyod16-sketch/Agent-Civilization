"""Внутренняя экономика: счета, двойная запись, бюджеты, награды (этап 6).

Ключевое правило: **деньги не появляются из воздуха**. Любая операция — это
группа проводок, сумма которых равна нулю. Единственный счёт, которому
разрешено уходить в минус, — `treasury` (эмиссия платформы). Остальные счета
не могут стать отрицательными, кроме случаев явно разрешённого
`credit_limit`.

Порядок операций над счётом (важно для конкурентности):

1. счета блокируются `SELECT ... FOR UPDATE` в детерминированном порядке
   (по возрастанию `id`) — это исключает взаимные блокировки между
   параллельными переводами;
2. проверяются статус счёта и достаточность средств;
3. вставляются проводки с `balance_after` — снимок баланса после операции;
4. обновляется кэш `accounts.balance_units`;
5. проверяется инвариант `SUM(amount_units) = 0` по транзакции — если нет,
   операция прерывается (транзакция БД откатывается).

Идемпотентность: у финансовой операции есть `idempotency_key`; повторный
вызов возвращает существующую транзакцию. Это защищает от двойных начислений
при повторных отчётах узлов и повторных запросах клиента.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.db.models.agent import Agent, AgentCapability
from app.db.models.economy import (
    Account,
    AccountStatus,
    AccountType,
    HoldStatus,
    LedgerEntry,
    RewardGrant,
    RewardRule,
    TaskBudget,
    Transaction,
    TransactionKind,
    TransactionStatus,
)
from app.db.models.task import Task, TaskStatus
from app.db.models.trust import TaskVerdict, Verdict
from app.db.models.user import User

logger = logging.getLogger(__name__)

CURRENCY = "ACU"
# Стартовый бюджет нового участника (внутренние единицы, не деньги)
SIGNUP_GRANT_UNITS = 100.0
DEFAULT_FEE_RATIO = 0.05
# Допуск на погрешность float при сверке проводок
EPSILON = 1e-6
# Квант внутренних единиц: 4 знака. Суммы приходят как Decimal и
# квантуются один раз на входе в сервис, чтобы 0.1+0.2 не превращалось
# в хвост float (этап 10, E).
UNIT_QUANT = Decimal("0.0001")
MAX_AMOUNT = Decimal("1000000")
MAX_BALANCE = Decimal("1000000000000")


def to_units(value: object) -> Decimal:
    """Приводит сумму к Decimal с 4 знаками, отклоняя «мусор» и переполнение."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("Некорректная сумма", code="amount_invalid") from exc
    if not amount.is_finite():
        raise ValidationError("Некорректная сумма", code="amount_invalid")
    quantized = amount.quantize(UNIT_QUANT)
    if quantized != amount:
        raise ValidationError(
            "Сумма поддерживает не более 4 знаков после запятой", code="amount_precision"
        )
    if abs(quantized) > MAX_AMOUNT:
        raise ValidationError("Слишком большая сумма", code="amount_too_large")
    return quantized


def _now() -> datetime:
    return datetime.now(UTC)


class LedgerInconsistent(RuntimeError):
    """Нарушен инвариант двойной записи — операция должна быть отменена."""


# ── Счета ──────────────────────────────────────────────────────────────
async def get_or_create_account(
    session: AsyncSession,
    *,
    account_type: AccountType,
    owner_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    label: str = "",
) -> Account:
    """Идемпотентно возвращает счёт нужного типа."""
    query = select(Account).where(Account.type == account_type, Account.currency == CURRENCY)
    query = query.where(Account.owner_id == owner_id) if owner_id else query.where(
        Account.owner_id.is_(None)
    )
    if agent_id is not None:
        query = query.where(Account.agent_id == agent_id)
    account = (await session.execute(query)).scalars().unique().one_or_none()
    if account is not None:
        return account

    account = Account(
        type=account_type,
        owner_id=owner_id,
        agent_id=agent_id,
        label=label[:120],
        balance_units=0.0,
        credit_limit=0.0,
    )
    if account_type is AccountType.treasury:
        # Treasury — единственный счёт с разрешённым минусом: он «печатает»
        account.credit_limit = 1e12
    session.add(account)
    try:
        await session.flush()
    except IntegrityError:  # гонка на создании — перечитываем существующий
        await session.rollback()
        return await get_or_create_account(
            session,
            account_type=account_type,
            owner_id=owner_id,
            agent_id=agent_id,
            label=label,
        )
    return account


async def treasury_account(session: AsyncSession) -> Account:
    return await get_or_create_account(
        session, account_type=AccountType.treasury, label="Казна платформы"
    )


async def user_account(session: AsyncSession, user_id: uuid.UUID) -> Account:
    return await get_or_create_account(
        session, account_type=AccountType.user, owner_id=user_id, label="Бюджет участника"
    )


async def agent_account(session: AsyncSession, agent_id: uuid.UUID) -> Account:
    return await get_or_create_account(
        session, account_type=AccountType.agent, agent_id=agent_id, label="Заработок агента"
    )


async def escrow_account(session: AsyncSession, task_id: uuid.UUID) -> Account:
    return await get_or_create_account(
        session, account_type=AccountType.escrow, owner_id=None, label=f"Эскроу задачи {task_id}"
    )


async def ensure_signup_grant(session: AsyncSession, user_id: uuid.UUID) -> Account:
    """Стартовый бюджет: один раз на пользователя, идемпотентно."""
    account = await user_account(session, user_id)
    await post_transaction(
        session,
        kind=TransactionKind.deposit,
        amount_units=SIGNUP_GRANT_UNITS,
        description="Стартовый бюджет участника",
        idempotency_key=f"signup:{user_id}",
        entries=[
            (await treasury_account(session), -SIGNUP_GRANT_UNITS),
            (account, SIGNUP_GRANT_UNITS),
        ],
    )
    return account


# ── Ядро: двойная запись ───────────────────────────────────────────────
async def _lock_accounts(
    session: AsyncSession, accounts: list[Account]
) -> dict[uuid.UUID, Account]:
    """Блокирует счета в детерминированном порядке и возвращает актуальные версии."""
    ordered = sorted({account.id for account in accounts})
    rows = (
        await session.execute(
            select(Account)
            .where(Account.id.in_(ordered))
            .order_by(Account.id)
            .with_for_update()
            # populate_existing обязателен: без него SQLAlchemy вернёт уже
            # загруженные в сессию объекты с их прежними (устаревшими)
            # балансами, и параллельная транзакция «перезапишет» чужое
            # списание — классический lost update. Тест
            # test_concurrent_transfers_do_not_lose_money это ловит.
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    locked = {row.id: row for row in rows}
    missing = [item for item in ordered if item not in locked]
    if missing:
        raise NotFoundError("Счёт не найден", code="account_not_found", details={"ids": missing})
    return locked


async def post_transaction(
    session: AsyncSession,
    *,
    kind: TransactionKind,
    amount_units: float,
    entries: list[tuple[Account, float]],
    description: str = "",
    task_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    initiated_by_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    details: dict[str, Any] | None = None,
    reversal_of_id: uuid.UUID | None = None,
) -> Transaction:
    """Записывает сбалансированную транзакцию.

    `entries` — пары (счёт, сумма со знаком). Сумма обязана быть нулём;
    иначе транзакция не создаётся (инвариант двойной записи).
    """
    if not entries or len(entries) < 2:
        raise ValidationError(
            "Транзакция требует минимум двух проводок", code="ledger_needs_two_entries"
        )
    total = sum(amount for _, amount in entries)
    if abs(total) > EPSILON:
        raise ValidationError(
            "Сумма проводок должна быть нулём (двойная запись)",
            code="ledger_unbalanced",
            details={"sum": round(float(total), 8)},
        )
    if amount_units < 0:
        raise ValidationError(
            "Сумма транзакции не может быть отрицательной", code="ledger_negative"
        )
    # Переполнение: сумма и каждая проводка ограничены сверху, чтобы
    # «бесконечный» баланс нельзя было получить даже при баге в расчётах.
    to_units(abs(amount_units))  # сумма обязана быть точной (4 знака)
    for _account, amount in entries:
        if abs(to_units(abs(amount))) > MAX_AMOUNT:
            raise ValidationError(
                "Проводка превышает допустимую сумму", code="amount_too_large"
            )

    if idempotency_key:
        existing = (
            await session.execute(
                select(Transaction).where(Transaction.idempotency_key == idempotency_key)
            )
        ).scalars().unique().one_or_none()
        if existing is not None:
            # Повтор операции — возвращаем исходную транзакцию без новых проводок
            logger.info("ledger_idempotent_replay key=%s", idempotency_key)
            return existing

    locked = await _lock_accounts(session, [account for account, _ in entries])

    transaction = Transaction(
        kind=kind,
        status=TransactionStatus.posted,
        amount_units=round(amount_units, 4),
        description=description[:280],
        task_id=task_id,
        agent_id=agent_id,
        initiated_by_id=initiated_by_id,
        idempotency_key=idempotency_key,
        details=details or {},
        posted_at=_now(),
        reversal_of_id=reversal_of_id,
    )
    session.add(transaction)
    await session.flush()

    applied: dict[uuid.UUID, float] = {}
    for account, amount in entries:
        current = locked[account.id]
        if current.status is not AccountStatus.active:
            raise ValidationError(
                "Счёт неактивен",
                code="account_frozen",
                details={"account_id": str(current.id), "status": current.status.value},
            )
        # Арифметика баланса — в Decimal: складываем кэш, накопленное по
        # этому же счёту в текущей транзакции и сумму проводки, затем
        # квантуем один раз. Так 0.1+0.2 не даёт хвоста.
        precise_amount = to_units(amount)
        started = to_units(current.balance_units) + to_units(applied.get(current.id, 0.0))
        new_balance = float(to_units(started + precise_amount))
        applied[current.id] = float(to_units(applied.get(current.id, 0.0)) + precise_amount)
        if to_units(new_balance) > MAX_BALANCE:
            raise ValidationError(
                "Баланс превысил допустимый предел", code="balance_overflow"
            )
        # Отрицательный баланс допустим только в пределах credit_limit
        if new_balance < -abs(current.credit_limit) - EPSILON:
            raise ValidationError(
                "Недостаточно средств",
                code="insufficient_funds",
                details={
                    "account_id": str(current.id),
                    "balance": round(current.balance_units, 4),
                    "required": round(-float(precise_amount), 4),
                },
            )
        session.add(
            LedgerEntry(
                transaction_id=transaction.id,
                account_id=current.id,
                amount_units=round(amount, 6),
                balance_after=new_balance,
            )
        )
        current.balance_units = new_balance

    await session.flush()

    # Инвариант двойной записи по факту записанных проводок
    written = (
        await session.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_units), 0.0)).where(
                LedgerEntry.transaction_id == transaction.id
            )
        )
    ).scalar_one()
    if abs(float(written)) > EPSILON:
        raise LedgerInconsistent(
            f"Транзакция {transaction.id} не сходится: сумма проводок {written}"
        )
    return transaction


async def reverse_transaction(
    session: AsyncSession,
    transaction_id: uuid.UUID,
    *,
    reason: str,
    initiated_by_id: uuid.UUID | None = None,
) -> Transaction:
    """Обратная транзакция: компенсирует проводки, не удаляя историю."""
    original = (
        await session.execute(select(Transaction).where(Transaction.id == transaction_id))
    ).scalars().unique().one_or_none()
    if original is None:
        raise NotFoundError("Транзакция не найдена", code="transaction_not_found")
    if original.status is TransactionStatus.reversed:
        raise ConflictError("Транзакция уже отменена", code="transaction_already_reversed")
    if original.kind is TransactionKind.reversal:
        raise ValidationError(
            "Нельзя отменить обратную транзакцию", code="transaction_not_reversible"
        )

    entries = list(
        (
            await session.execute(
                select(LedgerEntry).where(LedgerEntry.transaction_id == original.id)
            )
        ).scalars().all()
    )
    accounts = list(
        (
            await session.execute(
                select(Account).where(Account.id.in_([entry.account_id for entry in entries]))
            )
        ).scalars().all()
    )
    by_id = {account.id: account for account in accounts}
    reversal = await post_transaction(
        session,
        kind=TransactionKind.reversal,
        amount_units=original.amount_units,
        entries=[(by_id[entry.account_id], -entry.amount_units) for entry in entries],
        description=f"Отмена: {reason}"[:280],
        task_id=original.task_id,
        agent_id=original.agent_id,
        initiated_by_id=initiated_by_id,
        idempotency_key=f"reversal:{original.id}",
        details={"reason": reason, "original_kind": original.kind.value},
        reversal_of_id=original.id,
    )
    original.status = TransactionStatus.reversed
    original.reversed_at = _now()
    await session.flush()
    return reversal


# ── Бюджеты заданий ────────────────────────────────────────────────────
async def funding_ratio() -> float:
    return float(getattr(settings, "economy_fee_ratio", DEFAULT_FEE_RATIO))


async def hold_budget(
    session: AsyncSession,
    task: Task,
    *,
    requester_id: uuid.UUID,
    budget_units: float,
) -> TaskBudget | None:
    """Замораживает бюджет задания на эскроу-счёте заказчика.

    Бюджет не обязателен: задания без бюджета исполняются как раньше, просто
    без оплаты и награды. Если бюджет указан — он должен быть покрыт.
    """
    if budget_units <= 0:
        return None
    if budget_units > 1_000_000:
        raise ValidationError("Слишком большой бюджет задания", code="budget_too_large")

    existing = (
        await session.execute(select(TaskBudget).where(TaskBudget.task_id == task.id))
    ).scalars().unique().one_or_none()
    if existing is not None:
        if abs(existing.budget_units - budget_units) > EPSILON:
            raise ConflictError(
                "Бюджет задания уже зафиксирован", code="budget_exists"
            )
        return existing

    requester = await user_account(session, requester_id)
    escrow = await escrow_account(session, task.id)
    hold = await post_transaction(
        session,
        kind=TransactionKind.hold,
        amount_units=budget_units,
        description=f"Заморозка бюджета задания {task.id}",
        task_id=task.id,
        agent_id=task.agent_id,
        initiated_by_id=requester_id,
        idempotency_key=f"hold:{task.id}",
        entries=[(requester, -budget_units), (escrow, budget_units)],
        details={"task_id": str(task.id)},
    )
    budget = TaskBudget(
        task_id=task.id,
        requester_account_id=requester.id,
        escrow_account_id=escrow.id,
        agent_account_id=(await agent_account(session, task.agent_id)).id,
        budget_units=round(budget_units, 4),
        status=HoldStatus.held,
        hold_transaction_id=hold.id,
    )
    session.add(budget)
    await session.flush()
    return budget


async def settle_task_budget(
    session: AsyncSession,
    task: Task,
    *,
    verdict: TaskVerdict | None = None,
) -> TaskBudget | None:
    """Расчёт по завершённому заданию.

    * заказчику возвращается неизрасходованная часть бюджета;
    * исполнителю выплачивается стоимость фактической работы (`cost_units`),
      ограниченная бюджетом;
    * платформа удерживает комиссию;
    * при вердикте `verified` начисляется награда (reward rule) — отдельным
      вкладом из казны, вне бюджета задания.

    Идемпотентно: повторный вызов ничего не меняет (`settle:<task_id>`).
    """
    budget = (
        await session.execute(select(TaskBudget).where(TaskBudget.task_id == task.id))
    ).scalars().unique().one_or_none()
    if budget is None:
        # Без бюджета возможна только награда за подтверждённый результат
        await grant_reward(session, task, verdict=verdict)
        return None
    if budget.status is not HoldStatus.held:
        return budget

    requester = (
        await session.execute(
            select(Account).where(Account.id == budget.requester_account_id)
        )
    ).scalars().unique().one()
    escrow = await escrow_account(session, task.id)
    agent_acc = await agent_account(session, task.agent_id)

    spent = min(float(task.cost_units or 0.0), budget.budget_units)
    ratio = await funding_ratio()
    fee = round(spent * ratio, 4)
    payout = round(spent - fee, 4)
    refund = round(budget.budget_units - spent, 4)

    entries: list[tuple[Account, float]] = []
    if payout > 0:
        entries.append((agent_acc, payout))
    if fee > 0:
        entries.append((await treasury_account(session), fee))
    if refund > 0:
        entries.append((requester, refund))
    entries.append((escrow, -round(spent, 4) - refund))

    settle = await post_transaction(
        session,
        kind=TransactionKind.payout if spent > 0 else TransactionKind.release,
        amount_units=round(spent, 4),
        entries=entries,
        description=(
            f"Расчёт по заданию {task.id}: выплата {payout}, комиссия {fee}, возврат {refund}"
        ),
        task_id=task.id,
        agent_id=task.agent_id,
        idempotency_key=f"settle:{task.id}",
        details={
            "spent": spent,
            "payout": payout,
            "fee": fee,
            "refund": refund,
            "verdict": verdict.verdict.value if verdict is not None else None,
        },
    )
    budget.spent_units = spent
    budget.fee_units = fee
    budget.released_units = refund
    budget.status = HoldStatus.settled
    budget.settle_transaction_id = settle.id
    budget.settled_at = _now()
    await session.flush()

    await grant_reward(session, task, verdict=verdict)
    return budget


async def release_task_budget(
    session: AsyncSession, task: Task, *, reason: str
) -> TaskBudget | None:
    """Возврат бюджета заказчику (отмена, истечение, провал без выплаты)."""
    budget = (
        await session.execute(select(TaskBudget).where(TaskBudget.task_id == task.id))
    ).scalars().unique().one_or_none()
    if budget is None or budget.status is not HoldStatus.held:
        return budget
    requester = (
        await session.execute(select(Account).where(Account.id == budget.requester_account_id))
    ).scalars().unique().one()
    escrow = await escrow_account(session, task.id)
    release = await post_transaction(
        session,
        kind=TransactionKind.release,
        amount_units=budget.budget_units,
        entries=[(escrow, -budget.budget_units), (requester, budget.budget_units)],
        description=f"Возврат бюджета задания {task.id}: {reason}"[:280],
        task_id=task.id,
        agent_id=task.agent_id,
        idempotency_key=f"release:{task.id}",
        details={"reason": reason},
    )
    budget.status = HoldStatus.released
    budget.released_units = budget.budget_units
    budget.settle_transaction_id = release.id
    budget.settled_at = _now()
    await session.flush()
    return budget


async def expire_budgets(session: AsyncSession, *, older_than_hours: int = 48) -> int:
    """Возвращает бюджеты заданий, которые так и не были исполнены."""
    cutoff = _now() - timedelta(hours=older_than_hours)
    rows = (
        await session.execute(
            select(TaskBudget, Task)
            .join(Task, Task.id == TaskBudget.task_id)
            .where(
                TaskBudget.status == HoldStatus.held,
                TaskBudget.created_at < cutoff,
                Task.status.in_([TaskStatus.failed, TaskStatus.cancelled]),
            )
        )
    ).all()
    count = 0
    for _budget, task in rows:
        await release_task_budget(session, task, reason="задание завершилось без выплаты")
        count += 1
    return count


# ── Награды за подтверждённый результат ────────────────────────────────
async def active_reward_rule(session: AsyncSession) -> RewardRule | None:
    return (
        await session.execute(
            select(RewardRule).where(RewardRule.is_active.is_(True)).order_by(
                RewardRule.created_at
            )
        )
    ).scalars().first()


async def grant_reward(
    session: AsyncSession,
    task: Task,
    *,
    verdict: TaskVerdict | None = None,
) -> RewardGrant | None:
    """Начисляет награду за подтверждённый результат.

    Защита от повторных начислений — уникальность `(agent_id, task_id)` плюс
    идемпотентный ключ транзакции `reward:<task_id>`.
    """
    if verdict is None:
        verdict = (
            await session.execute(
                select(TaskVerdict).where(TaskVerdict.task_id == task.id)
            )
        ).scalars().unique().one_or_none()
    if verdict is None or verdict.verdict is not Verdict.verified:
        return None

    rule = await active_reward_rule(session)
    if rule is None:
        return None

    existing = (
        await session.execute(
            select(RewardGrant).where(
                RewardGrant.agent_id == task.agent_id, RewardGrant.task_id == task.id
            )
        )
    ).scalars().unique().one_or_none()
    if existing is not None:
        return existing

    agent = (
        await session.execute(select(Agent).where(Agent.id == task.agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    if float(agent.reputation_score) < rule.min_reputation:
        return None

    # Дневной лимит начислений на агента — защита от накрутки
    day_start = _now() - timedelta(days=1)
    daily = (
        await session.execute(
            select(func.coalesce(func.sum(RewardGrant.amount_units), 0.0)).where(
                RewardGrant.agent_id == agent.id, RewardGrant.created_at >= day_start
            )
        )
    ).scalar_one()
    if float(daily) >= rule.daily_cap_units:
        logger.info("reward_daily_cap agent=%s", agent.id)
        return None

    # Бонус за подтверждённые возможности (уровни, подтверждённые этапом 5)
    verified_levels = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(AgentCapability.verified_level), 0))
                .where(AgentCapability.agent_id == agent.id)
            )
        ).scalar_one()
        or 0
    )
    base = float(rule.verified_reward_units)
    bonus = float(rule.capability_bonus_units) * verified_levels
    amount = round(min(base + bonus, rule.daily_cap_units - float(daily)), 4)
    if amount <= 0:
        return None

    treasury = await treasury_account(session)
    account = await agent_account(session, agent.id)
    transaction = await post_transaction(
        session,
        kind=TransactionKind.reward,
        amount_units=amount,
        entries=[(treasury, -amount), (account, amount)],
        description=f"Награда за подтверждённый результат задания {task.id}"[:280],
        task_id=task.id,
        agent_id=agent.id,
        idempotency_key=f"reward:{task.id}",
        details={"base": base, "bonus": bonus, "rule_id": str(rule.id)},
    )
    grant = RewardGrant(
        agent_id=agent.id,
        task_id=task.id,
        rule_id=rule.id,
        transaction_id=transaction.id,
        amount_units=amount,
        base_units=base,
        bonus_units=bonus,
        reason=f"Награда за подтверждённый результат ({verdict.verdict.value})",
        granted_at=_now(),
    )
    session.add(grant)
    await session.flush()
    return grant


# ── Переводы и вывод ───────────────────────────────────────────────────
async def transfer(
    session: AsyncSession,
    *,
    actor: User,
    from_account_id: uuid.UUID,
    to_account_id: uuid.UUID,
    amount_units: float,
    comment: str = "",
    idempotency_key: str | None = None,
) -> Transaction:
    """Перевод между своими счетами или другому участнику."""
    if amount_units <= 0:
        raise ValidationError("Сумма перевода должна быть положительной", code="transfer_invalid")
    source = (
        await session.execute(select(Account).where(Account.id == from_account_id))
    ).scalars().unique().one_or_none()
    target = (
        await session.execute(select(Account).where(Account.id == to_account_id))
    ).scalars().unique().one_or_none()
    if source is None or target is None:
        raise NotFoundError("Счёт не найден", code="account_not_found")
    if source.id == target.id:
        raise ValidationError("Нельзя переводить на тот же счёт", code="transfer_same_account")
    if source.type is AccountType.treasury and not actor.is_admin:
        raise PermissionDeniedError("Эмиссия доступна только админу", code="treasury_forbidden")
    if (
        source.type is AccountType.user
        and source.owner_id != actor.id
        and not actor.is_admin
    ):
        raise PermissionDeniedError("Чужой счёт", code="account_forbidden")
    if target.type is AccountType.treasury and not actor.is_admin:
        raise PermissionDeniedError("Перевод в казну недоступен", code="treasury_forbidden")

    return await post_transaction(
        session,
        kind=TransactionKind.transfer,
        amount_units=amount_units,
        entries=[(source, -amount_units), (target, amount_units)],
        description=comment or "Перевод между счетами",
        initiated_by_id=actor.id,
        idempotency_key=idempotency_key,
        details={"from": str(source.id), "to": str(target.id)},
    )


async def withdraw_agent_earnings(
    session: AsyncSession, *, actor: User, agent_id: uuid.UUID, amount_units: float
) -> Transaction:
    """Вывод заработка агента на счёт владельца."""
    agent = (
        await session.execute(select(Agent).where(Agent.id == agent_id))
    ).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    if agent.owner_id != actor.id and not actor.is_admin:
        raise PermissionDeniedError("Чужой агент", code="agent_forbidden")
    source = await agent_account(session, agent_id)
    target = await user_account(session, agent.owner_id)
    return await post_transaction(
        session,
        kind=TransactionKind.withdrawal,
        amount_units=amount_units,
        entries=[(source, -amount_units), (target, amount_units)],
        description=f"Вывод заработка агента {agent.name}"[:280],
        agent_id=agent.id,
        initiated_by_id=actor.id,
        idempotency_key=f"withdraw:{agent.id}:{uuid.uuid4().hex}",
    )


# ── Чтение и сверка ────────────────────────────────────────────────────
async def account_statement(
    session: AsyncSession, account: Account, *, limit: int = 50
) -> dict[str, Any]:
    entries = (
        await session.execute(
            select(LedgerEntry, Transaction)
            .join(Transaction, Transaction.id == LedgerEntry.transaction_id)
            .where(LedgerEntry.account_id == account.id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(min(limit, 200))
        )
    ).all()
    inflow = (
        await session.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_units), 0.0)).where(
                LedgerEntry.account_id == account.id, LedgerEntry.amount_units > 0
            )
        )
    ).scalar_one()
    outflow = (
        await session.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_units), 0.0)).where(
                LedgerEntry.account_id == account.id, LedgerEntry.amount_units < 0
            )
        )
    ).scalar_one()
    return {
        "account": account,
        "total_in": round(float(inflow), 4),
        "total_out": round(abs(float(outflow)), 4),
        "entries": [
            {
                "id": entry.id,
                "transaction_id": entry.transaction_id,
                "amount": entry.amount_units,
                "balance_after": entry.balance_after,
                "kind": transaction.kind.value,
                "description": transaction.description,
                "status": transaction.status.value,
                "task_id": transaction.task_id,
                "agent_id": transaction.agent_id,
                "created_at": entry.created_at,
            }
            for entry, transaction in entries
        ],
    }


async def verify_ledger(session: AsyncSession) -> dict[str, Any]:
    """Сверка: каждая транзакция сходится, кэш балансов = сумме проводок.

    Возвращает отчёт; `ok=False` означает расхождение, которое обязано быть
    исправлено обратной транзакцией, а не правкой строк.
    """
    unbalanced = (
        await session.execute(
            select(Transaction.id, func.sum(LedgerEntry.amount_units).label("total"))
            .join(LedgerEntry, LedgerEntry.transaction_id == Transaction.id)
            .group_by(Transaction.id)
            .having(func.abs(func.sum(LedgerEntry.amount_units)) > EPSILON)
        )
    ).all()

    mismatched = (
        await session.execute(
            select(
                Account.id,
                Account.balance_units,
                func.coalesce(func.sum(LedgerEntry.amount_units), 0.0).label("ledger"),
            )
            .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
            .group_by(Account.id, Account.balance_units)
            .having(
                func.abs(
                    Account.balance_units
                    - func.coalesce(func.sum(LedgerEntry.amount_units), 0.0)
                )
                > EPSILON
            )
        )
    ).all()

    # Сумма кэшей балансов обязана быть нулём: единицы только переходят
    # между счетами. Это независимая проверка двойной записи.
    total_units = (
        await session.execute(
            select(func.coalesce(func.sum(Account.balance_units), 0.0))
        )
    ).scalar_one()

    return {
        "ok": not unbalanced and not mismatched,
        "unbalanced_transactions": [
            {"transaction_id": row[0], "sum": round(float(row[1]), 6)} for row in unbalanced
        ],
        "accounts_mismatch": [
            {"account_id": row[0], "balance": float(row[1]), "ledger": float(row[2])}
            for row in mismatched
        ],
        "total_units": round(float(total_units), 4),
        "checked_at": _now(),
    }


async def economy_stats(session: AsyncSession) -> dict[str, Any]:
    by_kind = (
        await session.execute(
            select(
                Transaction.kind,
                func.count(),
                func.coalesce(func.sum(Transaction.amount_units), 0.0),
            )
            .where(Transaction.status == TransactionStatus.posted)
            .group_by(Transaction.kind)
        )
    ).all()
    accounts_by_type = (
        await session.execute(
            select(Account.type, func.count(), func.coalesce(func.sum(Account.balance_units), 0.0))
            .group_by(Account.type)
        )
    ).all()
    held = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(TaskBudget.budget_units - TaskBudget.spent_units), 0.0),
            ).where(TaskBudget.status == HoldStatus.held)
        )
    ).one()
    rewards = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(RewardGrant.amount_units), 0.0))
        )
    ).one()
    return {
        "currency": CURRENCY,
        "transactions": {
            kind.value: {"count": int(count), "volume": round(float(volume), 4)}
            for kind, count, volume in by_kind
        },
        "accounts": {
            account_type.value: {
                "count": int(count),
                "balance": round(float(balance), 4),
            }
            for account_type, count, balance in accounts_by_type
        },
        "held_budgets": {"count": int(held[0] or 0), "units": round(float(held[1] or 0.0), 4)},
        "rewards": {
            "count": int(rewards[0] or 0),
            "units": round(float(rewards[1] or 0.0), 4),
        },
    }


async def user_economy(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    account = await user_account(session, user_id)
    agent_balances = (
        await session.execute(
            select(Agent.id, Agent.name, Account.balance_units)
            .join(Account, Account.agent_id == Agent.id)
            .where(Agent.owner_id == user_id, Account.type == AccountType.agent)
        )
    ).all()
    rewards = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(RewardGrant.amount_units), 0.0))
            .join(Agent, Agent.id == RewardGrant.agent_id)
            .where(Agent.owner_id == user_id)
        )
    ).one()
    return {
        "currency": CURRENCY,
        "account_id": account.id,
        "balance_units": round(float(account.balance_units), 4),
        "agents": [
            {"agent_id": row[0], "name": row[1], "balance_units": round(float(row[2]), 4)}
            for row in agent_balances
        ],
        "rewards": {"count": int(rewards[0] or 0), "units": round(float(rewards[1] or 0.0), 4)},
    }


async def transactions_page(
    session: AsyncSession,
    *,
    actor: User,
    account_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[tuple[Transaction, float]]:
    """История операций: свои счета, либо все для админа."""
    query = select(Transaction)
    if not actor.is_admin:
        own = select(Account.id).where(
            (Account.owner_id == actor.id)
            | Account.agent_id.in_(select(Agent.id).where(Agent.owner_id == actor.id))
        )
        query = query.where(
            Transaction.id.in_(
                select(LedgerEntry.transaction_id).where(LedgerEntry.account_id.in_(own))
            )
        )
    if account_id is not None:
        query = query.where(
            Transaction.id.in_(
                select(LedgerEntry.transaction_id).where(LedgerEntry.account_id == account_id)
            )
        )
    query = query.order_by(Transaction.created_at.desc()).limit(min(limit, 200))
    rows = list((await session.execute(query)).scalars().all())
    result: list[tuple[Transaction, float]] = []
    for transaction in rows:
        signed = (
            await session.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_units), 0.0)).where(
                    LedgerEntry.transaction_id == transaction.id,
                    LedgerEntry.account_id.in_(
                        select(Account.id).where(
                            (Account.owner_id == actor.id)
                            | Account.agent_id.in_(
                                select(Agent.id).where(Agent.owner_id == actor.id)
                            )
                        )
                    ),
                )
            )
        ).scalar_one()
        result.append((transaction, round(float(signed), 4)))
    return result


__all__ = [
    "CURRENCY",
    "DEFAULT_FEE_RATIO",
    "SIGNUP_GRANT_UNITS",
    "LedgerInconsistent",
    "account_statement",
    "active_reward_rule",
    "agent_account",
    "economy_stats",
    "ensure_signup_grant",
    "escrow_account",
    "expire_budgets",
    "get_or_create_account",
    "grant_reward",
    "hold_budget",
    "post_transaction",
    "release_task_budget",
    "reverse_transaction",
    "settle_task_budget",
    "transactions_page",
    "transfer",
    "treasury_account",
    "user_account",
    "user_economy",
    "verify_ledger",
    "withdraw_agent_earnings",
]
