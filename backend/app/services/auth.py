"""Регистрация, вход, ротация refresh-токенов, отзыв сессий."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AuthenticationError, ConflictError
from app.core.security import (
    TokenError,
    create_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.db.models.user import RefreshTokenSession, User, UserRole
from app.schemas.auth import RegisterRequest

# Фиктивный хеш: выравнивает время ответа для несуществующих email
_DUMMY_HASH = hash_password("dummy-password-for-timing-equalization-1")


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == email.lower().strip()))
    ).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def register_user(session: AsyncSession, data: RegisterRequest) -> User:
    email = data.email.lower().strip()
    existing = await get_user_by_email(session, email)
    if existing is not None:
        raise ConflictError("Пользователь с таким email уже существует", code="email_taken")

    user = User(
        email=email,
        password_hash=hash_password(data.password),
        display_name=data.display_name,
        role=UserRole.developer,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "Пользователь с таким email уже существует", code="email_taken"
        ) from exc
    return user


async def authenticate(
    session: AsyncSession, email: str, password: str
) -> tuple[User, bool]:
    """Возвращает (user, needs_rehash). Не раскрывает, что именно неверно."""
    user = await get_user_by_email(session, email)
    if user is None:
        verify_password(password, _DUMMY_HASH)  # constant-ish time
        raise AuthenticationError("Неверный email или пароль", code="invalid_credentials")
    if not verify_password(password, user.password_hash):
        raise AuthenticationError("Неверный email или пароль", code="invalid_credentials")
    if not user.is_active:
        raise AuthenticationError("Учётная запись отключена", code="account_disabled")
    return user, password_needs_rehash(user.password_hash)


async def issue_token_pair(
    session: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, str, datetime]:
    access, _, _ = create_token(str(user.id), "access", extra_claims={"role": user.role.value})
    refresh, refresh_expires, refresh_jti = create_token(str(user.id), "refresh")

    session.add(
        RefreshTokenSession(
            user_id=user.id,
            jti=refresh_jti,
            expires_at=refresh_expires,
            user_agent=(user_agent or "")[:255] or None,
            ip_address=(ip_address or "")[:64] or None,
        )
    )
    user.last_login_at = datetime.now(UTC)
    await session.flush()
    return access, refresh, refresh_expires


async def rotate_refresh_token(
    session: AsyncSession,
    refresh_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str, str]:
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise AuthenticationError("Недействительный refresh-токен", code="invalid_token") from exc

    jti = payload["jti"]
    stored = (
        await session.execute(
            select(RefreshTokenSession).where(RefreshTokenSession.jti == jti).with_for_update()
        )
    ).scalar_one_or_none()

    if stored is None:
        raise AuthenticationError("Сессия не найдена", code="session_not_found")

    if stored.revoked_at is not None:
        # Повторное использование отозванного токена → компрометация:
        # отзываем все сессии пользователя.
        await session.execute(
            update(RefreshTokenSession)
            .where(
                RefreshTokenSession.user_id == stored.user_id,
                RefreshTokenSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        # Отзыв сессий фиксируется отдельной транзакцией: откат из-за
        # ошибки аутентификации не должен отменять защитную меру.
        await session.commit()
        raise AuthenticationError(
            "Refresh-токен уже использован, все сессии отозваны", code="token_reuse_detected"
        )

    if stored.expires_at <= datetime.now(UTC):
        raise AuthenticationError("Сессия истекла", code="session_expired")

    user = await get_user_by_id(session, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthenticationError("Пользователь недоступен", code="account_disabled")

    stored.revoked_at = datetime.now(UTC)
    access, refresh, _ = await issue_token_pair(
        session, user, user_agent=user_agent, ip_address=ip_address
    )
    new_payload = decode_token(refresh, expected_type="refresh")
    stored.replaced_by_jti = new_payload["jti"]
    await session.flush()
    return user, access, refresh


async def revoke_session(session: AsyncSession, refresh_token: str) -> bool:
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except TokenError:
        return False
    stored = (
        await session.execute(
            select(RefreshTokenSession).where(RefreshTokenSession.jti == payload["jti"])
        )
    ).scalar_one_or_none()
    if stored is None or stored.revoked_at is not None:
        return False
    stored.revoked_at = datetime.now(UTC)
    await session.flush()
    return True


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        update(RefreshTokenSession)
        .where(
            RefreshTokenSession.user_id == user_id,
            RefreshTokenSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await session.flush()
    return int(result.rowcount or 0)


async def change_password(
    session: AsyncSession, user: User, current_password: str, new_password: str
) -> None:
    if not verify_password(current_password, user.password_hash):
        raise AuthenticationError("Текущий пароль неверен", code="invalid_credentials")
    user.password_hash = hash_password(new_password)
    await revoke_all_sessions(session, user.id)
    await session.flush()


def token_ttl() -> int:
    return settings.access_token_ttl_seconds
