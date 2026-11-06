#!/usr/bin/env python
"""Нагрузочный тест (этап 9). Честный: меряет то, что делает сервер.

    python scripts/load_test.py --users 50 --requests 20 --concurrency 20

Сценарии:

* `read` — `GET /agents` (каталог с фильтрами) — типичная читающая нагрузка;
* `mixed` — 80 % чтений + 20 % записей (постановка задания) — реальный профиль;
* `auth` — вход (дорогой PBKDF2) — узкое место, о котором надо знать заранее.

Результат: пропускная способность, перцентили задержки и доля ошибок.
Печатается вердикт по порогам из `--p95`/`--min-rps`, чтобы тест можно было
использовать в CI как регресс-барьер, а не только как «интересный график».

Важно: тест **пишет** данные (регистрации, задания) и подметает за собой
только в части статусов — запускать на dev/staging, не на проде.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_API = "http://127.0.0.1:8000"


# Пороги по сценариям: вход — намеренно дорогая CPU-операция (PBKDF2, 260k
# раундов), поэтому у него своя планка. Сравнивать его с чтением каталога
# бессмысленно: это разные ресурсы (CPU против БД+сети).
THRESHOLDS: dict[str, dict[str, float]] = {
    "read": {"p95": 800.0, "min_rps": 40.0},
    "mixed": {"p95": 1200.0, "min_rps": 30.0},
    "auth": {"p95": 5000.0, "min_rps": 8.0},
}


@dataclass
class Outcome:
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    codes: dict[int, int] = field(default_factory=dict)

    def record(self, duration_ms: float, status: int) -> None:
        self.latencies.append(duration_ms)
        self.codes[status] = self.codes.get(status, 0) + 1
        if status >= 400:
            self.errors += 1

    def summary(self) -> dict[str, Any]:
        if not self.latencies:
            return {"count": 0}
        ordered = sorted(self.latencies)
        return {
            "count": len(ordered),
            "errors": self.errors,
            "rps": 0.0,
            "avg_ms": round(statistics.fmean(ordered), 2),
            "p50_ms": round(percentile(ordered, 50), 2),
            "p95_ms": round(percentile(ordered, 95), 2),
            "p99_ms": round(percentile(ordered, 99), 2),
            "max_ms": round(ordered[-1], 2),
            "codes": dict(sorted(self.codes.items())),
        }


def percentile(ordered: list[float], value: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round((value / 100) * (len(ordered) - 1))))
    return ordered[index]


async def _timed(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> int:
    response = await client.request(method, path, **kwargs)
    return response.status_code


async def ensure_user(
    client: httpx.AsyncClient, *, email: str | None = None, password: str | None = None
) -> tuple[str, str]:
    """Возвращает (token, agent_id) для участника с опубликованным агентом.

    Если email/пароль заданы (`--login`), участник создаётся один раз до
    нагрузки, иначе регистрируемся здесь. Причина: регистрация ограничена
    `RATE_LIMIT_AUTH_PER_MINUTE` (20/мин на IP), и нагрузочный профиль,
    который создаёт десятки пользователей, измеряет лимит, а не систему.
    """
    email = email or f"load-{uuid.uuid4().hex[:10]}@example.com"
    password = password or "LoadTestPassword2026"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": "Нагрузка"},
    )
    if response.status_code == 409:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
    response.raise_for_status()
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    agent = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "slug": f"load-{uuid.uuid4().hex[:8]}",
            "name": "Нагрузочный агент",
            "summary": "Агент для нагрузочного теста",
            "category": "general",
            "capabilities": [{"capability_key": "text.echo", "declared_level": 1}],
        },
    )
    agent.raise_for_status()
    agent_id = agent.json()["id"]
    version = await client.post(
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
    return token, agent_id


async def run_scenario(
    *,
    api: str,
    scenario: str,
    works: int,
    concurrency: int,
    token: str,
    agent_id: str,
    email: str = "",
) -> Outcome:
    outcome = Outcome()
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=api, timeout=60.0) as client:
        headers = {"Authorization": f"Bearer {token}"}

        async def one(index: int) -> None:
            async with semaphore:
                started = time.perf_counter()
                try:
                    if scenario == "auth":
                        status = await _timed(
                            client,
                            "POST",
                            "/api/v1/auth/login",
                            json={"email": email, "password": _LOAD_PASSWORD},
                        )
                    elif scenario == "mixed" and index % 5 == 0:
                        status = await _timed(
                            client,
                            "POST",
                            "/api/v1/tasks",
                            headers=headers,
                            json={
                                "agent_id": agent_id,
                                "input_payload": {"input": {"text": "нагрузка"}},
                            },
                        )
                    else:
                        status = await _timed(
                            client,
                            "GET",
                            "/api/v1/agents",
                            headers=headers,
                            params={"page_size": 20, "sort": "reputation"},
                        )
                except Exception:
                    outcome.errors += 1
                    outcome.latencies.append((time.perf_counter() - started) * 1000)
                    return
                outcome.record((time.perf_counter() - started) * 1000, status)

        started = time.perf_counter()
        await asyncio.gather(*(one(index) for index in range(works)))
        elapsed = time.perf_counter() - started

    summary = outcome.summary()
    summary["rps"] = round(len(outcome.latencies) / elapsed, 2) if elapsed else 0.0
    summary["elapsed_seconds"] = round(elapsed, 2)
    return summary


_LOAD_PASSWORD = "LoadTestPassword2026"


async def main_async(args: argparse.Namespace) -> int:
    global _LOAD_PASSWORD
    works = args.users * args.requests
    async with httpx.AsyncClient(base_url=args.api, timeout=60.0) as client:
        try:
            await client.get("/api/v1/health")
        except httpx.HTTPError as exc:
            print(f"API недоступен: {exc}", file=sys.stderr)
            return 2
        if args.email:
            _LOAD_PASSWORD = args.password or _LOAD_PASSWORD
        token, agent_id = await ensure_user(
            client, email=args.email or None, password=args.password or None
        )
        me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        email = me.json()["email"]

    scenarios = [args.scenario] if args.scenario != "all" else ["read", "mixed", "auth"]
    failed = False
    summaries: dict[str, dict[str, Any]] = {}
    for name in scenarios:
        result = await run_scenario(
            api=args.api,
            scenario=name,
            works=works,
            concurrency=args.concurrency,
            token=token,
            agent_id=agent_id,
            email=email,
        )
        summaries[name] = result
        limits = THRESHOLDS.get(name, {})
        p95_limit = args.p95 if args.p95 is not None else limits.get("p95", 1500.0)
        rps_limit = args.min_rps if args.min_rps is not None else limits.get("min_rps", 10.0)
        verdict = "OK"
        if result["errors"]:
            verdict = "ERRORS"
            failed = True
        if result.get("p95_ms", 0) > p95_limit:
            verdict += f" (p95 > {p95_limit:g}ms)"
            failed = True
        if result.get("rps", 0) < rps_limit:
            verdict += f" (rps < {rps_limit:g})"
            failed = True
        print(
            f"[{name}] {result['count']} запросов за {result['elapsed_seconds']}с → "
            f"{result['rps']} rps, avg {result['avg_ms']}ms, p95 {result['p95_ms']}ms, "
            f"p99 {result['p99_ms']}ms, ошибок {result['errors']} {result['codes']} → {verdict}"
        )
    if args.json_out:
        from pathlib import Path

        payload = json.dumps(
            {
                "scenarios": summaries,
                "worked_requests": works,
                "concurrency": args.concurrency,
                "failed": failed,
            },
            ensure_ascii=False,
            indent=2,
        )
        await asyncio.to_thread(Path(args.json_out).write_text, payload, encoding="utf-8")
    print("LOAD " + ("FAIL" if failed else "OK"))
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Нагрузочный тест Agent Civilization")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--users", type=int, default=20, help="сколько «клиентов»")
    parser.add_argument("--requests", type=int, default=10, help="запросов на клиента")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--scenario", default="all", choices=["read", "mixed", "auth", "all"]
    )
    parser.add_argument(
        "--email",
        default="",
        help="существующий участник (иначе регистрируется новый; регистрация лимитирована)",
    )
    parser.add_argument("--password", default="")
    parser.add_argument("--json-out", default="", help="куда записать результат (JSON)")
    parser.add_argument(
        "--p95",
        type=float,
        default=None,
        help="порог p95 в мс (по умолчанию — по сценарию)",
    )
    parser.add_argument(
        "--min-rps",
        type=float,
        default=None,
        help="минимальный rps (по умолчанию — по сценарию)",
    )
    return parser


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
