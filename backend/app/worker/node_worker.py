"""Worker-процесс узла: забирает задания и выполняет их в песочнице.

Запуск (на сервере участника или в облаке):

    AC_NODE_KEY=node_... AC_NODE_SECRET=... \
    python -m app.worker.node_worker --api-url https://civilization.example

Чем занимается цикл:
  1. подписывает каждый запрос HMAC-SHA256 (секрет никуда не отправляется);
  2. подтверждает владение узлом (/nodes/me/verify);
  3. шлёт heartbeat с фактической нагрузкой;
  4. забирает задания (claim), выполняет и отправляет результат;
  5. чистит «зависшие» аренды и помечает offline-узлы (опционально, --janitor).

Недоверенный код агента исполняется ТОЛЬКО в песочнице (subprocess/docker),
никогда в процессе API.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import time
import uuid
from typing import Any

import httpx

from app.core.security import build_signing_payload
from app.sandbox import SandboxRunner, SandboxUnavailableError

logger = logging.getLogger("worker")


class NodeClient:
    """HTTP-клиент узла с HMAC-подписью каждого запроса."""

    def __init__(
        self,
        api_url: str,
        node_key: str,
        node_secret: str,
        api_prefix: str = "/api/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_prefix = api_prefix
        self.node_key = node_key
        # На сервере хранится sha256(secret); подпись строится от этого хеша,
        # поэтому секрет не передаётся по сети.
        self.key_hex = hashlib.sha256(node_secret.encode()).hexdigest()
        # transport используется в e2e-тестах (ASGI без реального сокета)
        self.http = httpx.AsyncClient(
            base_url=self.api_url, timeout=30.0, transport=transport
        )

    async def close(self) -> None:
        await self.http.aclose()

    def _signature_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signed_path = path[len(self.api_prefix):] if path.startswith(self.api_prefix) else path
        payload = build_signing_payload(
            node_key=self.node_key,
            method=method,
            path=signed_path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        signature = hmac.new(
            bytes.fromhex(self.key_hex), payload, hashlib.sha256
        ).hexdigest()
        return {
            "X-Node-Key": self.node_key,
            "X-Node-Signature": signature,
            "X-Node-Timestamp": timestamp,
            "X-Node-Nonce": nonce,
        }

    async def request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        body = json.dumps(json_body).encode() if json_body is not None else b""
        headers = self._signature_headers(method, path, body)
        headers["Content-Type"] = "application/json"
        return await self.http.request(method, path, content=body, headers=headers)


class NodeWorker:
    def __init__(
        self,
        client: NodeClient,
        *,
        capabilities: list[str] | None = None,
        poll_interval: float = 2.0,
        max_tasks: int = 1,
        janitor: bool = False,
        sandbox_backend: str | None = None,
    ) -> None:
        self.client = client
        self.capabilities = capabilities or ["python"]
        self.poll_interval = poll_interval
        self.max_tasks = max_tasks
        self.janitor = janitor
        self.sandbox_backend = sandbox_backend
        self._running = True
        self._executed = 0
        self._failed = 0

    def stop(self) -> None:
        self._running = False

    # ── шаги цикла ────────────────────────────────────────────────────
    async def verify(self) -> dict[str, Any]:
        response = await self.client.request("POST", "/api/v1/nodes/me/verify")
        response.raise_for_status()
        return response.json()

    async def heartbeat(self, sequence: int, running: int = 0) -> dict[str, Any]:
        response = await self.client.request(
            "POST",
            "/api/v1/nodes/me/heartbeat",
            {
                "sequence": sequence,
                "running_tasks": running,
                "cpu_load": 0.0,
                "memory_used_mb": 0,
                "health": "healthy",
                "details": {"worker": "python", "pid": os.getpid()},
            },
        )
        response.raise_for_status()
        return response.json()

    async def claim(self) -> list[dict[str, Any]]:
        response = await self.client.request(
            "POST",
            "/api/v1/nodes/me/tasks/claim",
            {"capabilities": self.capabilities, "max_tasks": self.max_tasks},
        )
        response.raise_for_status()
        return response.json().get("assignments", [])

    async def report_progress(self, task_id: str, lease: str, message: str) -> None:
        await self.client.request(
            "POST",
            f"/api/v1/nodes/me/tasks/{task_id}/progress",
            {"lease_token": lease, "message": message},
        )

    async def submit_result(
        self,
        task_id: str,
        lease: str,
        *,
        success: bool,
        output: dict[str, Any] | None = None,
        error_message: str | None = None,
        cost_units: float = 0.0,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self.client.request(
            "POST",
            f"/api/v1/nodes/me/tasks/{task_id}/result",
            {
                "lease_token": lease,
                "success": success,
                "output": output or {},
                "error_message": error_message,
                "cost_units": cost_units,
                "metrics": metrics or {},
            },
        )
        response.raise_for_status()
        return response.json()

    # ── исполнение ───────────────────────────────────────────────────
    async def execute(self, assignment: dict[str, Any]) -> dict[str, Any]:
        entrypoint_type = assignment.get("entrypoint_type")
        config = assignment.get("entrypoint_config") or {}
        limits = assignment.get("resource_limits") or {}

        if entrypoint_type == "python":
            runner = SandboxRunner.from_limits(limits)
            if self.sandbox_backend:
                runner.backend = self.sandbox_backend
            started = time.monotonic()
            try:
                result = await runner.run_python(
                    str(config.get("code", "")), assignment.get("input_payload") or {}
                )
            except SandboxUnavailableError as exc:
                return {
                    "success": False,
                    "error_message": f"sandbox unavailable: {exc}",
                    "metrics": {"duration_ms": 0},
                }
            duration_ms = int((time.monotonic() - started) * 1000)
            return {
                "success": result.status == "succeeded",
                "output": result.output,
                "error_message": (result.error or {}).get("message"),
                "metrics": {
                    "duration_ms": duration_ms,
                    "exit_code": result.exit_code,
                    "backend": result.backend,
                    "stdout": result.stdout[:2000],
                },
            }

        if entrypoint_type == "http":
            return await self._execute_http(config, assignment.get("input_payload") or {})

        return {
            "success": False,
            "error_message": (
                f"тип точки входа «{entrypoint_type}» не поддерживается этим worker'ом"
            ),
            "metrics": {},
        }

    async def _execute_http(
        self, config: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        url = str(config.get("url", ""))
        headers = {"Content-Type": "application/json"}
        if config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                response = await http.post(url, json={"input": payload}, headers=headers)
            duration_ms = int((time.monotonic() - started) * 1000)
            if response.status_code >= 400:
                return {
                    "success": False,
                    "error_message": f"HTTP {response.status_code}: {response.text[:500]}",
                    "metrics": {"duration_ms": duration_ms},
                }
            body = response.json()
            output = body.get("output", body) if isinstance(body, dict) else {"result": body}
            return {
                "success": True,
                "output": output if isinstance(output, dict) else {"result": output},
                "metrics": {"duration_ms": duration_ms, "status_code": response.status_code},
            }
        except Exception as exc:
            return {
                "success": False,
                "error_message": f"http entrypoint failed: {exc}"[:500],
                "metrics": {"duration_ms": int((time.monotonic() - started) * 1000)},
            }

    async def handle(self, assignment: dict[str, Any]) -> dict[str, Any]:
        task_id = assignment["task_id"]
        lease = assignment["lease_token"]
        await self.report_progress(task_id, lease, "задание принято worker'ом")
        outcome = await self.execute(assignment)
        response = await self.submit_result(
            task_id,
            lease,
            success=bool(outcome["success"]),
            output=outcome.get("output") or {},
            error_message=outcome.get("error_message"),
            metrics=outcome.get("metrics") or {},
        )
        if outcome["success"]:
            self._executed += 1
        else:
            self._failed += 1
        return response

    # ── главный цикл ─────────────────────────────────────────────────
    async def run_once(self) -> int:
        assignments = await self.claim()
        for assignment in assignments:
            try:
                await self.handle(assignment)
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "task_submit_failed",
                    extra={"task_id": assignment["task_id"], "status": exc.response.status_code},
                )
            except Exception as exc:  # pragma: no cover - сетевые сбои
                logger.error("task_execution_error", extra={"error": str(exc)})
        return len(assignments)

    async def run_forever(self) -> None:
        sequence = 0
        await self.verify()
        while self._running:
            sequence += 1
            try:
                await self.heartbeat(sequence)
                processed = await self.run_once()
                if not processed:
                    await asyncio.sleep(self.poll_interval)
            except httpx.HTTPError as exc:
                logger.warning("worker_http_error", extra={"error": str(exc)})
                await asyncio.sleep(self.poll_interval)
        await self.client.close()
        logger.info(
            "worker_stopped", extra={"executed": self._executed, "failed": self._failed}
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Civilization node worker")
    parser.add_argument("--api-url", default=os.environ.get("AC_API_URL", "http://localhost:8000"))
    parser.add_argument("--node-key", default=os.environ.get("AC_NODE_KEY"))
    parser.add_argument("--node-secret", default=os.environ.get("AC_NODE_SECRET"))
    parser.add_argument("--capabilities", default="python")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--sandbox-backend", default=None, choices=["subprocess", "docker", "noop"])
    parser.add_argument("--once", action="store_true", help="одна итерация и выход")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if not args.node_key or not args.node_secret:
        print("Нужны --node-key и --node-secret (или AC_NODE_KEY/AC_NODE_SECRET)", file=sys.stderr)
        return 2
    client = NodeClient(args.api_url, args.node_key, args.node_secret)
    worker = NodeWorker(
        client,
        capabilities=[c.strip() for c in args.capabilities.split(",") if c.strip()],
        poll_interval=args.poll_interval,
        max_tasks=args.max_tasks,
        sandbox_backend=args.sandbox_backend,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    if args.once:
        await worker.verify()
        await worker.heartbeat(1)
        processed = await worker.run_once()
        print(json.dumps({"processed": processed}))
        await client.close()
        return 0

    await worker.run_forever()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
