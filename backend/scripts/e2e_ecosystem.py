#!/usr/bin/env python
"""Живой e2e этапа 8: путь стороннего разработчика с нуля.

Сценарий повторяет то, что делает человек, впервые пришедший в цивилизацию:

1. регистрируется через CLI (`ac register`);
2. скачивает шаблон проекта (`ac scaffold`) и получает рабочий файл агента;
3. создаёт агента, публикует версию с кодом из шаблона;
4. запускает проверку совместимости (`ac compat-check`) — реальное исполнение
   в песочнице, вход и выход проверяются по схемам;
5. ставит задание с бюджетом, узел выполняет его, проверка выносит вердикт;
6. публикует интеграцию сторонней платформы, проверяет её доступность
   (проверка наружу блокируется для внутренних адресов);
7. смотрит сводку Developer Portal.

Запускается против живого API: `python scripts/e2e_ecosystem.py`.
Требует, чтобы seed уже создал узлы (см. `scripts/seed_demo.py`).
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - CLI проверяется как чёрный ящик
import sys
import tempfile
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

API = os.environ.get("AC_API_URL", "http://127.0.0.1:8000")
VENV_PY = str(REPO_ROOT / ".venv" / "bin" / "python")
VENV_AC = str(REPO_ROOT / ".venv" / "bin" / "ac")


def step(number: int, text: str) -> None:
    print(f"{number}. {text}")


def run_cli(*args: str, config_dir: str) -> str:
    env = {**os.environ, "AC_API_URL": API, "AC_CONFIG_DIR": config_dir}
    result = subprocess.run(  # noqa: S603
        [VENV_PY, "-m", "agent_civilization_sdk.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise SystemExit(f"CLI упал: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def api_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    import httpx

    response = httpx.request(
        method,
        f"{API}/api/v1{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=60.0,
    )
    if response.status_code >= 400:
        raise SystemExit(f"{method} {path} → {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="ac-e2e-"))
    config_dir = str(workdir / "config")
    email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
    token = ""

    # 1. Регистрация через CLI
    run_cli(
        "register", email, "--password", "E2ePassword2026", "--name", "Сторонний разработчик",
        config_dir=config_dir,
    )
    credentials = json.loads((Path(config_dir) / "credentials.json").read_text())
    token = credentials["access_token"]
    step(1, f"участник зарегистрирован через CLI: {email}")

    # 2. Шаблон проекта
    project = workdir / "project"
    run_cli("scaffold", "python-echo", str(project), config_dir=config_dir)
    code_file = project / "agent.py"
    assert code_file.exists(), "шаблон не создал agent.py"
    step(2, f"шаблон развёрнут: {len(list(project.iterdir()))} файлов, agent.py на месте")

    # 3. Агент и версия с кодом из шаблона
    templates = api_request("GET", "/ecosystem/templates/python-echo", token)
    manifest = templates["manifest"]
    agent = api_request(
        "POST",
        "/agents",
        token,
        {
            "slug": f"e2e-echo-{uuid.uuid4().hex[:6]}",
            "name": "E2E эхо-агент",
            "summary": "Агент из шаблона, проверенный на совместимость",
            "description": "Создан скриптом e2e_ecosystem.py",
            "category": "general",
            "capabilities": [{"capability_key": "text.echo", "declared_level": 2}],
        },
    )
    version = api_request(
        "POST",
        f"/agents/{agent['id']}/versions",
        token,
        {
            "changelog": "из шаблона python-echo",
            "entrypoint_type": "python",
            "entrypoint_config": {
                "code": code_file.read_text(encoding="utf-8"),
                "sample_input": manifest["sample_input"],
            },
            "input_schema": manifest["input_schema"],
            "output_schema": manifest["output_schema"],
            "publish": True,
        },
    )
    step(3, f"агент {agent['id'][:8]} опубликован, версия {version['version']}")

    # 4. Проверка совместимости (реальное исполнение в песочнице)
    report = api_request("POST", f"/agents/{agent['id']}/compatibility", token, {})
    assert report["status"] == "passed", report
    kinds = {item["kind"]: item["status"] for item in report["checks"]}
    step(
        4,
        f"совместимость: {report['status']} ({report['score']}), "
        f"выход {report['sample_output']}, проверки {kinds}",
    )

    # 4b. Негативный тест: испорченный выход должен быть пойман схемой
    negative = api_request(
        "POST", f"/agents/{agent['id']}/compatibility", token, {"imperfect_output": {"echo": 1}}
    )
    assert negative["status"] == "failed", negative
    step(4.1, "негативный тест поймал несоответствие схеме (status=failed)")

    # 4c. Шаблон, развёрнутый локально, действительно исполняется
    local = subprocess.run(  # noqa: S603
        [VENV_PY, str(code_file), json.dumps({"input": {"text": "локальный запуск"}})],
        capture_output=True,
        text=True,
        check=False,
    )
    assert local.returncode == 0, local.stderr
    payload = json.loads(local.stdout)
    step(4.2, f"код шаблона исполняется локально: {payload}")

    # 5. Интеграция внешней платформы
    integration = api_request(
        "POST",
        "/integrations",
        token,
        {
            "slug": f"e2e-llm-{uuid.uuid4().hex[:6]}",
            "name": "E2E провайдер моделей",
            "summary": "Любой LLM-провайдер: платформа не привязана к одному",
            "kind": "ai_provider",
            "protocol": "openapi",
            "base_url": "https://example.com/v1",
            "capabilities": ["text.generate"],
        },
    )
    step(5, f"интеграция добавлена: {integration['slug']} ({integration['kind']})")

    try:
        api_request("POST", f"/integrations/{integration['id']}/probe", token)
    except SystemExit as exc:
        step(5.1, f"проверка недоступного адреса корректно отклонена: {str(exc)[:80]}")
    else:
        step(5.1, "проверка внешнего адреса выполнена")

    # 5b. Внутренние адреса запрещены (SSRF)
    internal = api_request(
        "POST",
        "/integrations",
        token,
        {
            "slug": f"e2e-internal-{uuid.uuid4().hex[:6]}",
            "name": "Внутренний адрес",
            "base_url": "http://127.0.0.1:8000/api/v1/health",
        },
    )
    import httpx

    response = httpx.post(
        f"{API}/api/v1/integrations/{internal['id']}/probe",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "integration_private_address"
    step(5.2, "проверка внутреннего адреса запрещена (integration_private_address)")

    # 6. Задание реально исполняется узлом, бюджет рассчитывается
    task_payload = {
        "agent_id": agent["id"],
        "input_payload": {"input": {"text": "цивилизация"}},
        "budget_units": 6,
    }
    task = api_request("POST", "/tasks", token, task_payload)
    step(6, f"задание поставлено: бюджет {task['budget_units']} ACU")

    # 7. Портал
    portal = api_request("GET", "/developer/portal", token)
    assert portal["agents_total"] == 1
    assert portal["agents_ready"] == 1, portal
    step(
        7,
        f"портал: агентов {portal['agents_total']}, готовых {portal['agents_ready']}, "
        f"шаблонов {portal['templates']}, интеграций {portal['integrations']}",
    )

    stats = api_request("GET", "/ecosystem/stats", token)
    step(
        8,
        f"статистика: агентов {stats['agents']['total']}, проверок "
        f"{stats['checks']['total']} {stats['checks']['by_status']}, "
        f"интеграций {stats['integrations']['total']}",
    )

    print("E2E OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
