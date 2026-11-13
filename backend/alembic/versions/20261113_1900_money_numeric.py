"""Money columns Float -> Numeric(18, 4) (exact decimal ledger).

Revision ID: b7c3d91e5a02
Revises: 9a2d6f4b1c80
Create Date: 2026-11-13 19:00:00.000000

Двойная запись больше не зависит от двоичного представления float:
суммы проводок и балансы хранятся в точных decimal-числах с 4 знаками.
Существующие значения конвертируются через round(x::numeric, 4).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c3d91e5a02"
down_revision: str | None = "9a2d6f4b1c80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY_COLUMNS: list[tuple[str, str]] = [
    ("accounts", "balance_units"),
    ("accounts", "credit_limit"),
    ("transactions", "amount_units"),
    ("ledger_entries", "amount_units"),
    ("ledger_entries", "balance_after"),
    ("task_budgets", "budget_units"),
    ("task_budgets", "spent_units"),
    ("task_budgets", "fee_units"),
    ("task_budgets", "released_units"),
    ("reward_rules", "verified_reward_units"),
    ("reward_rules", "capability_bonus_units"),
    ("reward_rules", "daily_cap_units"),
    ("reward_grants", "amount_units"),
    ("reward_grants", "base_units"),
    ("reward_grants", "bonus_units"),
]


def upgrade() -> None:
    for table, column in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Numeric(18, 4),
            existing_type=sa.Float(),
            existing_nullable=False,
            postgresql_using=f"round({column}::numeric, 4)",
        )


def downgrade() -> None:
    for table, column in MONEY_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Float(),
            existing_type=sa.Numeric(18, 4),
            existing_nullable=False,
            postgresql_using=f"{column}::double precision",
        )
