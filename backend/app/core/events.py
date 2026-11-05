"""Шина событий реального времени.

В одном процессе — локальный pub/sub, в кластере — Redis Pub/Sub.
Реплики подписываются на канал ``ac:events`` и рассылают события своим
WebSocket-клиентам, поэтому горизонтальное масштабирование работает
без sticky sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from app.core import redis_client

logger = logging.getLogger(__name__)

CHANNEL = "ac:events"


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers[topic].add(queue)
        return queue

    async def unsubscribe(self, topic: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers[topic].discard(queue)
            if not self._subscribers[topic]:
                self._subscribers.pop(topic, None)

    def _local_publish(self, topic: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(topic, ())):
            if queue.full():
                # Медленный потребитель: отбрасываем самое старое событие
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                pass
        # Wildcard-подписчики получают всё
        if topic != "*":
            self._local_publish("*", event) if "*" in self._subscribers else None

    async def publish(self, topic: str, event: dict[str, Any]) -> None:
        payload = {"topic": topic, "event": event}
        self._local_publish(topic, event)
        try:
            await redis_client.publish(CHANNEL, json.dumps(payload, default=str))
        except Exception as exc:  # pragma: no cover
            logger.warning("event_publish_failed", extra={"error": str(exc)})

    async def receive_remote(self, payload: str) -> None:
        """Вызывается фоновым слушателем Redis."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:  # pragma: no cover
            return
        self._local_publish(data.get("topic", "*"), data.get("event", {}))

    @property
    def subscriber_count(self) -> int:
        return sum(len(v) for v in self._subscribers.values())


event_bus = EventBus()
