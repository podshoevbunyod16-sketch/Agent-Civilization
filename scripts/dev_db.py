#!/usr/bin/env python3
"""Поднимает локальный PostgreSQL для разработки (без Docker).

Использует пакет `pgserver` (встроенные бинарники PostgreSQL 16).
Скрипт рассчитан на длительную работу: он держит сервер запущенным,
пока процесс не будет остановлен (Ctrl+C / SIGTERM).

    python scripts/dev_db.py [--db agent_civilization]

Переменные окружения для backend:
    DATABASE_URL=postgresql+asyncpg://postgres@/<db>?host=<PGDATA>
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "var" / "pgdata"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="agent_civilization")
    args = parser.parse_args()

    try:
        import pgserver
    except ImportError:
        print("Установите зависимости: pip install pgserver", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # cleanup_mode=None: сервер остаётся жить после выхода из скрипта
    server = pgserver.get_server(str(DATA_DIR), cleanup_mode=None)
    uri = server.get_uri()
    databases = server.psql("SELECT datname FROM pg_database;")
    if args.db not in databases:
        server.psql(f'CREATE DATABASE "{args.db}";')
        print(f"Создана база {args.db}", flush=True)

    host = uri.split("host=")[-1]
    print("PostgreSQL готов", flush=True)
    print(f"DATABASE_URL=postgresql+asyncpg://postgres@/{args.db}?host={host}", flush=True)

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stopping:
        time.sleep(1)
    print("Остановка PostgreSQL", flush=True)
    server.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
