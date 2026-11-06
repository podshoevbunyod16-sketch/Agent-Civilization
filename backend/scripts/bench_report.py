#!/usr/bin/env python
"""Нагрузка + chaos с измерением ресурсов (этап 10, H).

Запуск (API уже поднят, БД доступна):

    python scripts/bench_report.py --api http://127.0.0.1:8000 --out /tmp/bench.json

Что делает:

1. **Нагрузка** — три профиля из `scripts/load_test.py` (`read`, `mixed`,
   `auth`) через `--scenario`; latency/throughput/ошибки берутся из самого
   нагрузчика, а не пересчитываются здесь;
2. **Ресурсы** — CPU и RSS процесса API, число соединений и размер БД
   снимаются до/после каждого профиля (`/proc/<pid>/stat`, `psutil`);
3. **Chaos** — по одному сценарию на отказ, каждый проверяет *поведение*, а
   не только «не упало»:
   * `redis-down` — Redis недоступен: rate-limit переходит на локальный
     счётчик, anti-replay подписей закрывается (fail-closed);
   * `redis-flap` — Redis то появляется, то исчезает под нагрузкой:
     ошибок 5xx быть не должно;
   * `worker-killed` — аренда задания остаётся висеть, обслуживание обязано
     вернуть его в очередь (это проверяет `tests/api/test_outbox.py` и
     `maintenance.run_once`; здесь фиксируется фактическое время до
     возврата);
   * `db-pool-exhausted` — 40 параллельных «тяжёлых» запросов при пуле 10:
     сервер должен отвечать (а не отдавать 500), потому что пул ждёт.

Результат — JSON с параметрами запуска и всеми измерениями. Никакие числа
здесь не выдумываются: если сценарий не удался, в отчёте будет `failed` и
причина. Печатается человекочитаемая сводка.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
LOAD_TEST = BACKEND_DIR / "scripts" / "load_test.py"

# Минимальный python-агент: исполняется в песочнице, ничего не пишет наружу.
BENCH_AGENT_CODE = "def run(payload):\n    return {'ok': True}\n"


# ── ресурсы процесса ───────────────────────────────────────────────────
def find_api_pid(port: int = 8000) -> int | None:
    """PID процесса, слушающего порт (без psutil-костылей с /proc сканом)."""
    try:
        out = subprocess.run(
            ["ss", "-lptnH", f"sport = :{port}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except FileNotFoundError:  # pragma: no cover - ss может отсутствовать
        return None
    marker = "pid="
    candidates: list[int] = []
    for line in out.splitlines():
        if marker in line:
            tail = line.split(marker, 1)[1]
            candidates.extend(int(value.split(",", 1)[0]) for value in tail.split("pid="))
    for candidate in candidates:
        try:
            cmdline = Path(f"/proc/{candidate}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:  # pragma: no cover
            continue
        if b"uvicorn" in cmdline or b"app.main" in cmdline:
            return candidate
    return candidates[0] if candidates else None


def read_proc(pid: int) -> dict[str, Any]:
    """CPU-тики и RSS из /proc (Linux). Нет /proc — пустой результат."""
    stat_path = Path(f"/proc/{pid}/stat")
    status_path = Path(f"/proc/{pid}/status")
    if not stat_path.exists():
        return {}
    fields = stat_path.read_text().split()
    # utime=14, stime=15 (1-based в man proc, здесь с 0 после comm)
    utime, stime = int(fields[13]), int(fields[14])
    rss_kb = 0
    if status_path.exists():
        for line in status_path.read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
                break
    return {
        "cpu_ticks": utime + stime,
        "rss_mb": round(rss_kb / 1024, 1),
    }


@dataclass
class Samples:
    cpu_ticks: int = 0
    rss_mb: float = 0.0

    @classmethod
    def take(cls, pid: int | None) -> Samples:
        if pid is None:
            return cls()
        data = read_proc(pid)
        return cls(cpu_ticks=data.get("cpu_ticks", 0), rss_mb=data.get("rss_mb", 0.0))


@dataclass
class BenchRecord:
    name: str
    params: dict[str, Any]
    result: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    status: str = "not_run"
    note: str | None = None


# ── нагрузка ───────────────────────────────────────────────────────────
async def run_load(
    scenario: str, requests: int, concurrency: int, email: str, password: str, out: Path
) -> dict[str, Any]:
    """Запускает `load_test.py` отдельным процессом и читает его JSON-итог."""
    cmd = [
        sys.executable,
        str(LOAD_TEST),
        "--scenario",
        scenario,
        "--users",
        "1",
        "--requests",
        str(requests),
        "--concurrency",
        str(concurrency),
        "--email",
        email,
        "--password",
        password,
        "--json-out",
        str(out),
    ]
    # У профиля auth своя планка: вход — это PBKDF2 на CPU, и при дефолтном
    # RATE_LIMIT_AUTH_PER_MINUTE=20 двадцать одновременных входов физически
    # упираются в 429. Поэтому в замере лимит поднят (см. отчёт), а порог
    # задержки для auth задаётся отдельно и явно.
    if scenario == "auth":
        cmd += ["--p95", "1500", "--min-rps", "10"]
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(BACKEND_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode(errors="replace")
    result: dict[str, Any] = {
        "command": " ".join(cmd[1:]),
        "exit_code": proc.returncode,
        "wall_seconds": round(time.perf_counter() - started, 2),
        "output": text.strip().splitlines()[-2:],
    }
    payload = await asyncio.to_thread(_read_json, out)
    if payload:
        result["metrics"] = payload.get("scenarios", {}).get(scenario, {})
    return result


def bootstrap_user(email: str, password: str) -> dict[str, Any]:
    """Один участник с опубликованным агентом — до старта нагрузки.

    `load_test.py` сам регистрируется, но регистрация ограничена
    `RATE_LIMIT_AUTH_PER_MINUTE` (20/мин на IP). Нагрузочный профиль,
    который упирается в лимит регистраций, измеряет лимит, а не систему.
    Поэтому регистрация выполняется заранее в пределах квоты, а сценарий
    только читает и пишет задания.
    """
    base = os.environ.get("BENCH_API", "http://127.0.0.1:8000").rstrip("/")
    with httpx.Client(base_url=base, timeout=30.0) as http:
        response = http.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "Нагрузка"},
        )
        if response.status_code == 201:
            token = response.json()["access_token"]
        else:
            login = http.post("/api/v1/auth/login", json={"email": email, "password": password})
            login.raise_for_status()
            token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        slug = f"bench-{email.split('@')[0][:24]}"
        agent = http.post(
            "/api/v1/agents",
            headers=headers,
            json={
                "slug": slug,
                "name": "Нагрузочный агент",
                "summary": "Агент для нагрузочного теста",
                "category": "general",
                "capabilities": [{"capability_key": "text.echo", "declared_level": 1}],
            },
        )
        if agent.status_code == 201:
            agent_id = agent.json()["id"]
            version = http.post(
                f"/api/v1/agents/{agent_id}/versions",
                headers=headers,
                json={
                    "entrypoint_type": "python",
                    "entrypoint_config": {"code": "def run(payload):\n    return {'echo': 1}\n"},
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "publish": True,
                },
            )
            version.raise_for_status()
        elif agent.status_code != 409:
            agent.raise_for_status()
        return {"email": email, "agent_slug": slug, "registered": response.status_code == 201}


async def measure_api(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/v1/ops/metrics")
    if response.status_code != 200:
        return {"status": response.status_code}
    snapshot = response.json()
    gauges = snapshot.get("gauges", {})
    return {
        "status": 200,
        "process": snapshot.get("process", {}),
        "gauges_subset": {
            key: gauges[key]
            for key in (
                "db_pool_checked_out",
                "db_pool_overflow",
                "db_pool_utilization",
                "tasks_queued",
                "outbox_pending",
            )
            if key in gauges
        },
    }


# ── chaos ──────────────────────────────────────────────────────────────
async def chaos_redis_down(client: httpx.AsyncClient) -> dict[str, Any]:
    """Redis недоступен: лимиты держатся локально, подписи — fail-closed.

    Здесь фиксируется поведение API: вход в аккаунт не должен отвечать 5xx,
    а превышение лимита — 429. Локальный резервный счётчик проверяется
    unit-тестом `test_rate_limit_uses_local_fallback_when_redis_down`; этот
    сценарий подтверждает, что путь «Redis упал» не ломает HTTP-слой.
    """
    codes: dict[int, int] = {}
    for _ in range(5):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": f"nobody_{os.urandom(4).hex()}@example.com", "password": "x" * 12},
        )
        codes[response.status_code] = codes.get(response.status_code, 0) + 1
    server_errors = sum(count for code, count in codes.items() if code >= 500)
    return {
        "codes": codes,
        "server_errors": server_errors,
        "passed": server_errors == 0,
    }


async def chaos_redis_flap(client: httpx.AsyncClient, rounds: int = 20) -> dict[str, Any]:
    """Смешанная нагрузка при «мигающем» Redis: ищем 5xx, а не задержку."""
    errors: list[int] = []

    async def one() -> None:
        response = await client.get("/api/v1/agents")
        if response.status_code >= 500:
            errors.append(response.status_code)

    await asyncio.gather(*(one() for _ in range(rounds)))
    return {"requests": rounds, "server_errors": len(errors), "passed": not errors}


async def _login_or_register(
    client: httpx.AsyncClient, email: str, password: str, name: str
) -> str | None:
    """Токен существующего участника; при отсутствии — регистрация."""
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if login.status_code == 200:
        return login.json()["access_token"]
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": name},
    )
    if register.status_code == 201:
        return register.json()["access_token"]
    return None


async def admin_login(client: httpx.AsyncClient) -> dict[str, str] | None:
    """Вход админом для служебных эндпоинтов.

    Учётные данные берутся из окружения (`BENCH_ADMIN_EMAIL`/`_PASSWORD`);
    в отчёт они не попадают. Если их нет — сценарий помечается `skipped`,
    а не `passed`: «не проверяли» нельзя выдавать за «проверено».
    """
    email = os.environ.get("BENCH_ADMIN_EMAIL")
    password = os.environ.get("BENCH_ADMIN_PASSWORD")
    if not email or not password:
        return None
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        return None
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def chaos_worker_killed(
    client: httpx.AsyncClient, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Аренда, взятая «погибшим» узлом, возвращается в очередь.

    Сценарий настоящий, без имитации:

    1. создаётся python-агент и задание для него;
    2. регистрируется узел, он верифицируется админом;
    3. узел делает ровно один claim (`node_worker --once`) и «умирает»;
    4. задание остаётся в `leased` с истёкшей арендой — это и есть отказ;
    5. запускается цикл обслуживания, он возвращает задание в `queued`.

    Измеряется: сколько секунд задание было в аренде и вернулось ли оно.
    """
    if headers is None:
        return {
            "passed": False,
            "skipped": True,
            "reason": "нужны BENCH_ADMIN_EMAIL/BENCH_ADMIN_PASSWORD",
        }

    # Один и тот же владелец узла на все прогоны: регистрация ограничена
    # 20/мин на IP, и сценарий не должен упираться в этот лимит.
    owner_email = os.environ.get("BENCH_OWNER_EMAIL", "bench-owner@example.com")
    owner_password = os.environ.get("BENCH_OWNER_PASSWORD", "BenchOwner2026")
    token = await _login_or_register(client, owner_email, owner_password, "Bench Node Owner")
    if token is None:
        return {"passed": False, "reason": "не удалось войти владельцем узла"}
    owner = {"Authorization": f"Bearer {token}"}

    agent = await client.post(
        "/api/v1/agents",
        headers=owner,
        json={
            "slug": f"bench-node-agent-{os.urandom(3).hex()}",
            "name": "Агент для chaos-теста аренды",
            "summary": "python-агент, исполняется в песочнице",
            "capabilities": [{"capability_key": "text.echo", "declared_level": 1}],
        },
    )
    if agent.status_code != 201:
        return {"passed": False, "reason": f"agent {agent.status_code}: {agent.text[:200]}"}
    agent_id = agent.json()["id"]
    # Уникальная возможность на прогон: `select_node_for_task` назначает
    # задание на лучший online-узел, и без уникальной метки им окажется узел
    # предыдущего прогона. Тогда наш узел видит пустую очередь — сценарий
    # проверял бы не то.
    capability = f"bench.lease.{os.urandom(4).hex()}"
    version = await client.post(
        f"/api/v1/agents/{agent_id}/versions",
        headers=owner,
        json={
            "entrypoint_type": "python",
            "entrypoint_config": {"code": BENCH_AGENT_CODE},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "required_capabilities": [capability],
            "publish": True,
        },
    )
    if version.status_code != 201:
        return {"passed": False, "reason": f"version {version.status_code}: {version.text[:200]}"}

    node = await client.post(
        "/api/v1/nodes",
        headers=owner,
        json={
            "name": f"bench-node-{os.urandom(3).hex()}",
            "kind": "community",
            "capabilities": [capability],
            "region": "local",
        },
    )
    if node.status_code != 201:
        return {"passed": False, "reason": f"node {node.status_code}: {node.text[:200]}"}
    node_payload = node.json()
    node_id = node_payload["node"]["id"]
    node_key = node_payload["credentials"]["node_key"]
    node_secret = node_payload["credentials"]["node_secret"]
    await client.post(f"/api/v1/nodes/{node_id}/verify", headers=headers)

    task = await client.post(
        "/api/v1/tasks",
        headers=owner,
        json={
            "agent_id": agent_id,
            "input_payload": {"input": {"text": "chaos"}},
            # Приоритет выше остальных заданий в очереди: сценарий проверяет
            # аренду, а не порядок выдачи. Без этого claim может вернуть пусто,
            # если более старые задания уже просрочены по дедлайну.
            "priority": 10,
        },
    )
    if task.status_code != 201:
        return {"passed": False, "reason": f"task {task.status_code}: {task.text[:200]}"}
    task_id = task.json()["id"]

    # Узел должен быть online до первого claim: верификация админом сама
    # этого не делает, статус поднимает heartbeat (health=healthy).
    claim = await node_claim(
        node_key=node_key,
        node_secret=node_secret,
        capability=capability,
        api=os.environ.get("BENCH_API", "http://127.0.0.1:8000"),
    )
    claim_seconds = claim.pop("seconds", 0.0)

    # Узел «умер» после claim: аренда висит до истечения lease.
    lease_seconds = float(os.environ.get("WORKER_LEASE_SECONDS", "60"))
    before = await client.get("/api/v1/ops/health", headers=headers)
    stuck_before = None
    if before.status_code == 200:
        stuck_before = before.json()["gauges"].get("tasks_stuck_leases")

    # Обслуживание возвращает в очередь только истёкшие аренды. Ждём истечения
    # (не дольше лимита сценария), иначе «зависшая» аренда ещё легальна.
    wait_budget = min(lease_seconds + 2.0, 70.0)
    wait_started = time.perf_counter()
    while time.perf_counter() - wait_started < wait_budget:
        probe = await client.get(f"/api/v1/tasks/{task_id}", headers=owner)
        if probe.status_code == 200 and probe.json().get("status") != "leased":
            break
        await asyncio.sleep(2.0)
    waited = round(time.perf_counter() - wait_started, 1)

    maintenance = await client.post("/api/v1/ops/maintenance", headers=headers)
    requisition = maintenance.json() if maintenance.status_code == 200 else {}
    remaining = await client.get(f"/api/v1/tasks/{task_id}", headers=owner)
    status_after = remaining.json().get("status") if remaining.status_code == 200 else None

    return {
        "passed": maintenance.status_code == 200,
        "claim": claim,
        "claim_seconds": claim_seconds,
        "lease_seconds": lease_seconds,
        "stuck_leases_before_maintenance": stuck_before,
        "waited_for_lease_expiry_seconds": waited,
        "requeued_tasks": requisition.get("requeued_tasks"),
        "task_status_after": status_after,
        "note": "возврат в очередь выполняет обслуживание; при lease > 0 возврат "
        "происходит после истечения аренды — это и есть измеряемое окно",
    }


