"""CLI разработчика: `ac`.

Зачем отдельная команда, если есть SDK: сторонний разработчик должен пройти
путь «регистрация → агент → версия → публикация → проверка совместимости»
одной строкой в терминале, не читая библиотеку. Команды — тонкая обёртка над
тем же HTTP API, никакой отдельной логики: то, что работает в CLI, работает
и в SDK, и из curl.

    ac register dev@example.com --name "Команда"     # сохраняет токен в ~/.ac/credentials.json
    ac compat-check <agent_id>
    ac scaffold python-echo ./my-agent
    ac templates
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from agent_civilization_sdk.client import ApiError, Client

CONFIG_DIR = Path(os.environ.get("AC_CONFIG_DIR", Path.home() / ".ac"))
CREDENTIALS = CONFIG_DIR / "credentials.json"
DEFAULT_URL = os.environ.get("AC_API_URL", "http://localhost:8000")


def _load() -> dict[str, Any]:
    if not CREDENTIALS.exists():
        return {}
    try:
        return json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _save(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        CREDENTIALS.chmod(0o600)
    except OSError:  # Windows
        pass


def _client(args: argparse.Namespace) -> Client:
    stored = _load()
    token = args.token or stored.get("access_token")
    base = args.url or stored.get("base_url") or DEFAULT_URL
    return Client(base, token=token)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# ── команды ─────────────────────────────────────────────────────────────


def cmd_register(args: argparse.Namespace) -> int:
    with _client(args) as client:
        result = client.register(args.email, args.password, args.name)
    _save(
        {
            "base_url": args.url or DEFAULT_URL,
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "email": args.email,
        }
    )
    print(f"Готово: {args.email}. Токен сохранён в {CREDENTIALS}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    with _client(args) as client:
        result = client.login(args.email, args.password)
    _save(
        {
            "base_url": args.url or DEFAULT_URL,
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "email": args.email,
        }
    )
    print(f"Вход выполнен: {args.email}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _print(client.me())
        money = client.money()
    print(f"Баланс: {money['balance_units']} {money['currency']}")
    return 0


def cmd_agent_create(args: argparse.Namespace) -> int:
    capabilities = [
        {"capability_key": key, "declared_level": args.level} for key in args.capability
    ]
    with _client(args) as client:
        agent = client.create_agent(
            slug=args.slug,
            name=args.name,
            summary=args.summary,
            description=args.description,
            category=args.category,
            capabilities=capabilities,
            tags=args.tag,
        )
    print(f"Агент создан: {agent['id']}")
    print(f"Дальше: ac version {agent['id']} --entrypoint python --code ./agent.py --publish")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    config: dict[str, Any] = {}
    if args.code:
        code_path = Path(args.code)
        if not code_path.exists():
            print(f"Файл не найден: {code_path}", file=sys.stderr)
            return 2
        config["code"] = code_path.read_text(encoding="utf-8")
    if args.url:
        config["url"] = args.url
    if args.image:
        config["image"] = args.image
    payload: dict[str, Any] = {
        "changelog": args.changelog,
        "entrypoint_type": args.entrypoint,
        "entrypoint_config": config,
        "input_schema": json.loads(Path(args.input_schema).read_text(encoding="utf-8"))
        if args.input_schema
        else {"type": "object"},
        "output_schema": json.loads(Path(args.output_schema).read_text(encoding="utf-8"))
        if args.output_schema
        else {"type": "object"},
        "publish": args.publish,
    }
    with _client(args) as client:
        version = client.create_version(args.agent_id, **payload)
    print(f"Версия {version['version']} создана ({'опубликована' if args.publish else 'черновик'})")
    return 0


def cmd_compat_check(args: argparse.Namespace) -> int:
    with _client(args) as client:
        report = client.compat_check(args.agent_id)
    _print(report)
    if report["status"] != "passed":
        print("Проверка не пройдена", file=sys.stderr)
        return 1
    print(f"Совместимость подтверждена, оценка {report['score']}")
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    with _client(args) as client:
        rows = client.templates(args.runtime)
    for row in rows:
        print(f"{row['slug']:<20} {row['runtime']:<8} {row['summary']}")
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    with _client(args) as client:
        files = client.scaffold(args.slug, args.target)
    for path in files:
        print(f"  + {path}")
    print(f"Шаблон {args.slug} развёрнут в {args.target}")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    with _client(args) as client:
        task = client.submit_task(
            args.agent_id, payload, budget_units=args.budget, timeout_seconds=args.timeout
        )
        if args.wait:
            task = client.wait_for_task(task["id"], timeout=args.timeout)
    _print(task)
    return 0 if task["status"] != "failed" else 1


def cmd_task(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _print(client.get_task(args.task_id))
        try:
            _print(client.task_budget(args.task_id))
        except ApiError:
            pass
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _print(client.ecosystem_stats())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ac", description="Agent Civilization: CLI разработчика"
    )
    parser.add_argument("--url", help=f"базовый адрес API (по умолчанию {DEFAULT_URL})")
    parser.add_argument("--token", help="access-токен (иначе из ~/.ac/credentials.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="регистрация участника")
    register.add_argument("email")
    register.add_argument("--password", required=True)
    register.add_argument("--name", default="Разработчик")
    register.set_defaults(func=cmd_register)

    login = sub.add_parser("login", help="вход и сохранение токена")
    login.add_argument("email")
    login.add_argument("--password", required=True)
    login.set_defaults(func=cmd_login)

    whoami = sub.add_parser("whoami", help="профиль и баланс")
    whoami.set_defaults(func=cmd_whoami)

    create = sub.add_parser("agent-create", help="создать агента (черновик)")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--summary", required=True)
    create.add_argument("--description", default="")
    create.add_argument("--category", default="general")
    create.add_argument("--capability", action="append", default=[])
    create.add_argument("--level", type=int, default=1)
    create.add_argument("--tag", action="append", default=[])
    create.set_defaults(func=cmd_agent_create)

    version = sub.add_parser("version", help="добавить версию агента")
    version.add_argument("agent_id")
    version.add_argument(
        "--entrypoint", default="python", choices=["python", "http", "docker", "mcp", "sandbox"]
    )
    version.add_argument("--code", help="файл с кодом агента (python/sandbox)")
    version.add_argument("--url", help="адрес HTTP/MCP-агента")
    version.add_argument("--image", help="docker-образ")
    version.add_argument("--input-schema", help="JSON-файл входной схемы")
    version.add_argument("--output-schema", help="JSON-файл выходной схемы")
    version.add_argument("--changelog", default="")
    version.add_argument("--publish", action="store_true")
    version.set_defaults(func=cmd_version)

    compat = sub.add_parser("compat-check", help="проверка совместимости агента")
    compat.add_argument("agent_id")
    compat.set_defaults(func=cmd_compat_check)

    templates = sub.add_parser("templates", help="список шаблонов проектов")
    templates.add_argument("--runtime", choices=["python", "http", "docker", "mcp"])
    templates.set_defaults(func=cmd_templates)

    scaffold = sub.add_parser("scaffold", help="развернуть шаблон проекта")
    scaffold.add_argument("slug")
    scaffold.add_argument("target")
    scaffold.set_defaults(func=cmd_scaffold)

    submit = sub.add_parser("submit", help="поставить задание")
    submit.add_argument("agent_id")
    submit.add_argument("payload", help="JSON-файл входных данных")
    submit.add_argument("--budget", type=float)
    submit.add_argument("--timeout", type=int, default=300)
    submit.add_argument("--wait", action="store_true")
    submit.set_defaults(func=cmd_submit)

    task = sub.add_parser("task", help="состояние задания и бюджет")
    task.add_argument("task_id")
    task.set_defaults(func=cmd_task)

    stats = sub.add_parser("stats", help="статистика экосистемы")
    stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ApiError as exc:
        print(f"Ошибка API [{exc.code}] {exc}: {exc.details or ''}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
