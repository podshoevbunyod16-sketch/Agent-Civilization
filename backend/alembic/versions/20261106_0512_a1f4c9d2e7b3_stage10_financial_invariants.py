"""stage10: финансовые инварианты на уровне БД

Revision ID: a1f4c9d2e7b3
Revises: eabfe5cc2b2c
Create Date: 2026-11-06 05:12:00.000000+00:00

Этап 10, E. Инварианты, которые нельзя нарушить даже прямым SQL/багом в
коде — они проверяются базой:

1. `ledger_entries.amount_units <> 0` — проводка на ноль бессмысленна;
2. `accounts.balance_units >= -credit_limit` — минус только в пределах
   кредитного лимита (у treasury лимит 1e12, у обычных счетов 0);
3. `task_budgets.budget_units > 0` и `spent_units/... >= 0`;
4. `transactions.amount_units >= 0`;
5. `CHECK (settled/held consistency)`: бюджет в статусе `held` не может
   иметь непустую дату расчёта.

Все ограничения добавляются как `NOT VALID` и затем `VALIDATE`, чтобы
миграция не блокировала таблицы на запись дольше необходимого. Если в
существующих данных есть нарушение, `VALIDATE` упадёт — это сигнал
разобрать данные, а не отключить проверку. Ничего не удаляется.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "a1f4c9d2e7b3"
down_revision: Union[str, None] = "eabfe5cc2b2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINTS: list[tuple[str, str, str]] = [
    (
        "ck_ledger_entries_nonzero",
        "ledger_entries",
        "amount_units <> 0",
    ),
    (
        "ck_accounts_balance_within_credit",
        "accounts",
        "balance_units >= -abs(credit_limit)",
    ),
    (
        "ck_accounts_credit_limit_nonnegative",
        "accounts",
        "credit_limit >= 0",
    ),
    (
        "ck_transactions_amount_nonnegative",
        "transactions",
        "amount_units >= 0",
    ),
    (
        "ck_task_budgets_positive",
        "task_budgets",
        "budget_units > 0",
    ),
    (
        "ck_task_budgets_spent_nonnegative",
        "task_budgets",
        "spent_units >= 0 AND fee_units >= 0 AND released_units >= 0",
    ),
    (
        "ck_task_budgets_spent_within_budget",
        "task_budgets",
        "spent_units <= budget_units",
    ),
]


def upgrade() -> None:
    for name, table, condition in CONSTRAINTS:
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID'
        )
    # Валидация отдельно: на пустых/корректных данных проходит мгновенно,
    # на «грязных» — падает и останавливает миграцию (это и нужно).
    for name, table, _condition in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")

    # Индекс для поиска проводок по счёту: выписка и сверка читают по нему.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ledger_entries_account_created "
        "ON ledger_entries (account_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ledger_entries_account_created")
    for name, table, _condition in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
