"""scaling indexes for queue and ledger

Индексы этапа 9. Каждый закрывает конкретный запрос из кода, а не «на всякий
случай»:

1. `ix_tasks_lease_expiry` — демон обслуживания ищет истёкшие аренды
   (`status IN (leased, running) AND lease_expires_at < now()`) каждые 30 с.
   Без индекса это seq scan по всей таблице заданий на каждом проходе.
2. `ix_tasks_deadline` — проверка просроченных дедлайнов в метриках.
3. `ix_transactions_kind_created` — сводка `/economy/stats` и история операций
   по типу за период.
4. `ix_reward_grants_agent_created` — дневной лимит наград: сумма наград
   агента за последние сутки.
5. `ix_compat_agent_negative_created` — «последняя проверка» и «последний
   негативный прогон» в Developer Portal (частые чтения со страницы).

Частичные индексы там, где выборка узкая (`is_negative = false` / `true`):
они меньше и точнее полноценных.

Revision ID: 75f08bc270fd
Revises: 025c3d3eb4bd
Create Date: 2026-11-05 21:12:22.423442+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '75f08bc270fd'
down_revision: Union[str, None] = '025c3d3eb4bd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_tasks_lease_expiry",
        "tasks",
        ["status", "lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("lease_expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_tasks_deadline",
        "tasks",
        ["deadline_at"],
        unique=False,
        postgresql_where=sa.text("deadline_at IS NOT NULL"),
    )
    op.create_index(
        "ix_transactions_kind_created",
        "transactions",
        ["kind", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_reward_grants_agent_created",
        "reward_grants",
        ["agent_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_compat_agent_negative_created",
        "compatibility_checks",
        ["agent_id", "is_negative", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_compat_agent_negative_created", table_name="compatibility_checks")
    op.drop_index("ix_reward_grants_agent_created", table_name="reward_grants")
    op.drop_index("ix_transactions_kind_created", table_name="transactions")
    op.drop_index("ix_tasks_deadline", table_name="tasks")
    op.drop_index("ix_tasks_lease_expiry", table_name="tasks")
