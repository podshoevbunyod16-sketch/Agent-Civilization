"""Демон резервного копирования (этап 10, F).

    python -m app.worker.backup --interval 3600

Что делает за проход:

1. создаёт зашифрованный снимок в `BACKUP_DIR`;
2. **сразу проверяет его восстановлением** в отдельной схеме — бэкап,
   который никогда не восстанавливали, бэкапом не считается;
3. применяет retention (`BACKUP_KEEP_DAYS`), удаляя и файл, и манифест;
4. пишет метрики: длительность, число строк, размер, успех проверки.

Остановка — SIGTERM/SIGINT после текущего прохода. Демон stateless: его
можно запускать в нескольких копиях, но обычно достаточно одной — при
параллельном запуске бэкапы просто создаются чаще (файлы уникальны по
времени и checksum).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import time

from app.core import db_metrics
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import metrics
from app.db.session import dispose_engine, get_session_factory
from app.services import backup as backup_service

logger = get_logger("backup-worker")

_running = True


def _stop(*_: object) -> None:
    global _running
    _running = False


async def run_once(*, verify: bool = True) -> dict[str, object]:
    factory = get_session_factory()
    started = time.perf_counter()
    async with factory() as session:
        manifest, path = await backup_service.create_backup(session)

    result: dict[str, object] = {
        "rows": manifest.rows,
        "bytes": manifest.extra.get("bytes"),
        "duration_ms": manifest.duration_ms,
        "path": str(path) if path else None,
        "verified": None,
    }

    if verify and path is not None:
        async with factory() as session:
            # cleanup: проверка не должна копить схемы `restore_*` в рабочей
            # базе — иначе каждая проверка оставляет мусор на диске.
            check = await backup_service.restore_backup(session, path, cleanup=True)
        result["verified"] = check["ok"]
        metrics.incr("backup_verify", outcome="ok" if check["ok"] else "failed")
        if not check["ok"]:
            # Громко: бэкап создан, но восстановление не сходится
            logger.error("backup_verify_failed", extra={"path": str(path), **check})

    removed = await asyncio.to_thread(
        backup_service.apply_retention,
        settings.backup_dir,
        keep_days=settings.backup_keep_days,
    )
    result["removed"] = removed
    metrics.incr("backup_created")
    metrics.observe_duration("backup_worker_run", (time.perf_counter() - started) * 1000)
    db_metrics.heartbeat("backup")
    logger.info("backup_pass", extra=result)
    return result


async def main(interval: int) -> None:
    global _running
    configure_logging()
    logger.info("backup_worker_started", extra={"interval": interval})
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, signal_name), _stop)

    while _running:
        try:
            await run_once()
        except Exception as exc:  # pragma: no cover - инфраструктурный сбой
            metrics.incr("worker_pass_failed", outcome="backup")
            logger.warning("backup_pass_failed", extra={"error": str(exc)})
        # Ждём интервал, но просыпаемся на остановку
        slept = 0
        while _running and slept < interval:
            await asyncio.sleep(min(5, interval - slept))
            slept += 5

    await dispose_engine()
    logger.info("backup_worker_stopped")


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(description="Резервное копирование с проверкой")
    parser.add_argument("--interval", type=int, default=3600, help="секунд между бэкапами")
    parser.add_argument("--no-verify", action="store_true", help="не проверять восстановлением")
    args = parser.parse_args()
    settings.validate_for_runtime()
    asyncio.run(main(args.interval))
