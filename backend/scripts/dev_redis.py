"""Настоящий Redis для chaos-замеров (fakeredis по TCP).

Зачем: часть защиты работает *только* при доступном Redis — anti-replay
подписанных запросов узлов (fail-closed), распределённый rate-limit,
pub/sub между репликами. Если Redis в окружении нет, сценарии «узел берёт
задание» и «аренда возвращается после падения» проверить нельзя, и
заявлять их проверенными было бы неправдой.

Скрипт поднимает `fakeredis` в режиме TCP-сервера, то есть приложение
общается с ним по обычному протоколу redis, без подмены клиента в коде:

    python scripts/dev_redis.py --port 6379

Ограничение честно: это не redis-server, поведение под нагрузкой и при
репликации не совпадает. Для chaos-тестов «сервис недоступен / снова
доступен» этого достаточно, для замера пропускной способности Redis — нет.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="TCP-сервер fakeredis для проверок")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    args = parser.parse_args()

    try:
        import fakeredis
    except ImportError:  # pragma: no cover - dev-зависимость
        print("нужен fakeredis: pip install fakeredis", file=sys.stderr)
        return 2

    server = fakeredis.TcpFakeServer((args.host, args.port), server_type="redis")
    print(f"fakeredis слушает redis://{args.host}:{args.port}", flush=True)
    try:
        # serve_forever(), а не sleep: сокет открыт и после bind, но без
        # цикла обработки соединения просто не принимаются (проверено).
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
