"""Пример: сторонний HTTP-агент подключается к цивилизации.

Показывает минимальный путь внешнего разработчика без Python-кода на
платформе: агент живёт отдельным процессом (здесь — на стандартной
библиотеке), платформа лишь знает его адрес и контракт.

    # 1. запустить агента
    python examples/external_agent.py

    # 2. зарегистрировать и опубликовать (в другом терминале)
    ac register dev@example.com --password 'SuperSecret123' --name "Команда"
    ac agent-create --slug external-echo --name "Внешний эхо-агент" \\
        --summary "Отвечает по HTTP-контракту" --capability text.echo
    ac version <agent_id> --entrypoint http --url http://127.0.0.1:8100/run --publish
    ac compat-check <agent_id>     # статическая проверка контракта
    ac submit <agent_id> payload.json --budget 2 --wait

Контракт HTTP-агента: POST на указанный url с телом `{"input": {...}}`,
ответ — JSON; поле `output` считается результатом, иначе весь объект.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8100


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - имя задано базовым классом
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        payload = body.get("input") or body
        text = str(payload.get("text", ""))
        result = {
            "output": {"echo": text, "length": len(text), "source": "external-http-agent"}
        }
        data = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:  # тише в консоли
        print(f"[external-agent] {fmt % args}")


if __name__ == "__main__":
    print(f"Внешний агент слушает http://127.0.0.1:{PORT}/run")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
