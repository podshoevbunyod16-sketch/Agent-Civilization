from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseModel


class UserRole(enum.StrEnum):
    member = "member"
    developer = "developer"
    admin = "admin"


class User(BaseModel):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=False, length=20),
        default=UserRole.developer,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── 2FA (TOTP), этап 10, B ────────────────────────────────────────
    # Секрет TOTP хранится только в зашифрованном виде (AES-GCM, app/core/secrets).
    totp_secret: Mapped[str | None] = mapped_column(String(255))
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Коды восстановления: хранятся хешами, каждый одноразовый.
    recovery_codes: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    # Счётчик неудачных входов подряд (сбрасывается успешным входом).
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agents: Mapped[list[Agent]] = relationship(  # noqa: F821
        back_populates="owner"
    )

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.admin


class RefreshTokenSession(BaseModel):
    """Серверная сессия refresh-токена (ротация + отзыв)."""

    __tablename__ = "refresh_token_sessions"

    user_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    # Идентификатор сессии в access-токене (`sid`): позволяет отозвать
    # access-токен до истечения и показывать пользователю список устройств.
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LoginAttempt(BaseModel):
    """Попытки входа для защиты от перебора (этап 10, B).

    Ключ — email (нормализованный) и IP: серия неудач блокирует вход на
    время `login_lockout_seconds`, независимо от того, существует ли
    аккаунт (иначе по разнице ответов видно, какие email зарегистрированы).
    """

    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_lookup", "identifier", "created_at"),
    )

    identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class OneTimeToken(BaseModel):
    """Одноразовый токен: подтверждение email, сброс пароля, приглашение.

    Свойства, которые проверяются тестами:

    * **энтропия** — 32 байта из `secrets.token_urlsafe`;
    * **в БД только хеш** (sha256 от токена с «перчинкой» SECRET_KEY):
      утечка дампа БД не даёт возможности воспользоваться ссылкой;
    * **TTL** — короткий, задаётся на создание;
    * **single-use** — при успешном использовании ставится `used_at`,
      повторная попытка отклоняется; все прочие активные токены того же
      назначения отзываются (новая ссылка инвалидирует старую).
    """

    __tablename__ = "one_time_tokens"
    __table_args__ = (
        Index("ix_one_time_tokens_lookup", "token_hash", "purpose"),
        Index("ix_one_time_tokens_user", "user_id", "purpose"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, nullable=False)


class ApiKey(BaseModel):
    """Программный ключ доступа (для CI/автоматизации, этап 10, B).

    Хранится хеш, показывается один раз. Отдельно от пароля: отзывается
    без смены пароля и живёт с собственным сроком и набором прав (scope).
    """

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_prefix", "prefix"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
