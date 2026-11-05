"""Rate limiting на Redis (fixed window), распределённый между репликами.

Design note: при недоступности Redis запросы НЕ блокируются (fail-open),
но инцидент фиксируется в логах. Для auth-эндпоинтов дополнительно
применяется лимит по IP, чтобы не давать перебирать пароли.
"""

from __future__ import annotations

import time

from app.core import redis_client
from app.core.config import settings
from app.core.errors import RateLimitError


async def check_rate_limit(bucket: str, identifier: str, *, limit: int, window: int = 60) -> None:
    if not settings.rate_limit_enabled:
        return
    now = int(time.time())
    window_start = now - (now % window)
    key = f"rl:{bucket}:{identifier}:{window_start}"
    counter = await redis_client.incr_window(key, window + 5)
    if counter is None:
        return
    if counter > limit:
        raise RateLimitError(
            "Слишком много запросов, попробуйте позже",
            details={
                "limit": limit,
                "window_seconds": window,
                "retry_after": window - (now % window),
            },
        )


async def check_default(identifier: str) -> None:
    await check_rate_limit("default", identifier, limit=settings.rate_limit_default_per_minute)


async def check_auth(identifier: str) -> None:
    await check_rate_limit("auth", identifier, limit=settings.rate_limit_auth_per_minute)
