"""stage10 event outbox

Revision ID: eabfe5cc2b2c
Revises: e2ceb3bb5364
Create Date: 2026-11-06 04:24:42.319179+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'eabfe5cc2b2c'
down_revision: Union[str, None] = 'e2ceb3bb5364'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Таблица outbox (этап 10, D).

    Только добавление: существующие данные и индексы не затрагиваются.
    Индексы подобраны под реальные запросы отправителя:
    `(status, next_attempt_at)` — выборка пачки, `(topic, created_at)` —
    диагностика по конкретному каналу.
    """
    op.create_table(
        "event_outbox",
        sa.Column("topic", sa.String(length=120), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sending",
                "published",
                "failed",
                name="outbox_status",
                native_enum=False,
                length=20,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")),
    )
    op.create_index(
        op.f("ix_event_outbox_event_id"), "event_outbox", ["event_id"], unique=False
    )
    op.create_index(
        "ix_event_outbox_pending",
        "event_outbox",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_event_outbox_topic", "event_outbox", ["topic", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_event_outbox_topic", table_name="event_outbox")
    op.drop_index("ix_event_outbox_pending", table_name="event_outbox")
    op.drop_index(op.f("ix_event_outbox_event_id"), table_name="event_outbox")
    op.drop_table("event_outbox")
