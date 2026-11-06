"""Rate limiting (этап 10, A #4–#6).

Схема: распределённый счётчик в Redis + локальный резервный счётчик в
процессе. Резервный слой нужен потому, что «fail-open при недоступности
Redis» — это дыра: достаточно уронить Redis (или дождаться его перезапуска),
чтобы перебор паролей перестал ограничиваться вообще.

Правила:

* **Redis доступен** — считаем там, локальный резервный счётчик не растёт.
* **Redis недоступен** — считаем локально, в памяти процесса. Порог тот же.
  При нескольких репликах это даёт лимит `limit × реплики`: слабее, чем
  точный общий лимит, но строго лучше, чем полное отсутствие ограничения.
  Факт деградации виден в метрике `ratelimit_backend_fallback_total`
  и в `/ops/health`.
* **Анти-replay nonce** — не fail-open: при недоступном Redis подписанный
  запрос отклоняется (иначе повтор запроса проходит проверку дважды).
  Это осознанный выбор в пользу безопасности: узел повторит запрос.

Лимиты применяются к критическим операциям отдельными ведрами
(`check_critical`, `check_write`), а не только к auth.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from app.core import redis_client
from app.core.config import settings
from app.core.errors import RateLimitError
from app.core.logging import get_logger

logger = get_logger(__name__)

# bucket -> {окно: счётчик}
_local_counters: dict[str, dict[int, int]] = defaultdict(dict)
_local_lock = threading.Lock()
LOCAL_MAX_BUCKETS = 10_000


def _local_incr(bucket: str, identifier: str, window: int) -> int:
    """Резервный счётчик в памяти процесса (fixed window)."""
    now = int(time.time())
    window_start = now - (now % window)
    key = f"{bucket}:{identifier}"
    with _local_lock:
        if len(_local_counters) > LOCAL_MAX_BUCKETS:
            _local_counters.clear()  # защита от роста памяти при высокой кардинальности
        buckets = _local_counters[key]
        # Оставляем только текущее окно: старые ключи не нужны
        for stale in [w for w in buckets if w < window_start]:
            buckets.pop(stale, None)
        buckets[window_start] = buckets.get(window_start, 0) + 1
        return buckets[window_start]


def _raise_limit(limit: int, window: int, *, backend: str) -> None:
    now = int(time.time())
    from app.core.metrics import metrics

    metrics.incr(f"ratelimit_hit_{backend}")
    raise RateLimitError(
        "Слишком много запросов, попробуйте позже",
        details={
            "limit": limit,
            "window_seconds": window,
            "retry_after": window - (now % window),
        },
    )


async def check_rate_limit(bucket: str, identifier: str, *, limit: int, window: int = 60) -> None:
    if not settings.rate_limit_enabled:
        return
    now = int(time.time())
    window_start = now - (now % window)
    key = f"rl:{bucket}:{identifier}:{window_start}"
    counter = await redis_client.incr_window(key, window + 5)
    if counter is None:
        # Redis недоступен: не пропускаем запрос безусловно, а ограничиваем
        # локально (см. docstring модуля).
        from app.core.metrics import metrics

        metrics.incr("ratelimit_backend_fallback")
        local = _local_incr(bucket, identifier, window)
        if local > limit:
            _raise_limit(limit, window, backend="local")
        return
    if counter > limit:
        _raise_limit(limit, window, backend="redis")


async def check_default(identifier: str) -> None:
    await check_rate_limit("default", identifier, limit=settings.rate_limit_default_per_minute)


async def check_auth(identifier: str) -> None:
    await check_rate_limit("auth", identifier, limit=settings.rate_limit_auth_per_minute)


async def check_write(identifier: str) -> None:
    """Изменяющие операции: ниже лимит, чем на чтение."""
    await check_rate_limit("write", identifier, limit=settings.rate_limit_write_per_minute)


async def check_critical(identifier: str) -> None:
    """Критические операции (деньги, ротация секретов, бэкап, 2FA)."""
    await check_rate_limit("critical", identifier, limit=settings.rate_limit_critical_per_minute)


async def check_node(node_key: str) -> None:
    """Лимит на подписанные запросы узла (heartbeat/claim)."""
    await check_rate_limit("node", node_key, limit=settings.rate_limit_node_per_minute)


def reset_local() -> None:
    """Сброс резервных счётчиков (тесты)."""
    with _local_lock:
        _local_counters.clear()


def local_state() -> int:
    with _local_lock:
        return len(_local_counters)