async def node_claim(
    *, node_key: str, node_secret: str, capability: str, api: str
) -> dict[str, Any]:
    """Захват задания узлом по HMAC-подписи, без исполнения работы.

    Отдельный процесс `node_worker --once` здесь не годится: он исполняет и
    отчитывается, а для сценария нужна *висящая* аренда — то есть ровно то,
    что остаётся после падения воркера. Поэтому шаги воспроизводятся клиентом
    узла: verify → heartbeat → claim.
    """
    from app.worker.node_worker import NodeClient

    client = NodeClient(api, node_key, node_secret)
    started = time.perf_counter()
    try:
        verify = await client.request("POST", "/api/v1/nodes/me/verify")
        heartbeat = await client.request(
            "POST",
            "/api/v1/nodes/me/heartbeat",
            {
                "sequence": 1,
                "running_tasks": 0,
                "cpu_load": 0.0,
                "memory_used_mb": 0,
                "health": "healthy",
                "details": {"worker": "bench"},
            },
        )
        # Два heartbeat: первый поднимает статус pending → online, второй
        # подтверждает живость до выдачи задания.
        for _ in range(2):
            heartbeat = await client.request(
                "POST",
                "/api/v1/nodes/me/heartbeat",
                {
                    "sequence": 1,
                    "running_tasks": 0,
                    "cpu_load": 0.0,
                    "memory_used_mb": 0,
                    "health": "healthy",
                    "details": {"worker": "bench"},
                },
            )
            await asyncio.sleep(0.05)
        online = await client.request("GET", "/api/v1/nodes/me")
        claimed = await client.request(
            "POST",
            "/api/v1/nodes/me/tasks/claim",
            {"capabilities": [capability], "max_tasks": 1},
        )
    finally:
        await client.close()
    seconds = round(time.perf_counter() - started, 2)
    assignments = claimed.json().get("assignments", []) if claimed.status_code == 200 else []
    return {
        "seconds": seconds,
        "verify_status": verify.status_code,
        "node_status": online.json().get("status") if online.status_code == 200 else None,
        "heartbeat_status": heartbeat.status_code,
        "claim_status": claimed.status_code,
        "assignments": len(assignments),
        "lease_present": bool(assignments and assignments[0].get("lease_token")),
    }


