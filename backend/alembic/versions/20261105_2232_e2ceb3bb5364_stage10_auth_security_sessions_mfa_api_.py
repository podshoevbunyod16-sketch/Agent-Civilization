"""stage10: auth security — sessions, 2FA, login attempts, one-time tokens, API keys

Revision ID: e2ceb3bb5364
Revises: 75f08bc270fd
Create Date: 2026-11-05 22:32:13.965385+00:00

Что меняется (этап 10, B и C):

* `users`: TOTP-секрет (только шифротекст AES-GCM), коды восстановления
  (хеши), счётчик неудачных входов и время блокировки.
* `refresh_token_sessions`: `session_id` (привязка access-токена к серверной
  сессии), `last_used_at`, `revoked_reason`.
* новые таблицы: `login_attempts`, `one_time_tokens`, `api_keys`.
* `mcp_servers.auth_token`: 500 → 2000 символов — теперь там шифротекст.

Миграция аддитивная: ни одна колонка не удаляется и не переименовывается,
данные не переписываются. Поля NOT NULL добавляются с server_default, чтобы
`alembic upgrade` проходил на непустой таблице `users`.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2ceb3bb5364"
down_revision: Union[str, None] = "75f08bc270fd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("identifier", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_attempts")),
    )
    op.create_index(
        op.f("ix_login_attempts_created_at"), "login_attempts", ["created_at"], unique=False
    )
    op.create_index(
        "ix_login_attempts_lookup",
        "login_attempts",
        ["identifier", "created_at"],
        unique=False,
    )

    op.create_table(
        "api_keys",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_api_keys_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
    )
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"], unique=False)

    op.create_table(
        "one_time_tokens",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_one_time_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_one_time_tokens")),
    )
    op.create_index(
        "ix_one_time_tokens_lookup", "one_time_tokens", ["token_hash", "purpose"], unique=False
    )
    op.create_index(
        "ix_one_time_tokens_user", "one_time_tokens", ["user_id", "purpose"], unique=False
    )

    # ── users: 2FA и защита от перебора ──────────────────────────────
    op.add_column("users", sa.Column("totp_secret", sa.String(length=255), nullable=True))
    op.add_column(
        "users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column(
            "recovery_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))

    # ── сессии ────────────────────────────────────────────────────────
    op.add_column(
        "refresh_token_sessions", sa.Column("session_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "refresh_token_sessions", sa.Column("last_used_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "refresh_token_sessions", sa.Column("revoked_reason", sa.String(length=40))
    )
    op.create_index(
        op.f("ix_refresh_token_sessions_session_id"),
        "refresh_token_sessions",
        ["session_id"],
        unique=False,
    )
    # Существующие сессии получают собственный session_id: без него access-токены
    # старого формата работали бы до истечения, но потеряли бы привязку к сессии.
    op.execute(
        "UPDATE refresh_token_sessions SET session_id = replace(gen_random_uuid()::text, '-', '') "
        "WHERE session_id IS NULL"
    )

    # ── MCP-токен теперь шифротекст ───────────────────────────────────
    op.alter_column(
        "mcp_servers",
        "auth_token",
        existing_type=sa.VARCHAR(length=500),
        type_=sa.String(length=2000),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "mcp_servers",
        "auth_token",
        existing_type=sa.String(length=2000),
        type_=sa.VARCHAR(length=500),
        existing_nullable=True,
    )
    op.drop_index(op.f("ix_refresh_token_sessions_session_id"), table_name="refresh_token_sessions")
    op.drop_column("refresh_token_sessions", "revoked_reason")
    op.drop_column("refresh_token_sessions", "last_used_at")
    op.drop_column("refresh_token_sessions", "session_id")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
    op.drop_column("users", "recovery_codes")
    op.drop_column("users", "totp_enabled_at")
    op.drop_column("users", "totp_secret")
    op.drop_index("ix_one_time_tokens_user", table_name="one_time_tokens")
    op.drop_index("ix_one_time_tokens_lookup", table_name="one_time_tokens")
    op.drop_table("one_time_tokens")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_login_attempts_lookup", table_name="login_attempts")
    op.drop_index(op.f("ix_login_attempts_created_at"), table_name="login_attempts")
    op.drop_table("login_attempts")
