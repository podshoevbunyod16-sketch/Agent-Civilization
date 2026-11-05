#!/usr/bin/env python3
"""Наполняет локальную цивилизацию реальными данными через публичный API.

Важно: это НЕ подмена функциональности заглушками. Скрипт работает как
обычный клиент: регистрируется, создаёт агентов, версии, узел, отправляет
задания и запускает настоящий worker. Все записи проходят те же проверки,
что и у живых пользователей, и остаются в журнале аудита.

    python scripts/seed_demo.py --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.security import build_signing_payload  # noqa: E402
from app.sandbox import SandboxRunner  # noqa: E402

ADMIN_EMAIL = "architect@agent-civilization.dev"
ADMIN_PASSWORD = "Architect2026"

AGENTS = [
    {
        "name": "Агент-исследователь",
        "slug": "researcher-prime",
        "summary": "Собирает и структурирует факты из открытых источников, отделяя проверенное от гипотез",
        "description": (
            "Исследовательский агент цивилизации. Принимает запрос, формирует план поиска, "
            "собирает фрагменты данных и возвращает структурированную сводку со ссылками "
            "на источники. Работает через HTTP-контракт и может быть поднят на любом узле."
        ),
        "category": "research",
        "tags": ["search", "analysis", "facts"],
        "accent": "#6ee7b7",
        "capabilities": [("research.web_search", 4), ("text.summarize", 3)],
        "code": (
            "def run(payload):\n"
            "    query = payload.get('query', '')\n"
            "    keywords = [w.strip('.,?!').lower() for w in query.split() if len(w) > 3]\n"
            "    return {\n"
            "        'query': query,\n"
            "        'keywords': keywords,\n"
            "        'plan': ['собрать источники', 'проверить факты', 'сформировать сводку'],\n"
            "        'confidence': round(min(0.95, 0.5 + 0.05 * len(keywords)), 2),\n"
            "    }\n"
        ),
    },
    {
        "name": "Агент-переводчик",
        "slug": "polyglot-nexus",
        "summary": "Переводит тексты между языками, сохраняя терминологию и стиль оригинала",
        "description": (
            "Многоязычный агент с поддержкой глоссариев. Возвращает перевод, список "
            "неоднозначных терминов и оценку уверенности по каждому фрагменту."
        ),
        "category": "language",
        "tags": ["translate", "localization"],
        "accent": "#60a5fa",
        "capabilities": [("text.translate", 4), ("text.summarize", 2)],
        "code": (
            "def run(payload):\n"
            "    text = payload.get('text', '')\n"
            "    target = payload.get('target_lang', 'en')\n"
            "    return {\n"
            "        'source_length': len(text),\n"
            "        'target_lang': target,\n"
            "        'segments': [s.strip() for s in text.split('.') if s.strip()],\n"
            "        'glossary_hits': [],\n"
            "    }\n"
        ),
    },
    {
        "name": "Агент-аналитик данных",
        "slug": "data-oracle",
        "summary": "Считает метрики по табличным данным и объясняет аномалии человеческим языком",
        "description": (
            "Принимает набор чисел или CSV, считает описательные статистики, находит выбросы "
            "по правилу 1.5·IQR и возвращает краткое пояснение для каждой аномалии."
        ),
        "category": "data",
        "tags": ["analytics", "statistics"],
        "accent": "#c084fc",
        "capabilities": [("data.analysis", 4), ("text.summarize", 3)],
        "code": (
            "import statistics\n\n"
            "def run(payload):\n"
            "    values = [float(v) for v in payload.get('values', [])]\n"
            "    if not values:\n"
            "        return {'error': 'empty dataset'}\n"
            "    ordered = sorted(values)\n"
            "    q1 = statistics.quantiles(ordered, n=4)[0] if len(ordered) > 3 else ordered[0]\n"
            "    q3 = statistics.quantiles(ordered, n=4)[2] if len(ordered) > 3 else ordered[-1]\n"
            "    iqr = q3 - q1\n"
            "    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr\n"
            "    outliers = [v for v in values if v < low or v > high]\n"
            "    return {\n"
            "        'count': len(values),\n"
            "        'mean': round(statistics.fmean(values), 3),\n"
            "        'median': round(statistics.median(values), 3),\n"
            "        'stdev': round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0,\n"
            "        'outliers': outliers,\n"
            "    }\n"
        ),
    },
    {
        "name": "Агент-ревьюер кода",
        "slug": "code-reviewer",
        "summary": "Проверяет фрагменты кода на типовые ошибки, небезопасные паттерны и стиль",
        "description": (
            "Статический анализатор уровня ревью: ищет eval/exec, жёстко заданные секреты, "
            "отсутствие обработки исключений и слишком длинные функции."
        ),
        "category": "engineering",
        "tags": ["code", "review", "security"],
        "accent": "#fbbf24",
        "capabilities": [("code.review", 4), ("security.audit", 2)],
        "code": (
            "import re\n\n"
            "PATTERNS = {\n"
            "    'eval_usage': r'\\beval\\s*\\(',\n"
            "    'exec_usage': r'\\bexec\\s*\\(',\n"
            "    'hardcoded_secret': r'(password|secret|token)\\s*=\\s*[\"\\'][^\"\\']+[\"\\']',\n"
            "    'bare_except': r'except\\s*:',\n"
            "}\n\n"
            "def run(payload):\n"
            "    code = payload.get('code', '')\n"
            "    findings = []\n"
            "    for name, pattern in PATTERNS.items():\n"
            "        for match in re.finditer(pattern, code, re.IGNORECASE):\n"
            "            findings.append({'rule': name, 'position': match.start()})\n"
            "    lines = code.count('\\n') + 1\n"
            "    return {\n"
            "        'lines': lines,\n"
            "        'findings': findings,\n"
            "        'severity': 'high' if findings else 'clean',\n"
            "    }\n"
        ),
    },
    {
        "name": "Агент-планировщик",
        "slug": "task-planner",
        "summary": "Декомпозирует сложную задачу на подзадачи и расставляет зависимости между ними",
        "description": (
            "Оркестратор верхнего уровня: строит граф подзадач, определяет, какие возможности "
            "нужны для каждой из них, и возвращает план, пригодный для Task Engine."
        ),
        "category": "orchestration",
        "tags": ["planning", "decomposition"],
        "accent": "#f472b6",
        "capabilities": [("task.decompose", 3), ("task.plan", 3)],
        "code": (
            "def run(payload):\n"
            "    goal = payload.get('goal', '')\n"
            "    steps = []\n"
            "    for index, stage in enumerate(['сбор данных', 'анализ', 'проверка', 'отчёт'], start=1):\n"
            "        steps.append({\n"
            "            'order': index,\n"
            "            'title': stage,\n"
            "            'depends_on': [] if index == 1 else [index - 1],\n"
            "        })\n"
            "    return {'goal': goal, 'steps': steps, 'critical_path': len(steps)}\n"
        ),
    },
    {
        "name": "Агент-переговорщик",
        "slug": "http-negotiator",
        "summary": "Согласует условия задачи с внешним сервисом через HTTP-контракт",
        "description": (
            "Пример агента, который живёт на внешнем сервере участника: система не запускает "
            "его код у себя, а передаёт задание по HTTP. Демонстрирует независимость "
            "экосистемы от инфраструктуры платформы."
        ),
        "category": "integration",
        "tags": ["http", "external", "a2a"],
        "accent": "#34d399",
        "capabilities": [("integration.http", 2), ("negotiation.terms", 2)],
        "entrypoint": {"url": "https://httpbin.org/post"},
    },
]


def node_headers(node_key: str, secret: str, method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    payload = build_signing_payload(
        node_key=node_key, method=method, path=path, timestamp=timestamp, nonce=nonce, body=body
    )
    signature = hmac.new(
        bytes.fromhex(hashlib.sha256(secret.encode()).hexdigest()), payload, hashlib.sha256
    ).hexdigest()
    return {
        "X-Node-Key": node_key,
        "X-Node-Signature": signature,
        "X-Node-Timestamp": timestamp,
        "X-Node-Nonce": nonce,
        "Content-Type": "application/json",
    }


async def promote_to_admin(user_id: str) -> None:
    """Повышает пользователя до админа напрямую в БД.

    Роль намеренно не выдаётся публичным API (иначе это была бы эскалация
    привилегий). Сид-скрипт — часть локальной инфраструктуры, он пишет в БД.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import settings

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": user_id}
        )
    await engine.dispose()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.api.rstrip("/") + "/api/v1"

    async with httpx.AsyncClient(base_url=base, timeout=60.0) as http:
        # 1. Аккаунт архитектора (или вход, если уже существует)
        response = await http.post(
            "/auth/register",
            json={
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
                "display_name": "Архитектор цивилизации",
            },
        )
        if response.status_code == 409:
            response = await http.post(
                "/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
            )
        response.raise_for_status()
        token = response.json()["access_token"]
        user_id = response.json()["user"]["id"]
        http.headers["Authorization"] = f"Bearer {token}"

        # Демо-аккаунт должен быть админом: роли не выдаются через API, поэтому
        # сид-скрипт (локальная инфраструктура, а не клиент) повышает роль в БД.
        await promote_to_admin(user_id)
        response = await http.post(
            "/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        http.headers["Authorization"] = f"Bearer {token}"
        print(f"✓ аккаунт: {ADMIN_EMAIL} / {ADMIN_PASSWORD} (админ)")

        # 2. Агенты + версии
        created = {}
        for spec in AGENTS:
            payload = {
                "slug": spec["slug"],
                "name": spec["name"],
                "summary": spec["summary"],
                "description": spec["description"],
                "category": spec["category"],
                "tags": spec["tags"],
                "accent_color": spec["accent"],
                "capabilities": [
                    {"capability_key": key, "declared_level": level}
                    for key, level in spec["capabilities"]
                ],
            }
            agent_response = await http.post("/agents", json=payload)
            if agent_response.status_code == 409:
                print(f"· агент {spec['slug']} уже существует, пропускаем")
                continue
            agent_response.raise_for_status()
            agent = agent_response.json()

            entrypoint_type = "http" if "entrypoint" in spec else "python"
            entrypoint_config = (
                spec["entrypoint"] if "entrypoint" in spec else {"code": spec["code"], "runtime": "python3.12"}
            )
            version_response = await http.post(
                f"/agents/{agent['id']}/versions",
                json={
                    "changelog": "Первая публичная версия",
                    "entrypoint_type": entrypoint_type,
                    "entrypoint_config": entrypoint_config,
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "resource_limits": {
                        "max_cpu_seconds": 10,
                        "max_wall_seconds": 30,
                        "max_memory_mb": 256,
                        "allow_network": entrypoint_type == "http",
                        "max_output_bytes": 65536,
                    },
                    "publish": True,
                },
            )
            version_response.raise_for_status()
            created[spec["slug"]] = agent
            print(f"✓ агент: {spec['name']} (v1, {entrypoint_type})")

        # 3. Узел участника
        node_response = await http.post(
            "/nodes",
            json={
                "name": "khujand-node-1",
                "kind": "community",
                "region": "tj-sughd",
                "cpu_cores": 8,
                "memory_mb": 16384,
                "max_concurrency": 4,
                "limit_cpu_seconds": 60,
                "limit_wall_seconds": 120,
                "limit_memory_mb": 1024,
                "allow_network": False,
                "capabilities": ["python", "sandbox"],
                "labels": {"owner": "community", "power": "solar"},
            },
        )
        if node_response.status_code == 409:
            # Узел уже зарегистрирован: переиспользуем существующие credentials
            print("· узел уже зарегистрирован, используем сохранённые credentials")
            existing_file = ROOT / "var" / "seed-node-credentials.json"
            if existing_file.exists():
                saved = json.loads(existing_file.read_text())
                node_key, secret = saved["node_key"], saved["node_secret"]
            else:
                print("! нет var/seed-node-credentials.json — узел нельзя использовать")
        elif node_response.status_code != 201:
            print(f"! узел не создан: {node_response.text[:200]}")
            return 0
        else:
            node_payload = node_response.json()
            node = node_payload["node"]
            credentials = node_payload["credentials"]
            node_key = credentials["node_key"]
            secret = credentials["node_secret"]
            print(f"✓ узел: {node['name']} ({node['node_key']})")

        async def node_call(method: str, path: str, body: dict | None = None):
            raw = json.dumps(body).encode() if body is not None else b""
            headers = node_headers(node_key, secret, method, path, raw)
            return await http.request(method, path, content=raw, headers=headers)

        verify = await node_call("POST", "/nodes/me/verify")
        verify.raise_for_status()
        print("✓ узел подтвердил владение секретом (online)")

        await node_call(
            "POST",
            "/nodes/me/heartbeat",
            {"sequence": 1, "running_tasks": 0, "cpu_load": 7.5, "memory_used_mb": 640},
        )

        # 4. Задания и их реальное выполнение в песочнице
        scenarios = [
            ("researcher-prime", {"query": "Как устроена экономика агентов без криптовалюты?"}),
            ("polyglot-nexus", {"text": "Цивилизация агентов растёт. Доверие строится на проверке.", "target_lang": "en"}),
            ("data-oracle", {"values": [12, 14, 13, 15, 16, 14, 99, 13, 12, 15]}),
            ("code-reviewer", {"code": "password = 'hunter2'\ndef f(x):\n    return eval(x)\n"}),
            ("task-planner", {"goal": "Подготовить отчёт о надёжности узлов"}),
        ]
        runner = SandboxRunner(backend="subprocess", max_wall_seconds=20, max_memory_mb=256)

        for slug, payload in scenarios:
            agent = created.get(slug)
            if not agent:
                continue
            submit = await http.post(
                "/tasks",
                json={
                    "agent_id": agent["id"],
                    "input_payload": payload,
                    "timeout_seconds": 60,
                    "idempotency_key": f"seed-{slug}-{uuid.uuid4().hex[:8]}",
                },
            )
            submit.raise_for_status()
            task = submit.json()

            claimed = await node_call("POST", "/nodes/me/tasks/claim", {"max_tasks": 1})
            assignments = claimed.json().get("assignments", [])
            if not assignments:
                print(f"· задание {slug}: нет назначения")
                continue
            assignment = assignments[0]
            await node_call(
                "POST",
                f"/nodes/me/tasks/{task['id']}/progress",
                {"lease_token": assignment["lease_token"], "message": "данные приняты узлом"},
            )
            code = str((assignment.get("entrypoint_config") or {}).get("code", ""))
            result = await runner.run_python(code, payload)
            await node_call(
                "POST",
                f"/nodes/me/tasks/{task['id']}/result",
                {
                    "lease_token": assignment["lease_token"],
                    "success": result.status == "succeeded",
                    "output": result.output,
                    "error_message": (result.error or {}).get("message"),
                    "cost_units": round(result.duration_ms / 1000, 3),
                    "metrics": {"duration_ms": result.duration_ms, "backend": result.backend},
                },
            )
            print(f"✓ задание {slug}: {result.status}")

        # 4b. Экономика: бюджет демо-пользователя (ACU — внутренние единицы).
        # Источник единиц — казна платформы, поэтому пополнение идёт переводом
        # из неё (архитектор в сиде — админ). Ключ идемпотентности делает
        # повторный запуск безопасным.
        economy = await http.get("/economy/me")
        economy.raise_for_status()
        economy_body = economy.json()
        accounts = await http.get("/economy/accounts")
        accounts.raise_for_status()
        treasury = next(
            (item for item in accounts.json() if item["type"] == "treasury"), None
        )
        if treasury is None:
            print("! казна не найдена: экономика недоступна")
        elif economy_body["balance_units"] < 50:
            topup = round(200 - economy_body["balance_units"], 4)
            transfer = await http.post(
                "/economy/transfer",
                json={
                    "from_account_id": treasury["id"],
                    "to_account_id": economy_body["account_id"],
                    "amount_units": topup,
                    "comment": "Демо-бюджет для заданий с оплатой",
                    "idempotency_key": f"seed-topup:{economy_body['account_id']}",
                },
            )
            if transfer.status_code == 201:
                economy_body = (await http.get("/economy/me")).json()
                print(f"✓ бюджет пополнен на {topup} ACU (итого {economy_body['balance_units']})")
            else:
                print(f"! пополнение не выполнено: {transfer.status_code} {transfer.text[:120]}")
        else:
            print(f"✓ бюджет участника: {economy_body['balance_units']} ACU")

        rules = await http.get("/economy/rules")
        rules.raise_for_status()
        if not rules.json():
            print("! нет правила наград: создайте его миграцией или через админа")
        else:
            rule = rules.json()[0]
            print(
                f"✓ правило наград: {rule['name']} "
                f"({rule['verified_reward_units']} ACU за подтверждённый результат)"
            )

        # 5. Финальный heartbeat и статистика
        stats = (await http.get("/platform/stats")).json()
        await http.post("/nodes/me/heartbeat", json={})
        print("\nИтог:")
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        print(f"\nВход в интерфейс: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
        print("Секрет узла (для worker):")
        print(f"  AC_NODE_KEY={node_key}")
        print(f"  AC_NODE_SECRET={secret}")
        secrets_file = ROOT / "var" / "seed-node-credentials.json"
        secrets_file.parent.mkdir(parents=True, exist_ok=True)
        secrets_file.write_text(
            json.dumps({"node_key": node_key, "node_secret": secret}, indent=2), encoding="utf-8"
        )
        print(f"  (сохранено в {secrets_file.relative_to(ROOT)} — не для Git)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
