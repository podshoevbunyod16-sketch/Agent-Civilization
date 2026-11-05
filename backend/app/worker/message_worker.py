"""Воркер доставки A2A-сообщений.

Забирает сообщения из очереди и доставляет их согласно стратегии:

* `http`    — POST на endpoint агента участника (тело `{"input": payload}`);
* `sandbox` — исполняет python-агента в песочнице (лимиты из версии агента);
* `mcp`     — вызывает инструмент MCP-сервера по JSON-RPC;
* `inbox`   — доставка не требуется, сообщение остаётся доступным в inbox.

Гарантии: at-least-once, экспоненциальный backoff, `max_attempts`,
идемпотентность на уровне отправки, повторный захват «зависших» сообщений.

Запуск:

    python -m app.worker.message_worker --api-url http://localhost:8000 \
        --internal-token <SERVICE_TOKEN>

Воркер ходит во внутренний API (`/internal/messages/*`), защищённый
сервисным токеном: это единственный компонент, которому разрешено
выполнять сетевые вызовы и код агентов.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from typing import Any

import httpx

from app.core.config import settings
from app.sandbox import SandboxRunner, SandboxUnavailableError
from app.sandbox.mcp import McpClient, McpError

logger = logging.getLogger("message_worker")


class MessageDispatcher:
    """Доставляет одно сообщение конкретной стратегией."""

    def __init__(self, *, sandbox_backend: str | None = None, http_timeout: float = 30.0):
        self.sandbox_backend = sandbox_backend
        self.http_timeout = http_timeout

    async def deliver(self, message: dict[str, Any], assignment: dict[str, Any]) -> dict[str, Any]:
        strategy = message["strategy"]
        started = time.monotonic()
        try:
            if strategy == "http":
                payload, error = await self._deliver_http(assignment, message["payload"])
            elif strategy == "sandbox":
                payload, error = await self._deliver_sandbox(assignment, message["payload"])
            elif strategy == "mcp":
                payload, error = await self._deliver_mcp(assignment, message["payload"])
            elif strategy == "inbox":
                payload, error = {}, None
            else:  # pragma: no cover - защита от неизвестной стратегии
                payload, error = {}, {
                    "type": "UnsupportedStrategy",
                    "message": f"стратегия {strategy} не поддерживается",
                }
        except Exception as exc:  # сеть/песочница/парсинг
            payload, error = {}, {
                "type": exc.__class__.__name__,
                "message": str(exc)[:2000],
            }
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "success": error is None,
            "response_payload": payload,
            "error": error,
            "latency_ms": latency_ms,
        }

    async def _deliver_http(
        self, assignment: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        config = assignment.get("entrypoint_config") or {}
        url = str(config.get("url", ""))
        if not url.startswith(("http://", "https://")):
            return {}, {"type": "BadEndpoint", "message": "у агента нет корректного url"}
        headers = {"Content-Type": "application/json"}
        if config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        async with httpx.AsyncClient(timeout=self.http_timeout) as http:
            response = await http.post(url, json={"input": payload}, headers=headers)
        if response.status_code >= 400:
            return {}, {
                "type": "AgentHttpError",
                "message": f"HTTP {response.status_code}: {response.text[:300]}",
            }
        try:
            body = response.json()
        except ValueError:
            return {"raw": response.text[:4000]}, None
        if isinstance(body, dict):
            output = body.get("output", body)
            return (output if isinstance(output, dict) else {"result": output}), None
        return {"result": body}, None

    async def _deliver_sandbox(
        self, assignment: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        config = assignment.get("entrypoint_config") or {}
        code = str(config.get("code", ""))
        if not code.strip():
            return {}, {"type": "BadEndpoint", "message": "у агента нет кода"}
        runner = SandboxRunner.from_limits(assignment.get("resource_limits"))
        if self.sandbox_backend:
            runner.backend = self.sandbox_backend
        try:
            result = await runner.run_python(code, payload)
        except SandboxUnavailableError as exc:
            return {}, {"type": "SandboxUnavailable", "message": str(exc)}
        if result.status != "succeeded":
            return {}, {
                "type": (result.error or {}).get("type", "SandboxError"),
                "message": (result.error or {}).get("message", result.status),
            }
        return result.output, None

    async def _deliver_mcp(
        self, assignment: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        mcp = assignment.get("mcp") or {}
        server_url = mcp.get("server_url")
        if not server_url:
            return {}, {"type": "McpMissing", "message": "MCP-сервер не зарегистрирован"}
        tool_name = payload.get("tool") or payload.get("tool_name")
        arguments = payload.get("arguments") or payload.get("input") or {}
        if not tool_name:
            # Если инструмент не указан, отдаём список доступных
            async with McpClient(
                server_url, auth_token=mcp.get("auth_token"), timeout=self.http_timeout
            ) as client:
                await client.initialize()
                tools = await client.list_tools()
            return {"tools": tools}, None
        try:
            async with McpClient(
                server_url, auth_token=mcp.get("auth_token"), timeout=self.http_timeout
            ) as client:
                await client.initialize()
                result = await client.call_tool_as_json(str(tool_name), arguments)
        except McpError as exc:
            return {}, {"type": "McpError", "message": str(exc), "code": exc.code}
        if result["is_error"]:
            return {}, {"type": "McpToolError", "message": str(result["output"])[:1000]}
        return {"output": result["output"], "tool": tool_name}, None


class MessageWorker:
    """Цикл: забрать пачку сообщений → доставить → отчитаться."""

    def __init__(
        self,
        client: InternalClient,
        *,
        poll_interval: float = 1.0,
        batch_size: int = 10,
        dispatcher: MessageDispatcher | None = None,
    ) -> None:
        self.client = client
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.dispatcher = dispatcher or MessageDispatcher()
        self._running = True
        self.delivered = 0
        self.failed = 0

    def stop(self) -> None:
        self._running = False

    async def run_once(self) -> dict[str, int]:
        batch = await self.client.claim(limit=self.batch_size)
        delivered = failed = retried = 0
        for message in batch.get("messages", []):
            assignment = message.get("assignment") or {}
            outcome = await self.dispatcher.deliver(message, assignment)
            result = await self.client.report(
                message["id"],
                success=outcome["success"],
                response_payload=outcome["response_payload"],
                error=outcome["error"],
                latency_ms=outcome["latency_ms"],
            )
            if outcome["success"]:
                delivered += 1
            elif result.get("retry_scheduled"):
                retried += 1
            else:
                failed += 1
        self.delivered += delivered
        self.failed += failed
        return {"processed": len(batch.get("messages", [])), "delivered": delivered,
                "failed": failed, "retried": retried}

    async def run_forever(self) -> None:
        while self._running:
            try:
                outcome = await self.run_once()
                if outcome["processed"] == 0:
                    await asyncio.sleep(self.poll_interval)
            except httpx.HTTPError as exc:
                logger.warning("worker_http_error error=%s", exc)
                await asyncio.sleep(self.poll_interval)
        await self.client.close()
        logger.info("message_worker_stopped delivered=%s failed=%s", self.delivered, self.failed)


class InternalClient:
    """Клиент внутреннего API доставки (защищён сервисным токеном)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"X-Service-Token": token},
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def claim(self, *, limit: int = 10) -> dict[str, Any]:
        response = await self._http.post(
            f"{settings.api_prefix}/internal/messages/claim", json={"limit": limit}
        )
        response.raise_for_status()
        return response.json()

    async def report(
        self,
        message_id: str,
        *,
        success: bool,
        response_payload: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        response = await self._http.post(
            f"{settings.api_prefix}/internal/messages/{message_id}/result",
            json={
                "success": success,
                "response_payload": response_payload or {},
                "error": error,
                "latency_ms": latency_ms,
            },
        )
        response.raise_for_status()
        return response.json()

    async def heartbeat(self) -> dict[str, Any]:
        response = await self._http.get(f"{settings.api_prefix}/health")
        response.raise_for_status()
        return response.json()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Civilization message worker")
    parser.add_argument(
        "--api-url", default=os.environ.get("AC_API_URL", "http://localhost:8000")
    )
    parser.add_argument(
        "--internal-token", default=os.environ.get("AC_SERVICE_TOKEN")
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--sandbox-backend", default=None, choices=["subprocess", "docker", "noop"])
    parser.add_argument("--once", action="store_true", help="одна итерация и выход")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if not args.internal_token:
        print(
            "Нужен --internal-token (или AC_SERVICE_TOKEN): "
            "он должен совпадать с INTERNAL_SERVICE_TOKEN на сервере",
            file=__import__("sys").stderr,
        )
        return 2
    client = InternalClient(args.api_url, args.internal_token)
    worker = MessageWorker(
        client,
        poll_interval=args.poll_interval,
        batch_size=args.batch_size,
        dispatcher=MessageDispatcher(sandbox_backend=args.sandbox_backend),
    )
    if args.once:
        print(await worker.run_once())
        await client.close()
        return 0

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await worker.run_forever()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
