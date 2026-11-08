"""Create, hash, list, and revoke user API keys."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models.api_key import ApiKey
from app.db.models.user import User
from app.schemas.auth import ApiKeyCreate

API_KEY_PREFIX = "ac_live_"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_api_key(
    session: AsyncSession, *, user: User, data: ApiKeyCreate
) -> tuple[ApiKey, str]:
    secret = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    record = ApiKey(
        user_id=user.id,
        name=data.name.strip(),
        key_prefix=secret[:16],
        key_hash=token_hash(secret),
        expires_at=now + timedelta(days=data.expires_in_days),
    )
    session.add(record)
    await session.flush()
    return record, secret


async def list_api_keys(session: AsyncSession, *, user_id: uuid.UUID) -> list[ApiKey]:
    rows = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    return list(rows.scalars().all())


async def revoke_api_key(session: AsyncSession, *, key_id: uuid.UUID, user_id: uuid.UUID) -> ApiKey:
    row = (
        await session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("API-ключ не найден", code="api_key_not_found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.flush()
    return row


async def authenticate_api_key(session: AsyncSession, token: str) -> User | None:
    if not token.startswith(API_KEY_PREFIX):
        return None
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == token_hash(token)))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    user = (
        await session.execute(select(User).where(User.id == row.user_id, User.is_active.is_(True)))
    ).scalar_one_or_none()
    return user
