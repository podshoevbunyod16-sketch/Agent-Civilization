"""Шаблоны проектов агентов (этап 8).

Шаблон — это не «заглушка для вида»: файлы реально отдаются Developer Portal,
а `python`-шаблоны дополнительно проходят проверку совместимости (их код
исполняется в песочнице с примером входа). Поэтому содержимое держится
минимальным, но рабочим.
"""

from __future__ import annotations

from typing import Any

PYTHON_ECHO = '''"""Агент-эхо: минимальный рабочий шаблон Agent Civilization."""

from __future__ import annotations

import json
import os


def run(payload: dict) -> dict:
    """Точка входа. Возвращает dict — он станет выходом задания."""
    text = (payload.get("input") or {}).get("text", "")
    return {"echo": text, "length": len(text)}


if __name__ == "__main__":
    # Локальный запуск: python agent.py '{"input": {"text": "привет"}}'
    import sys

    raw = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AC_PAYLOAD", "{}")
    print(json.dumps(run(json.loads(raw)), ensure_ascii=False))
'''

PYTHON_SUMMARIZER = '''"""Агент-суммаризатор: шаблон с несколькими возможностями."""

from __future__ import annotations

import re


def summarize(text: str, max_sentences: int = 3) -> dict:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\\s+", text) if s.strip()]
    head = sentences[:max_sentences]
    return {
        "summary": " ".join(head),
        "sentence_count": len(sentences),
        "char_count": len(text),
    }


def run(payload: dict) -> dict:
    body = payload.get("input") or {}
    return summarize(str(body.get("text", "")), int(body.get("max_sentences", 3)))
'''

PYTHON_VERIFIER = '''"""Независимый верификатор: проверяет чужой результат по схеме (этап 5)."""

from __future__ import annotations

REQUIRED = ("summary",)


def run(payload: dict) -> dict:
    candidate = payload.get("input") or payload.get("output") or {}
    missing = [key for key in REQUIRED if not candidate.get(key)]
    return {
        "passed": not missing,
        "missing": missing,
        "reason": "все обязательные поля на месте" if not missing else "не хватает полей",
    }
'''

HTTP_AGENT = '''"""HTTP-агент: контракт POST {base_url}, JSON → JSON."""

from __future__ import annotations

from typing import Any

import httpx

ANALYZE_URL = "https://example.com/api/analyze"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(ANALYZE_URL, json=payload, timeout=30.0)
    response.raise_for_status()
    return response.json()
'''

DOCKERFILE = '''FROM python:3.11-slim

WORKDIR /agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .

# Контракт: JSON в stdin, JSON в stdout
ENTRYPOINT ["python", "-c", "import json,sys,agent; print(json.dumps( \
    agent.run(json.load(sys.stdin))))"]
'''

MCP_SERVER = '''"""MCP-адаптер: публикует инструмент агента по протоколу MCP (JSON-RPC)."""

from __future__ import annotations

from typing import Any

TOOLS = [
    {
        "name": "echo",
        "description": "Возвращает переданный текст",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


async def handle(method: str, params: dict[str, Any]) -> Any:
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "echo":
            raise ValueError(f"unknown tool: {name}")
        return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "serverInfo": {"name": "ac-echo"}}
    raise ValueError(f"unknown method: {method}")
'''

REQUIREMENTS = "httpx>=0.27\n"
README_TMPL = """# {name}

Шаблон Agent Civilization: {summary}

## Запуск локально

```bash
pip install -r requirements.txt
python agent.py '{{"input": {{"text": "привет"}}}}'
```

## Регистрация в цивилизации

```bash
ac register --template {slug}
ac compat-check --agent <agent_id>
```
"""


def _python_template(
    *,
    slug: str,
    name: str,
    summary: str,
    entrypoint: str,
    capabilities: list[dict[str, Any]],
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    sample_input: dict[str, Any],
    test_code: str | None = None,
) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": name,
        "summary": summary,
        "runtime": "python",
        "language": "python",
        "files": [
            {"path": "agent.py", "language": "python", "content": entrypoint},
            {"path": "requirements.txt", "language": "text", "content": REQUIREMENTS},
            {
                "path": "README.md",
                "language": "markdown",
                "content": README_TMPL.format(name=name, summary=summary, slug=slug),
            },
            {
                "path": "manifest.json",
                "language": "json",
                "content": (
                    '{\n'
                    f'  "name": "{name}",\n'
                    f'  "entrypoint_type": "python",\n'
                    '  "entrypoint_config": {"code_file": "agent.py"},\n'
                    f'  "capabilities": {capabilities!r},\n'
                    '  "resource_limits": {"max_wall_seconds": 30, "max_memory_mb": 256}\n'
                    "}\n"
                ).replace("'", '"'),
            },
        ],
        "manifest": {
            "entrypoint_type": "python",
            "entrypoint_config": {"code_file": "agent.py"},
            "input_schema": input_schema,
            "output_schema": output_schema,
            "capabilities": capabilities,
            "resource_limits": {
                "max_wall_seconds": 30,
                "max_memory_mb": 256,
                "max_output_bytes": 65536,
            },
            "sample_input": sample_input,
            "test_code": test_code,
        },
        "requires_capabilities": [item["key"] for item in capabilities],
    }


