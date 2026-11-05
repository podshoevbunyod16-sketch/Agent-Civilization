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
