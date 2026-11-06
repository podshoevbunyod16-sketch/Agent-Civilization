"""Схемы API внутренней экономики (этап 6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.economy import AccountStatus, AccountType, HoldStatus, TransactionKind


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: AccountType
    status: AccountStatus
    owner_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    currency: str
    balance_units: float
    credit_limit: float
    label: str
    created_at: datetime


class LedgerEntryRead(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    amount: float
    balance_after: float
    kind: str
    description: str
    status: str
    task_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    created_at: datetime


class AccountStatement(BaseModel):
    account: AccountRead
    total_in: float
    total_out: float
    entries: list[LedgerEntryRead]


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: TransactionKind
    status: str
    amount_units: float
    currency: str
    description: str
    task_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    idempotency_key: str | None
    details: dict[str, Any]
    posted_at: datetime | None
    reversed_at: datetime | None
    created_at: datetime


class TransactionWithImpact(BaseModel):
    """"Операция + влияние на счета пользователя (знак)."""

    transaction: TransactionRead
    impact: float


class TransferRequest(BaseModel):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    # Деньги принимаются как Decimal и проверяются на точность: 0.1+0.2
    # в float даёт 0.30000000000000004, и такие «хвосты» нельзя пускать в
    # проводки. На границе API сумма конвертируется один раз, дальше в
    # сервисе используется Decimal с 4 знаками (этап 10, E).
    amount_units: Decimal = Field(gt=0, le=1_000_000, decimal_places=4)

    @field_validator("amount_units")
    @classmethod
    def _precise(cls, value: Decimal) -> Decimal:
        quantized = value.quantize(Decimal("0.0001"))
        if quantized != value:
            raise ValueError("Сумма поддерживает не более 4 знаков после запятой")
        return quantized

    comment: str = Field(default="", max_length=280)
    idempotency_key: str | None = Field(default=None, max_length=160)


class WithdrawRequest(BaseModel):
    agent_id: uuid.UUID
    amount_units: Decimal = Field(gt=0, le=1_000_000, decimal_places=4)


class BudgetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    budget_units: float
    spent_units: float
    fee_units: float
    released_units: float
    status: HoldStatus
    settled_at: datetime | None
    created_at: datetime


class RewardRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    verified_reward_units: float
    capability_bonus_units: float
    platform_fee_ratio: float
    min_reputation: float
    daily_cap_units: float
    is_active: bool


class RewardRuleUpdate(BaseModel):
    verified_reward_units: float | None = Field(default=None, ge=0, le=10_000)
    capability_bonus_units: float | None = Field(default=None, ge=0, le=10_000)
    platform_fee_ratio: float | None = Field(default=None, ge=0, le=1)
    min_reputation: float | None = Field(default=None, ge=0, le=100)
    daily_cap_units: float | None = Field(default=None, ge=0, le=1_000_000)
    is_active: bool | None = None


class RewardGrantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    task_id: uuid.UUID
    amount_units: float
    base_units: float
    bonus_units: float
    reason: str
    granted_at: datetime | None
    created_at: datetime


class MoneyRead(BaseModel):
    """Денежный отчёт с балансами."""

    currency: str
    account_id: uuid.UUID | None = None
    balance_units: float
    agents: list[dict[str, Any]] = Field(default_factory=list)
    rewards: dict[str, Any] = Field(default_factory=dict)


class LedgerVerification(BaseModel):
    ok: bool
    unbalanced_transactions: list[dict[str, Any]]
    accounts_mismatch: list[dict[str, Any]]
    total_units: float
    checked_at: datetime


class EconomyStats(BaseModel):
    currency: str
    transactions: dict[str, dict[str, float]]
    accounts: dict[str, dict[str, float]]
    held_budgets: dict[str, float]
    rewards: dict[str, float]


__all__ = [
    "AccountRead",
    "AccountStatement",
    "BudgetRead",
    "EconomyStats",
    "LedgerEntryRead",
    "LedgerVerification",
    "MoneyRead",
    "RewardGrantRead",
    "RewardRuleRead",
    "RewardRuleUpdate",
    "TransactionRead",
    "TransactionWithImpact",
    "TransferRequest",
    "WithdrawRequest",
]
