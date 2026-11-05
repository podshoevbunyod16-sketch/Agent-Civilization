"""Redis: rate limiting, распределённые блокировки, кеш, pub/sub событий.

Все операции деградируют безопасно: если Redis недоступен, приложение
не падает (для rate limit — fail-open с предупреждением в логе).

Важно: «Redis недоступен» и «значение не установлено» — разные состояния.
Для них используется отдельный сентинел ``UNAVAILABLE``, иначе anti-replay
защита подписанных запросов молча отключалась бы при потере Redis.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


class _Unavailable:
    """Сентинел: операция с Redis не выполнена (нет соединения/ошибка)."""

    _instance: _Unavailable | None = None

    def __new__(cls) -> _Unavailable:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover
        return "<redis-unavailable>"

    def __bool__(self) -> bool:
        return False


UNAVAILABLE = _Unavailable()

_client: Any | None = None


async def get_redis() -> Any | None:
    global _client
    if _client is None:
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True,
                socket_connect_timeout=1, socket_timeout=2,
            )
            await client.ping()
            _client = client
        except Exception as exc:  # pragma: no cover - инфраструктурный сбой
            logger.warning("redis_unavailable", extra={"error": str(exc)})
            _client = None
    return _client


def set_redis(client: Any | None) -> None:
    """Подмена клиента (используется тестами и воркерами)."""
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # pragma: no cover
            pass
    _client = None


async def _exec(operation: Any) -> Any:
    """Выполняет операцию; при сбое возвращает UNAVAILABLE (никогда None)."""
    try:
        client = await get_redis()
        if client is None:
            return UNAVAILABLE
        return await operation(client)
    except Exception as exc:  # pragma: no cover - инфраструктурный сбой
        logger.warning("redis_operation_failed", extra={"error": str(exc)})
        return UNAVAILABLE


async def incr_window(key: str, window_seconds: int) -> int | None:
    """Атомарный счётчик в окне (fixed window). None — если Redis недоступен."""

    async def _run(client: Any) -> int:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            result = await pipe.execute()
        return int(result[0])

    value = await _exec(_run)
    if value is UNAVAILABLE:
        return None
    return int(value)


async def set_json(key: str, value: str, ttl_seconds: int) -> bool:
    result = await _exec(lambda c: c.set(key, value, ex=ttl_seconds))
    return bool(result) and result is not UNAVAILABLE


async def get_json(key: str) -> str | None:
    result = await _exec(lambda c: c.get(key))
    return None if result is UNAVAILABLE else result


async def delete(key: str) -> None:
    await _exec(lambda c: c.delete(key))


async def acquire_lock(
    key: str, ttl_seconds: int = 30, wait_seconds: float = 0.0
) -> str | None:
    """Распределённая блокировка. SET NX атомарен на стороне Redis."""
    token = uuid.uuid4().hex
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        ok = await _exec(lambda c: c.set(f"lock:{key}", token, nx=True, ex=ttl_seconds))
        if ok is True:
            return token
        if ok is UNAVAILABLE:
            return None
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def release_lock(key: str, token: str) -> None:
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """
    await _exec(lambda c: c.eval(script, 1, f"lock:{key}", token))


async def publish(channel: str, message: str) -> int:
    result = await _exec(lambda c: c.publish(channel, message))
    if result is UNAVAILABLE:
        return 0
    return int(result or 0)


async def consume_once(key: str, ttl_seconds: int) -> bool:
    """Anti-replay для подписей узлов: True — если ключ увиден впервые.

    При недоступном Redis запрос пропускается (fail-open) и инцидент
    логируется: доступность важнее строгой защиты от replay, а подпись
    и временное окно всё равно проверяются.
    """
    ok = await _exec(lambda c: c.set(f"once:{key}", "1", nx=True, ex=ttl_seconds))
    if ok is UNAVAILABLE:
        logger.warning("anti_replay_degraded", extra={"key_present": True})
        return True
    return bool(ok)
