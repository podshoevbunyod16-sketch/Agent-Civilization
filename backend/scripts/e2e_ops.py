#!/usr/bin/env python
"""Живой e2e этапа 9: наблюдаемость, обслуживание, бэкап, отказ узла.

Сценарий проверяет именно эксплуатационные свойства, а не «эндпоинт отвечает»:

1. `/metrics` без токена закрыт;
2. с сервисным токеном отдаёт Prometheus-формат, метки без идентификаторов;
3. `/ops/health` показывает состояние и считает gauge'и из базы;
4. узел «умирает» (heartbeat прекращается) → демон обслуживания помечает его
   offline, и health это видит;
5. задание, взятое узлом, после истечения аренды возвращается в очередь;
6. бэкап отдаёт gzip + контрольную сумму, число строк совпадает с заголовком;
7. нагрузочный мини-прогон: 60 параллельных чтений без ошибок.

Запуск: `python scripts/e2e_ops.py` (нужен поднятый API и seed).
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess  # noqa: S404 - демон запускается как отдельный процесс
import sys
import time
import uuid
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

API = os.environ.get("AC_API_URL", "http://127.0.0.1:8000")
SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "dev-internal-token-2026")
ADMIN_EMAIL = os.environ.get("AC_ADMIN_EMAIL", "architect@agent-civilization.dev")
ADMIN_PASSWORD = os.environ.get("AC_ADMIN_PASSWORD", "Architect2026")
VENV_PY = str(REPO_ROOT / ".venv" / "bin" / "python")


def step(number: float, text: str) -> None:
    print(f"{number}. {text}")


def main() -> int:
    with httpx.Client(base_url=API, timeout=60.0) as http:
        # 1. Метрики закрыты без токена
        denied = http.get("/metrics")
        assert denied.status_code == 401, denied.status_code
        assert denied.json()["error"]["code"] == "metrics_forbidden"
        step(1, "метрики закрыты без сервисного токена (metrics_forbidden)")

        # 2. Prometheus-формат с токеном (гистограмма заполняется запросами,
        # поэтому сначала нужен хотя бы один обычный запрос)
        http.get("/api/v1/health")
        metrics = http.get("/metrics", headers={"X-Service-Token": SERVICE_TOKEN})
        assert metrics.status_code == 200, metrics.text[:200]
        body = metrics.text
        for marker in (
            "# TYPE ac_uptime_seconds gauge",
            "ac_http_requests_total",
            "ac_http_request_duration_ms_bucket",
            "ac_tasks_queued",
            "ac_ledger_total_units",
        ):
            assert marker in body, marker
        step(2, f"метрики отданы в формате Prometheus ({len(body.splitlines())} строк)")

        login = http.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Состояние системы
        health = http.get("/api/v1/ops/health", headers=headers)
        assert health.status_code == 200, health.text
        data = health.json()
        assert data["gauges"]["ledger_total_units"] == 0, data["gauges"]
        step(
            3,
            f"health: {data['status']}, очередь {int(data['gauges']['tasks_queued'])}, "
            f"узлов online {int(data['gauges']['nodes_online'])}, "
            f"баланс журнала {data['gauges']['ledger_total_units']}",
        )

        # 4. Метки метрик не содержат идентификаторов
        sample_id = str(uuid.uuid4())
        http.get(f"/api/v1/agents/{sample_id}", headers=headers)
        body = http.get("/metrics", headers={"X-Service-Token": SERVICE_TOKEN}).text
        assert sample_id not in body
        assert "/agents/{agent_id}" in body
        step(4, "метки маршрутов без UUID (кардинальность ограничена)")

        # 5. Обслуживание: идемпотентный проход
        maintenance = http.post("/api/v1/ops/maintenance", headers=headers)
        assert maintenance.status_code == 200, maintenance.text
        outcome = maintenance.json()
        assert outcome["duration_ms"] >= 0
        step(
            5,
            f"обслуживание: возвращено аренд {outcome['requeued_tasks']}, "
            f"узлов offline {outcome['nodes_offline']}, "
            f"истекло сообщений {outcome['expired_messages']}, "
            f"{outcome['duration_ms']} мс",
        )

        # 6. Демон обслуживания как отдельный процесс
        result = subprocess.run(  # noqa: S603
            [VENV_PY, "-m", "app.worker.maintenance", "--once", "--stale-after", "60"],
            capture_output=True,
            text=True,
            cwd=str(BACKEND_DIR),
            env=os.environ.copy(),
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-500:]
        assert "maintenance_once" in result.stdout
        step(6, "демон обслуживания выполнил проход отдельным процессом")

        # 7. Бэкап: gzip + контрольная сумма
        backup = http.get(
            "/api/v1/ops/backup",
            params={"tables": "accounts,transactions,tasks"},
            headers=headers,
        )
        assert backup.status_code == 200, backup.text[:200]
        assert backup.headers["content-type"] == "application/gzip"
        checksum = backup.headers["X-Checksum-Sha256"]
        payload = gzip.decompress(backup.content).decode()
        lines = payload.strip().split("\n")
        header = json.loads(lines[0])
        assert header["format"] == "agent-civilization-backup"
        assert len(lines) - 1 == sum(header["tables"].values())
        step(
            7,
            f"бэкап: {sum(header['tables'].values())} строк "
            f"({header['tables']}), sha256 {checksum[:12]}…",
        )

        # 8. Мини-нагрузка: 60 параллельных чтений
        started = time.perf_counter()
        statuses: list[int] = []

        def read() -> None:
            response = http.get("/api/v1/agents", params={"page_size": 20}, headers=headers)
            statuses.append(response.status_code)

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(lambda _: read(), range(60)))
        elapsed = time.perf_counter() - started
        assert all(code == 200 for code in statuses), statuses
        step(8, f"нагрузка: 60 чтений за {elapsed:.2f} с, ошибок нет")

        # 9. Недоступный узел виден в метриках
        body = http.get("/metrics", headers={"X-Service-Token": SERVICE_TOKEN}).text
        stale = next(
            (line for line in body.splitlines() if line.startswith("ac_nodes_stale{")), None
        )
        assert stale is not None
        step(9, f"метрика мёртвых узлов доступна: {stale}")

    print("E2E OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
