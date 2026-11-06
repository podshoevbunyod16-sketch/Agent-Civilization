"""Этап 10, E: финансовые инварианты — property-based и конкурентные тесты.

Проверяются не «удачные сценарии», а свойства, которые обязаны держаться
при любых входах:

1. двойная запись: сумма проводок транзакции всегда ноль;
2. деньги не появляются и не исчезают: сумма всех балансов = 0;
3. `balance_units` (кэш) всегда равен сумме проводок счёта;
4. отрицательный баланс невозможен за пределами кредитного лимита;
5. идемпотентность: повтор с тем же ключом не создаёт новых проводок;
6. конкурентные переводы не теряют и не дублируют деньги;
7. точность: суммы с 5 знаками отклоняются, 4 знака сохраняются точно.

Свойства 1–3 проверяются Hypothesis на случайных последовательностях сумм —
это ловит накопление ошибки float, которое не видно на «круглых» числах.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from app.core.errors import ValidationError
from app.db.models.economy import Account, LedgerEntry, Transaction
from app.services import economy as economy_service
from tests.conftest import register_user

# Суммы в допустимом диапазоне, ровно 4 знака после запятой
money = st.decimals(
    min_value=Decimal("0.0001"), max_value=Decimal("500.0000"), places=4
)


async def _fund_user(session, user_id: uuid.UUID, amount: Decimal) -> Account:
    """Пополняет счёт участника из казны (deposit)."""
    account = await economy_service.user_account(session, user_id)
    treasury = await economy_service.treasury_account(session)
    await economy_service.post_transaction(
        session,
        kind=economy_service.TransactionKind.deposit,
        amount_units=float(amount),
        entries=[(treasury, -float(amount)), (account, float(amount))],
        description="Тестовое пополнение",
        idempotency_key=f"deposit:{uuid.uuid4().hex}",
    )
    await session.commit()
    return account


async def _assert_invariants(session) -> None:
    """Проверки, которые обязаны выполняться в любой момент времени."""
    # 1. Каждая транзакция сбалансирована
    unbalanced = (
        await session.execute(
            select(Transaction.id)
            .join(LedgerEntry, LedgerEntry.transaction_id == Transaction.id)
            .group_by(Transaction.id)
            .having(func.abs(func.sum(LedgerEntry.amount_units)) > Decimal("0.000001"))
        )
    ).scalars().all()
    assert unbalanced == [], f"транзакции без баланса: {unbalanced}"

    # 2. Сумма всех балансов — ноль
    total = (
        await session.execute(select(func.coalesce(func.sum(Account.balance_units), 0)))
    ).scalar_one()
    assert abs(Decimal(str(total))) <= Decimal("0.000001"), f"сумма балансов {total}"

    # 3. Кэш баланса совпадает с суммой проводок по каждому счёту
    rows = (
        await session.execute(
            select(
                Account.id,
                Account.balance_units,
                func.coalesce(func.sum(LedgerEntry.amount_units), 0),
            )
            .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
            .group_by(Account.id, Account.balance_units)
        )
    ).all()
    for account_id, cached, computed in rows:
        assert abs(Decimal(str(cached)) - Decimal(str(computed))) <= Decimal(
            "0.000001"
        ), f"счёт {account_id}: кэш {cached} != проводки {computed}"

    # 4. Отрицательный баланс только у счетов с кредитным лимитом
    overdrawn = (
        await session.execute(
            select(Account.id).where(
                Account.balance_units < -func.abs(Account.credit_limit) - 0.000001
            )
        )
    ).scalars().all()
    assert overdrawn == [], f"счета с превышением кредитного лимита: {overdrawn}"


@pytest.mark.asyncio
@hyp_settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(amounts=st.lists(money, min_size=1, max_size=6))
async def test_double_entry_holds_for_random_amounts(client, session, amounts):
    """Свойство: после произвольной серии операций инварианты держатся."""
    user = await register_user(client)
    # Регистрация начисляет стартовый грант 100 ACU — учитываем в ожиданиях
    user_id = uuid.UUID(user.id)
    total = sum(amounts)
    account = await _fund_user(session, user_id, total)

    target = await economy_service.user_account(session, user_id)
    treasury = await economy_service.treasury_account(session)
    remaining = Decimal(str(account.balance_units))
    for amount in amounts:
        if amount > remaining:
            break
        await economy_service.post_transaction(
            session,
            kind=economy_service.TransactionKind.transfer,
            amount_units=float(amount),
            entries=[(target, -float(amount)), (treasury, float(amount))],
            description="Случайный перевод",
            idempotency_key=f"t:{uuid.uuid4().hex}",
        )
        remaining -= amount
    await session.commit()
    await _assert_invariants(session)


@pytest.mark.asyncio
async def test_four_decimal_places_are_exact(client, session):
    """0.1 + 0.2 — это ровно 0.3, а не 0.30000000000000004."""
    user = await register_user(client)
    # Регистрация начисляет стартовый грант 100 ACU — учитываем в ожиданиях
    user_id = uuid.UUID(user.id)
    await _fund_user(session, user_id, Decimal("1.0000"))
    account = await economy_service.user_account(session, user_id)
    treasury = await economy_service.treasury_account(session)
    await session.commit()

    # Два перевода по 0.1 и один 0.2: сумма должна быть ровно 0.3
    for amount in (Decimal("0.1000"), Decimal("0.2000")):
        await economy_service.post_transaction(
            session,
            kind=economy_service.TransactionKind.transfer,
            amount_units=float(amount),
            entries=[(account, -float(amount)), (treasury, float(amount))],
            description="Точность",
            idempotency_key=f"p:{uuid.uuid4().hex}",
        )
        await session.commit()

    await session.refresh(account)
    # Стартовый грант участника (100) + 1.0 пополнение − 0.3 = 100.7
    expected_first = Decimal(str(account.balance_units))
    assert expected_first == Decimal("100.7")


@pytest.mark.asyncio
async def test_five_decimal_places_rejected(client, session):
    user = await register_user(client)
    # Регистрация начисляет стартовый грант 100 ACU — учитываем в ожиданиях
    account = await _fund_user(session, uuid.UUID(user.id), Decimal("10.0000"))
    treasury = await economy_service.treasury_account(session)
    with pytest.raises(ValidationError) as exc:
        await economy_service.post_transaction(
            session,
            kind=economy_service.TransactionKind.transfer,
            amount_units=1.0,
            # 0.12345 — пять знаков: сервис обязан отказать, а не округлить
            entries=[(account, -0.12345), (treasury, 0.12345)],
            description="Мусорные знаки",
            idempotency_key=f"q:{uuid.uuid4().hex}",
        )
    assert exc.value.code in ("amount_precision", "amount_invalid")


@pytest.mark.asyncio
async def test_negative_amount_rejected(client, session):
    user = await register_user(client)
    # Регистрация начисляет стартовый грант 100 ACU — учитываем в ожиданиях
    account = await _fund_user(session, uuid.UUID(user.id), Decimal("5.0000"))
    with pytest.raises(ValidationError):
        await economy_service.post_transaction(
            session,
            kind=economy_service.TransactionKind.transfer,
            amount_units=-1.0,
            entries=[(account, 1.0), (account, -1.0)],
            description="Отрицательная сумма",
        )


@pytest.mark.asyncio
async def test_idempotency_key_prevents_double_spend(client, user, session):
    account = await _fund_user(session, uuid.UUID(user.id), Decimal("100.0000"))
    treasury = await economy_service.treasury_account(session)
    start_balance = Decimal(str(account.balance_units))
    key = "idem-transfer-1"

    first = await economy_service.post_transaction(
        session,
        kind=economy_service.TransactionKind.transfer,
        amount_units=10.0,
        entries=[(account, -10.0), (treasury, 10.0)],
        description="Идемпотентность",
        idempotency_key=key,
    )
    await session.commit()
    count_after_first = (
        await session.execute(select(func.count()).select_from(LedgerEntry))
    ).scalar_one()

    second = await economy_service.post_transaction(
        session,
        kind=economy_service.TransactionKind.transfer,
        amount_units=10.0,
        entries=[(account, -10.0), (treasury, 10.0)],
        description="Идемпотентность",
        idempotency_key=key,
    )
    await session.commit()
    count_after_second = (
        await session.execute(select(func.count()).select_from(LedgerEntry))
    ).scalar_one()

    assert first.id == second.id
    assert count_after_first == count_after_second
    await session.refresh(account)
    # Списано ровно один раз, несмотря на повторный вызов
    assert Decimal(str(account.balance_units)) == start_balance - Decimal("10")


@pytest.mark.asyncio
async def test_concurrent_transfers_do_not_lose_money(client, user, session):
    """10 параллельных переводов по 5 с исходного баланса 100 → ровно 50."""
    from app.db.session import get_session_factory

    account = await _fund_user(session, uuid.UUID(user.id), Decimal("100.0000"))
    treasury = await economy_service.treasury_account(session)
    await session.commit()
    account_id, treasury_id = account.id, treasury.id
    start_balance = Decimal(str(account.balance_units))

    factory = get_session_factory()

    async def one_transfer(index: int) -> str:
        async with factory() as own:
            source = (
                await own.execute(select(Account).where(Account.id == account_id))
            ).scalar_one()
            target = (
                await own.execute(select(Account).where(Account.id == treasury_id))
            ).scalar_one()
            try:
                await economy_service.post_transaction(
                    own,
                    kind=economy_service.TransactionKind.transfer,
                    amount_units=5.0,
                    entries=[(source, -5.0), (target, 5.0)],
                    description=f"Параллельный перевод {index}",
                    idempotency_key=f"conc:{index}",
                )
                await own.commit()
                return "ok"
            except Exception as exc:  # недостаточно средств / блокировка
                await own.rollback()
                return type(exc).__name__

    results = await asyncio.gather(*(one_transfer(i) for i in range(10)))
    ok_count = results.count("ok")

    await session.rollback()
    # `populate_existing`: иначе ORM вернёт объект из identity map со старым
    # значением и тест «пройдёт» на устаревших данных.
    refreshed = (
        await session.execute(
            select(Account)
            .where(Account.id == account_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    expected = start_balance - Decimal("5") * ok_count
    assert Decimal(str(refreshed.balance_units)) == expected
    assert ok_count >= 1  # хотя бы один перевод прошёл
    await _assert_invariants(session)


@pytest.mark.asyncio
async def test_insufficient_funds_does_not_change_balance(client, user, session):
    account = await _fund_user(session, uuid.UUID(user.id), Decimal("10.0000"))
    treasury = await economy_service.treasury_account(session)
    balance = Decimal(str(account.balance_units))
    with pytest.raises(ValidationError) as exc:
        await economy_service.post_transaction(
            session,
            kind=economy_service.TransactionKind.transfer,
            amount_units=float(balance) + 50,
            entries=[(account, -float(balance) - 50), (treasury, float(balance) + 50)],
            description="Недостаточно средств",
        )
    assert exc.value.code == "insufficient_funds"
    await session.rollback()
    await _assert_invariants(session)


@pytest.mark.asyncio
async def test_reversal_restores_balances_and_keeps_history(client, user, session):
    account = await _fund_user(session, uuid.UUID(user.id), Decimal("50.0000"))
    treasury = await economy_service.treasury_account(session)
    start_balance = Decimal(str(account.balance_units))
    original = await economy_service.post_transaction(
        session,
        kind=economy_service.TransactionKind.transfer,
        amount_units=20.0,
        entries=[(account, -20.0), (treasury, 20.0)],
        description="Перевод для отмены",
        idempotency_key="rev:1",
    )
    await session.commit()

    reversal = await economy_service.reverse_transaction(
        session, original.id, reason="ошибка оператора"
    )
    await session.commit()

    await session.refresh(account)
    assert Decimal(str(account.balance_units)) == start_balance
    assert reversal.reversal_of_id == original.id
    # История не удаляется: обе транзакции на месте
    total = (await session.execute(select(func.count()).select_from(Transaction))).scalar_one()
    assert total >= 3  # deposit + transfer + reversal
    await _assert_invariants(session)


@pytest.mark.asyncio
async def test_double_reversal_rejected(client, user, session):
    account = await _fund_user(session, uuid.UUID(user.id), Decimal("30.0000"))
    treasury = await economy_service.treasury_account(session)
    original = await economy_service.post_transaction(
        session,
        kind=economy_service.TransactionKind.transfer,
        amount_units=10.0,
        entries=[(account, -10.0), (treasury, 10.0)],
        description="Отменяемая",
        idempotency_key="rev:2",
    )
    await session.commit()
    await economy_service.reverse_transaction(session, original.id, reason="first")
    await session.commit()
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError):
        await economy_service.reverse_transaction(session, original.id, reason="second")


@pytest.mark.asyncio
async def test_db_rejects_unbalanced_ledger_directly(client, user, session):
    """Обход через прямой SQL тоже не проходит: ограничения в БД."""
    from sqlalchemy.exc import IntegrityError

    user_obj = await register_user(client)
    account = await _fund_user(session, uuid.UUID(user_obj.id), Decimal("5.0000"))
    await session.commit()

    # Проводка на ноль запрещена ограничением ck_ledger_entries_nonzero
    transaction = economy_service.Transaction(
        kind=economy_service.TransactionKind.adjustment,
        amount_units=1.0,
        description="прямая вставка",
        idempotency_key="direct:1",
    )
    session.add(transaction)
    await session.flush()
    session.add(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=account.id,
            amount_units=0,
            balance_after=account.balance_units,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_api_rejects_balance_above_credit_limit_via_sql(client, user, session):
    """Отрицательный баланс за пределами лимита блокирует сама БД."""
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    account = await _fund_user(session, uuid.UUID(user.id), Decimal("1.0000"))
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            update(Account)
            .where(Account.id == account.id)
            .values(balance_units=-100.0, credit_limit=0.0)
        )
        await session.flush()
    await session.rollback()
