"""Отправитель outbox (этап 10, D).

    python -m app.worker.outbox --interval 1

Stateless-цикл: можно запускать в любом числе копий. Пачки забираются
`FOR UPDATE SKIP LOCKED`, поэтому две копии не отправляют одно и то же
одновременно. Гарантия доставки — **at-least-once**: при падении между
публикацией и commit строка вернётся в очередь и событие уйдёт повторно.
`exactly-once` здесь не заявляется и не нужен: подписчики идемпотентны по
`event_id`.

Проход также:
* возвращает «зависшие» строки (`sending` дольше минуты) в очередь;
* удаляет опубликованные события старше retention (outbox не растёт вечно).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import time

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_session_factory
from app.services import outbox

logger = get_logger("outbox-worker")

_running = True


def _stop(*_: object) -> None:
    global _running
    _running = False


async def run_once(*, limit: int = 100, purge: bool = False) -> dict[str, int]:
    factory = get_session_factory()
    async with factory() as session:
        reclaimed = await outbox.reclaim_stuck(session, older_than_seconds=60)
        result = await outbox.publish_batch(session, limit=limit)
        result["reclaimed"] = reclaimed
        if purge:
            result["purged"] = await outbox.purge_published(session)
        # Коммитим: события, отправленные в этом проходе, помечаются published
        await session.commit()
        return result


async def main(interval: float = 1.0, purge_every: int = 300) -> None:
    global _running
    configure_logging()
    logger.info("outbox_worker_started", extra={"interval": interval})
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, signal_name), _stop)

    ticks = 0
    while _running:
        started = time.perf_counter()
        try:
            result = await run_once(purge=(ticks % max(1, purge_every)) == 0)
            if result["claimed"] or result["reclaimed"]:
                logger.info("outbox_pass", extra=result)
        except Exception as exc:  # pragma: no cover - инфраструктурный сбой
            logger.warning("outbox_pass_failed", extra={"error": str(exc)})
        ticks += 1
        elapsed = time.perf_counter() - started
        await asyncio.sleep(max(0.0, interval - elapsed))

    await dispose_engine()
    logger.info("outbox_worker_stopped")


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(description="Отправитель исходящих событий (outbox)")
    parser.add_argument("--interval", type=float, default=1.0, help="пауза между проходами")
    parser.add_argument(
        "--purge-every", type=int, default=300, help="чистить опубликованные раз в N проходов"
    )
    args = parser.parse_args()
    settings.validate_for_runtime()
    asyncio.run(main(interval=args.interval, purge_every=args.purge_every))
