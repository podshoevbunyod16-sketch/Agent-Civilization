"""Rate limiting на Redis (fixed window), распределённый между репликами.

При недоступности Redis включается локальный резервный лимитер (процесс-
локальный fixed window, ограниченная память): защита от брутфорса и флуда
сохраняется и без Redis, вплоть до рестарта процесса. Redis остаётся
основным путём; локальный резерв — деградация, а не замена.
"""

from __future__ import annotations

import threading
import time

from app.core import redis_client
from app.core.config import settings
from app.core.errors import RateLimitError


class _LocalWindowLimiter:
    """Процесс-локальный fixed window с ограничением по памяти."""

    MAX_KEYS = 20_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, tuple[int, int]] = {}  # key -> (window, count)

    def incr(self, key: str, window: int) -> int:
        now = int(time.time())
        window_start = now - (now % window)
        with self._lock:
            current_window, count = self._counters.get(key, (window_start, 0))
            if current_window != window_start:
                current_window, count = window_start, 0
            count += 1
            self._counters[key] = (current_window, count)
            if len(self._counters) > self.MAX_KEYS:
                # Вытесняем самые старые окна, чтобы память не росла бесконечно
                stale = [k for k, (w, _) in self._counters.items() if w < window_start - window]
                for k in stale[: self.MAX_KEYS // 2]:
                    self._counters.pop(k, None)
                if len(self._counters) > self.MAX_KEYS:
                    for k in list(self._counters)[: self.MAX_KEYS // 2]:
                        self._counters.pop(k, None)
            return count


_local_limiter = _LocalWindowLimiter()


async def check_rate_limit(bucket: str, identifier: str, *, limit: int, window: int = 60) -> None:
    if not settings.rate_limit_enabled:
        return
    now = int(time.time())
    window_start = now - (now % window)
    key = f"rl:{bucket}:{identifier}:{window_start}"
    counter = await redis_client.incr_window(key, window + 5)
    if counter is None:
        # Redis недоступен — локальный резерв: защита хуже, чем у Redis,
        # но не отсутствует вовсе.
        counter = _local_limiter.incr(key, window)
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
