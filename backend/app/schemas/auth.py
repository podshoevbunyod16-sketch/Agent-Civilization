from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.config import settings
from app.db.models.user import UserRole


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=2, max_length=120)

    @field_validator("password")
    @classmethod
    def _strong_password(cls, value: str) -> str:
        if len(value) < settings.password_min_length:
            raise ValueError(
                f"Пароль должен быть не короче {settings.password_min_length} символов"
            )
        if value.isdigit() or value.isalpha():
            raise ValueError("Пароль должен содержать буквы и цифры")
        return value

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Имя не может быть пустым")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class LogoutRequest(BaseModel):
    refresh_token: str | None = None
    all_sessions: bool = False


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: UserRole
    is_active: bool
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserPublic


class AccessToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _strong(cls, value: str) -> str:
        if len(value) < settings.password_min_length:
            raise ValueError(
                f"Пароль должен быть не короче {settings.password_min_length} символов"
            )
        if value.isdigit() or value.isalpha():
            raise ValueError("Пароль должен содержать буквы и цифры")
        return value


# ── Этап 10, B: 2FA, сессии, токены, API-ключи ────────────────────────
class LoginRequestV2(LoginRequest):
    """Вход с необязательным кодом второго фактора.

    Клиент сначала отправляет email/пароль; если сервер отвечает
    `mfa_required`, повторяет запрос с `totp_code` (или кодом восстановления).
    """

    totp_code: str | None = Field(default=None, max_length=32)


class EmailRequest(BaseModel):
    email: EmailStr


class TokenConfirmRequest(BaseModel):
    token: str = Field(min_length=10, max_length=512)


class PasswordResetRequest(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _strong(cls, value: str) -> str:
        if len(value) < settings.password_min_length:
            raise ValueError(
                f"Пароль должен быть не короче {settings.password_min_length} символов"
            )
        if value.isdigit() or value.isalpha():
            raise ValueError("Пароль должен содержать буквы и цифры")
        return value


class TokenIssued(BaseModel):
    """Ответ на запрос ссылки.

    Сама ссылка в production уходит письмом; поле `delivery` показывает,
    как это произошло (`email`). В dev-режиме (`EMAIL_DELIVERY=console`)
    токен возвращается в ответе, чтобы сценарий был проверяем без SMTP.
    Ответ на восстановление доступа одинаков для существующего и
    несуществующего email — иначе это способ перечислить пользователей.
    """

    message: str = "Если адрес зарегистрирован, письмо отправлено"
    delivery: str = "email"
    dev_token: str | None = None


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    issuer: str


class TotpEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TotpEnableResponse(BaseModel):
    recovery_codes: list[str]
    warning: str = "Сохраните коды восстановления: они показываются один раз"


class TotpDisableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)


class SessionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_agent: str | None = None
    ip_address: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime
    is_current: bool = False


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    scopes: list[str] = Field(default_factory=list, max_length=20)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreated(BaseModel):
    key: str
    warning: str = "Ключ показывается один раз: сохраните его"
    api_key: ApiKeyPublic


class AccountSecurity(BaseModel):
    """Сводка безопасности аккаунта (используется в `/auth/me` и UI)."""

    email_verified: bool
    mfa_enabled: bool
    mfa_required: bool
    active_sessions: int
    active_api_keys: int
    warnings: list[str] = []