def _http_template() -> dict[str, Any]:
    capabilities = [{"key": "text.analyze", "level": 1}]
    return {
        "slug": "http-agent",
        "name": "HTTP-агент",
        "summary": "Внешний сервис за HTTP: JSON на входе, JSON на выходе",
        "runtime": "http",
        "language": "python",
        "files": [
            {"path": "agent.py", "language": "python", "content": HTTP_AGENT},
            {"path": "requirements.txt", "language": "text", "content": REQUIREMENTS},
            {
                "path": "manifest.json",
                "language": "json",
                "content": (
                    '{\n'
                    '  "entrypoint_type": "http",\n'
                    '  "entrypoint_config": {"url": "https://example.com/api/analyze",\n'
                    '                         "method": "POST"},\n'
                    '  "input_schema": {"type": "object", "required": ["input"]},\n'
                    '  "output_schema": {"type": "object", "required": ["result"]}\n'
                    "}\n"
                ),
            },
        ],
        "manifest": {
            "entrypoint_type": "http",
            "entrypoint_config": {"url": "https://example.com/api/analyze", "method": "POST"},
            "input_schema": {"type": "object", "required": ["input"]},
            "output_schema": {"type": "object", "required": ["result"]},
            "capabilities": capabilities,
            "sample_input": {"input": {"text": "проверка"}},
        },
        "requires_capabilities": ["text.analyze"],
    }


def _docker_template() -> dict[str, Any]:
    capabilities = [{"key": "text.echo", "level": 1}]
    return {
        "slug": "docker-agent",
        "name": "Docker-агент",
        "summary": "Изолированный контейнер: JSON в stdin, JSON в stdout",
        "runtime": "docker",
        "language": "python",
        "files": [
            {"path": "Dockerfile", "language": "dockerfile", "content": DOCKERFILE},
            {
                "path": "agent.py",
                "language": "python",
                "content": PYTHON_ECHO,
            },
            {"path": "requirements.txt", "language": "text", "content": REQUIREMENTS},
        ],
        "manifest": {
            "entrypoint_type": "docker",
            "entrypoint_config": {"image": "ac-agent-echo:latest", "network": "none"},
            "input_schema": {"type": "object", "required": ["input"]},
            "output_schema": {"type": "object", "required": ["echo"]},
            "capabilities": capabilities,
            "resource_limits": {"max_wall_seconds": 60, "max_memory_mb": 512},
            "sample_input": {"input": {"text": "проверка"}},
        },
        "requires_capabilities": ["text.echo"],
    }


def _mcp_template() -> dict[str, Any]:
    capabilities = [{"key": "mcp.tools", "level": 1}]
    return {
        "slug": "mcp-server",
        "name": "MCP-сервер",
        "summary": "Публикация инструментов агента по протоколу MCP (JSON-RPC)",
        "runtime": "mcp",
        "language": "python",
        "files": [
            {"path": "server.py", "language": "python", "content": MCP_SERVER},
            {
                "path": "manifest.json",
                "language": "json",
                "content": (
                    '{\n'
                    '  "entrypoint_type": "mcp",\n'
                    '  "entrypoint_config": {"url": "https://example.com/mcp"},\n'
                    '  "capabilities": [{"key": "mcp.tools", "level": 1}]\n'
                    "}\n"
                ),
            },
        ],
        "manifest": {
            "entrypoint_type": "mcp",
            "entrypoint_config": {"url": "https://example.com/mcp"},
            "capabilities": capabilities,
            "sample_input": {"input": {"text": "проверка"}},
        },
        "requires_capabilities": ["mcp.tools"],
    }


PROJECT_TEMPLATES: list[dict[str, Any]] = [
    _python_template(
        slug="python-echo",
        name="Python: эхо-агент",
        summary="Минимальный рабочий агент: принимает текст, возвращает его и длину",
        entrypoint=PYTHON_ECHO,
        capabilities=[{"key": "text.echo", "level": 1}],
        input_schema={
            "type": "object",
            "required": ["input"],
            "properties": {"input": {"type": "object"}},
        },
        output_schema={
            "type": "object",
            "required": ["echo", "length"],
            "properties": {"echo": {"type": "string"}, "length": {"type": "integer"}},
        },
        sample_input={"input": {"text": "привет"}},
        test_code=(
            "def run(payload):\n"
            "    out = payload.get('output') or {}\n"
            "    return {'passed': out.get('echo') == 'привет' and out.get('length') == 6}\n"
        ),
    ),
    _python_template(
        slug="python-summarizer",
        name="Python: суммаризатор",
        summary="Сжимает текст до нескольких предложений, объявляет две возможности",
        entrypoint=PYTHON_SUMMARIZER,
        capabilities=[
            {"key": "text.summarize", "level": 2},
            {"key": "text.analyze", "level": 1},
        ],
        input_schema={
            "type": "object",
            "required": ["input"],
            "properties": {"input": {"type": "object"}},
        },
        output_schema={
            "type": "object",
            "required": ["summary", "sentence_count"],
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "sentence_count": {"type": "integer", "minimum": 1},
            },
        },
        sample_input={"input": {"text": "Первое. Второе. Третье. Четвёртое."}},
    ),
    _python_template(
        slug="python-verifier",
        name="Python: независимый верификатор",
        summary="Проверяет результат другого агента и возвращает вердикт",
        entrypoint=PYTHON_VERIFIER,
        capabilities=[{"key": "verification.schema", "level": 1}],
        input_schema={"type": "object", "required": ["input"]},
        output_schema={
            "type": "object",
            "required": ["passed"],
            "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
        },
        sample_input={"input": {"summary": "готовый результат"}},
    ),
    _http_template(),
    _docker_template(),
    _mcp_template(),
]

TEMPLATES_BY_SLUG = {item["slug"]: item for item in PROJECT_TEMPLATES}


def get_template(slug: str) -> dict[str, Any] | None:
    return TEMPLATES_BY_SLUG.get(slug)


__all__ = ["PROJECT_TEMPLATES", "TEMPLATES_BY_SLUG", "get_template"]
