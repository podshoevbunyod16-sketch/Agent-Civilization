"""default reward rule

Revision ID: 9f6a22c3adac
Revises: 3c59ebe89031
Create Date: 2026-11-05 20:32:19.699983+00:00

Базовое правило наград — данные, без которых начисления этапа 6 не работают.
Ставки консервативные: 2 ACU за подтверждённый результат, бонус 0.5 за
единицу подтверждённого уровня возможности, комиссия платформы 5%, дневной
лимит 1000 ACU на агента (защита от накрутки).
"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9f6a22c3adac"
down_revision: Union[str, None] = "3c59ebe89031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    reward_rules = sa.table(
        "reward_rules",
        sa.column("id", sa.UUID()),
        sa.column("name", sa.String()),
        sa.column("verified_reward_units", sa.Float()),
        sa.column("capability_bonus_units", sa.Float()),
        sa.column("platform_fee_ratio", sa.Float()),
        sa.column("min_reputation", sa.Float()),
        sa.column("daily_cap_units", sa.Float()),
        sa.column("is_active", sa.Boolean()),
        sa.column("details", postgresql.JSONB()),
    )
    op.bulk_insert(
        reward_rules,
        [
            {
                "id": str(uuid.uuid4()),
                "name": "default",
                "verified_reward_units": 2.0,
                "capability_bonus_units": 0.5,
                "platform_fee_ratio": 0.05,
                "min_reputation": 0.0,
                "daily_cap_units": 1000.0,
                "is_active": True,
                "details": {"note": "базовое правило этапа 6"},
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM reward_rules WHERE name = 'default'")
