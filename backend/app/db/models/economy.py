"""Модели внутренней экономики (этап 6).

Расчётные единицы (**ACU**, Agent Civilization Units) — не криптовалюта и не
деньги: это внутренний учёт вклада и стоимости. Инварианты, которые держит
схема:

1. **Двойная запись.** Каждая транзакция — это ≥ 2 проводки (`ledger_entries`),
   сумма которых по одной транзакции равна нулю (`CHECK` на уровне БД не
   выразить, поэтому инвариант проверяется сервисом и тестами). Из баланса
   нельзя «напечатать» единицы: они всегда переходят между счетами.
2. **Неизменяемость.** Проводки append-only: `UPDATE`/`DELETE` не
   выполняются никогда. Исправление — обратная транзакция (`reversal_of_id`).
3. **Идемпотентность.** `transactions.idempotency_key` уникален: повторный
   отчёт узла с тем же ключом возвращает существующую транзакцию, а не
   создаёт второе начисление.
4. **Баланс выводится из проводок.** `accounts.balance_units` — кэш для
   быстрых ответов; источник истины — `SUM(ledger_entries.amount_units)`.

Типы счетов:

* `user` — бюджет заказчика (единицы кладутся при регистрации/пополнении);
* `agent` — заработок агента (владелец может вывести в свой счёт);
* `treasury` — эмиссионный счёт платформы: единственный, кому разрешено
  уходить в минус, и единственный источник новых единиц;
* `escrow` — заблокированный бюджет задания (замораживается при постановке
  в очередь, распределяется при завершении).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class AccountType(enum.StrEnum):
    user = "user"
    agent = "agent"
    treasury = "treasury"
    escrow = "escrow"


class AccountStatus(enum.StrEnum):
    active = "active"
    frozen = "frozen"
    closed = "closed"


class TransactionKind(enum.StrEnum):
    """Тип транзакции. Задаёт смысл проводок, а не их форму."""

    deposit = "deposit"            # treasury → user (пополнение бюджета)
    hold = "hold"                  # user → escrow (заморозка бюджета задания)
    release = "release"            # escrow → user (возврат неиспользованного)
    payout = "payout"              # escrow → agent (оплата за работу)
    fee = "fee"                    # escrow → treasury (комиссия платформы)
    reward = "reward"              # treasury → agent (награда за подтверждённое)
    transfer = "transfer"          # user/agent → user (перевод между счетами)
    withdrawal = "withdrawal"      # agent → user (вывод заработка владельцу)
    reversal = "reversal"          # обратная транзакция
    adjustment = "adjustment"      # ручная корректировка админом


class TransactionStatus(enum.StrEnum):
    posted = "posted"
    reversed = "reversed"


class HoldStatus(enum.StrEnum):
    """Состояние бюджета задания."""

    held = "held"
    settled = "settled"    # выплачено исполнителю (+ комиссия)
    released = "released"  # возвращено заказчику (в т.ч. по истечении)


class Account(BaseModel):
    """Счёт участника. Баланс — кэш, истина в проводках."""

    __tablename__ = "accounts"

    type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type", native_enum=False, length=20),
        nullable=False,
        index=True,
    )
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name="account_status", native_enum=False, length=20),
        default=AccountStatus.active,
        nullable=False,
    )
    # Владелец: user_id для user/escrow, agent_id для agent, оба NULL для treasury
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    currency: Mapped[str] = mapped_column(String(8), default="ACU", nullable=False)
    balance_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.00"),
        nullable=False,
    )
    # Разрешённый минус (только treasury)
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.00"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    __table_args__ = (
        Index("ix_accounts_type_owner", "type", "owner_id"),
        # Один счёт на владельца в валюте — частичный индекс, чтобы не
        # мешать нескольким treasury-счетам (у них owner_id = NULL)
        Index(
            "uq_accounts_user_currency",
            "owner_id",
            "currency",
            unique=True,
            postgresql_where=text("owner_id IS NOT NULL"),
        ),
        CheckConstraint(
            "type <> 'treasury' OR owner_id IS NULL", name="treasury_without_owner"
        ),
        CheckConstraint(
            "type <> 'agent' OR agent_id IS NOT NULL", name="agent_account_has_agent"
        ),
    )


class Transaction(BaseModel):
    """Транзакция: заголовок группы проводок (двойная запись)."""

    __tablename__ = "transactions"

    kind: Mapped[TransactionKind] = mapped_column(
        Enum(TransactionKind, name="transaction_kind", native_enum=False, length=20),
        nullable=False,
        index=True,
    )
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status", native_enum=False, length=20),
        default=TransactionStatus.posted,
        nullable=False,
    )
    amount_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="ACU", nullable=False)
    description: Mapped[str] = mapped_column(String(280), default="", nullable=False)

    # Связи с доменом
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    initiated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )

    # Идемпотентность финансовой операции
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_transactions_idempotency", "idempotency_key", unique=True),
        CheckConstraint("amount_units >= 0", name="transaction_amount_non_negative"),
    )


class LedgerEntry(BaseModel):
    """Проводка: изменение одного счёта. Append-only.

    Сумма `amount_units` по всем проводкам транзакции = 0 — это и есть
    двойная запись. Отрицательное значение — списание со счёта.
    """

    __tablename__ = "ledger_entries"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    amount_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    # Баланс счёта после проводки — снимок для аудита и проверки сходимости
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    note: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    __table_args__ = (Index("ix_ledger_account_created", "account_id", "created_at"),)


class TaskBudget(BaseModel):
    """Бюджет задания: заморозка → расчёт с исполнителем."""

    __tablename__ = "task_budgets"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), unique=True
    )
    requester_account_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    escrow_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    agent_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )

    budget_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    spent_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.00"),
        nullable=False,
    )
    fee_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.00"),
        nullable=False,
    )
    released_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.00"),
        nullable=False,
    )

    status: Mapped[HoldStatus] = mapped_column(
        Enum(HoldStatus, name="hold_status", native_enum=False, length=20),
        default=HoldStatus.held,
        nullable=False,
        index=True,
    )
    hold_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )
    settle_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("budget_units >= 0", name="budget_non_negative"),
        CheckConstraint("spent_units >= 0", name="budget_spent_non_negative"),
    )


class RewardRule(BaseModel):
    """Правило награды за подтверждённый результат (этап 5 → 6)."""

    __tablename__ = "reward_rules"

    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    # Награда за результат с вердиктом verified
    verified_reward_units: Mapped[Decimal] = mapped_column(
            Numeric(18, 4), default=Decimal("2.00"), nullable=False
        )
    # Множитель за каждую подтверждённую возможность (уровень × множитель)
    capability_bonus_units: Mapped[Decimal] = mapped_column(
            Numeric(18, 4), default=Decimal("0.50"), nullable=False
        )
    # Доля платформы от оплаты задания
    platform_fee_ratio: Mapped[float] = mapped_column(Float, default=0.05, nullable=False)
    # Порог репутации, ниже которого награда не начисляется
    min_reputation: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    daily_cap_units: Mapped[Decimal] = mapped_column(
            Numeric(18, 4), default=Decimal("1000.00"), nullable=False
        )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "platform_fee_ratio >= 0 AND platform_fee_ratio <= 1", name="fee_ratio_range"
        ),
        CheckConstraint(
            "verified_reward_units >= 0", name="reward_non_negative"
        ),
    )


class RewardGrant(BaseModel):
    """Начисление награды: защита от повторной выплаты за один результат."""

    __tablename__ = "reward_grants"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("reward_rules.id", ondelete="SET NULL")
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )
    amount_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    base_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.00"),
        nullable=False,
    )
    bonus_units: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0.00"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Один результат — одна награда, гарантия на уровне БД
        UniqueConstraint("agent_id", "task_id", name="uq_reward_agent_task"),
        CheckConstraint("amount_units >= 0", name="grant_non_negative"),
    )


__all__ = [
    "Account",
    "AccountStatus",
    "AccountType",
    "HoldStatus",
    "LedgerEntry",
    "RewardGrant",
    "RewardRule",
    "TaskBudget",
    "Transaction",
    "TransactionKind",
    "TransactionStatus",
]