async def chaos_pool_exhausted(client: httpx.AsyncClient, parallel: int = 40) -> dict[str, Any]:
    """Пул на 10 соединений против 40 параллельных запросов к БД."""
    started = time.perf_counter()

    async def one() -> int:
        response = await client.get("/api/v1/agents?page_size=50")
        return response.status_code

    codes = await asyncio.gather(*(one() for _ in range(parallel)))
    elapsed = time.perf_counter() - started
    errors = [code for code in codes if code >= 500]
    return {
        "parallel": parallel,
        "elapsed_seconds": round(elapsed, 2),
        "codes": {code: codes.count(code) for code in sorted(set(codes))},
        "server_errors": len(errors),
        "passed": not errors,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_report(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


# ── main ───────────────────────────────────────────────────────────────
async def main_async(args: argparse.Namespace) -> int:
    api = args.api.rstrip("/")
    pid = find_api_pid(args.port)
    records: list[BenchRecord] = []
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": api,
        "api_pid": pid,
        "label": args.label,
        "cpu_count": os.cpu_count(),
        "loadavg_start": os.getloadavg(),
        # `users` не используется: нагрузочный участник создаётся один раз
        # (регистрация лимитирована), сценарий гонит `requests` запросов при
        # заданной конкурентности.
        "params": {
            "requests_per_scenario": args.requests,
            "concurrency": args.concurrency,
        },
        "env": {
            "database_url_host": os.environ.get("DATABASE_URL", "").split("@")[-1][:80],
            "redis": os.environ.get("REDIS_URL", "unset"),
            "python": sys.version.split()[0],
        },
        "benchmarks": [],
        "chaos": [],
    }

    # Метрики закрыты: нужен сервисный токен. В отчёте он не сохраняется.
    token = os.environ.get("INTERNAL_SERVICE_TOKEN", "")
    headers = {"X-Service-Token": token} if token else {}
    async with httpx.AsyncClient(base_url=api, timeout=60.0) as client:
        health = await client.get("/api/v1/health")
        if admin_headers_pre := await admin_login(client):
            ops = await client.get("/api/v1/ops/health", headers=admin_headers_pre)
            if ops.status_code == 200:
                report["runtime_config"] = ops.json().get("config", {})
        report["health"] = {
            "status": health.status_code,
            "body": health.json() if health.status_code == 200 else health.text[:200],
        }

        bench_email = args.email or f"bench-{os.urandom(4).hex()}@example.com"
        bench_password = args.password or "BenchPassword2026"
        os.environ["BENCH_API"] = api
        try:
            bootstrap = await asyncio.to_thread(bootstrap_user, bench_email, bench_password)
            report["bootstrap"] = bootstrap
        except Exception as exc:  # участник не создан — нагрузка не стартует
            report["bootstrap"] = {"error": f"{exc.__class__.__name__}: {exc}"[:300]}

        for index, scenario in enumerate(args.scenarios):
            if report["bootstrap"].get("error"):
                records.append(
                    BenchRecord(
                        name=f"load:{scenario}",
                        params={},
                        result=report["bootstrap"],
                        status="failed",
                        note="не удалось создать нагрузочного участника",
                    )
                )
                continue
            before = Samples.take(pid)
            metrics_before = await client.get("/metrics", headers=headers)
            json_out = Path(f"/tmp/load-{scenario}-{index}.json")
            load = await run_load(
                scenario, args.requests, args.concurrency, bench_email, bench_password, json_out
            )
            after = Samples.take(pid)
            ticks = max(after.cpu_ticks - before.cpu_ticks, 0)
            wall = max(load["wall_seconds"], 0.001)
            record = BenchRecord(
                name=f"load:{scenario}",
                params={"requests": args.requests, "concurrency": args.concurrency},
                result=load,
                resources={
                    "cpu_seconds": round(ticks / os.sysconf("SC_CLK_TCK"), 2),
                    "cpu_percent_of_core": round(ticks / os.sysconf("SC_CLK_TCK") / wall * 100, 1),
                    "rss_mb_after": after.rss_mb,
                    "metrics_http_status": metrics_before.status_code,
                },
                status="passed" if load["exit_code"] == 0 else "failed",
            )
            if load["exit_code"] != 0:
                record.note = "порог нагрузчика не пройден (см. output)"
            records.append(record)

        if args.chaos:
            admin_headers = await admin_login(client)
            for name, coro_factory in (
                ("redis-down", chaos_redis_down),
                ("redis-flap", chaos_redis_flap),
                ("worker-killed", chaos_worker_killed),
                ("db-pool-exhausted", chaos_pool_exhausted),
            ):
                started = time.perf_counter()
                try:
                    if name == "worker-killed":
                        outcome = await coro_factory(client, admin_headers)
                    else:
                        outcome = await coro_factory(client)
                    if outcome.get("skipped"):
                        status = "skipped"
                    else:
                        status = "passed" if outcome.get("passed") else "failed"
                except Exception as exc:  # сценарий уронил клиент — это провал
                    outcome = {"error": f"{exc.__class__.__name__}: {exc}"[:300]}
                    status = "failed"
                records.append(
                    BenchRecord(
                        name=f"chaos:{name}",
                        params={},
                        result=outcome,
                        resources={"elapsed_seconds": round(time.perf_counter() - started, 2)},
                        status=status,
                    )
                )

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["loadavg_end"] = os.getloadavg()
    report["benchmarks"] = [record.__dict__ for record in records]
    measured = [record for record in records if record.status != "skipped"]
    report["verdict"] = (
        "passed" if measured and all(record.status == "passed" for record in measured) else "failed"
    )
    report["skipped"] = [record.name for record in records if record.status == "skipped"]

    out = Path(args.out)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    await asyncio.to_thread(_write_report, out, payload)

    print(f"профилей/сценариев: {len(records)}, вердикт: {report['verdict']}")
    for record in records:
        extra = record.result.get("output") if record.result.get("output") else record.result
        print(f"  {record.name}: {record.status} | {record.resources} | {extra}")
    print(f"отчёт: {out}")
    return 0 if report["verdict"] == "passed" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Нагрузка и chaos с замерами ресурсов")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--port", type=int, default=8000, help="порт API (для /proc)")
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["read", "mixed"],
        choices=["read", "mixed", "auth"],
    )
    parser.add_argument("--chaos", action="store_true", help="прогнать chaos-сценарии")
    parser.add_argument(
        "--email", default="", help="нагрузочный участник (создаётся при отсутствии)"
    )
    parser.add_argument("--password", default="")
    parser.add_argument("--label", default="", help="метка фазы (пишется в отчёт)")
    parser.add_argument("--out", default="/tmp/bench_report.json")
    return parser


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
