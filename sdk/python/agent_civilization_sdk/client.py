"""Клиенты публичного API: пользовательский и узловой."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx


class ApiError(Exception):
    """Ошибка API с машиночитаемым кодом."""

    def __init__(
        self, message: str, *, code: str = "unknown", status: int = 0, details: Any = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class Client:
    """Клиент цивилизации: пользовательская часть API."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        api_prefix: str = "/api/v1",
    ) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.api_prefix = api_prefix
        self.access_token = token
        self.refresh_token: str | None = None

    # ── инфраструктура ───────────────────────────────────────────────
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        response = self._http.request(
            method, f"{self.api_prefix}{path}", headers=headers, **kwargs
        )
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            error = (payload or {}).get("error", {})
            raise ApiError(
                error.get("message") or f"HTTP {response.status_code}",
                code=error.get("code", f"http_{response.status_code}"),
                status=response.status_code,
                details=error.get("details"),
            )
        return payload

    def _authenticate(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.access_token = payload["access_token"]
        self.refresh_token = payload["refresh_token"]
        return payload

    def refresh(self) -> None:
        payload = self._request(
            "POST", "/auth/refresh", json={"refresh_token": self.refresh_token}
        )
        self.access_token = payload["access_token"]
        self.refresh_token = payload["refresh_token"]

    # ── auth ─────────────────────────────────────────────────────────
    def register(self, email: str, password: str, display_name: str) -> dict[str, Any]:
        return self._authenticate(
            self._request(
                "POST",
                "/auth/register",
                json={"email": email, "password": password, "display_name": display_name},
            )
        )

    def login(self, email: str, password: str) -> dict[str, Any]:
        return self._authenticate(
            self._request("POST", "/auth/login", json={"email": email, "password": password})
        )

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/auth/me")

    # ── агенты ───────────────────────────────────────────────────────
    def create_agent(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/agents", json=payload)

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/{agent_id}")

    def search_agents(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/agents", params=params)

    def update_agent(self, agent_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("PATCH", f"/agents/{agent_id}", json=payload)

    def create_version(self, agent_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("POST", f"/agents/{agent_id}/versions", json=payload)

    def publish(self, agent_id: str, publish: bool = True) -> dict[str, Any]:
        return self._request("POST", f"/agents/{agent_id}/publish", json={"publish": publish})

    def suspend(self, agent_id: str, reason: str) -> dict[str, Any]:
        return self._request("POST", f"/agents/{agent_id}/suspend", json={"reason": reason})

    def capabilities(self) -> list[dict[str, Any]]:
        return self._request("GET", "/capabilities")

    # ── задачи ───────────────────────────────────────────────────────
    def submit_task(
        self,
        agent_id: str,
        input_payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        priority: int = 5,
        timeout_seconds: int = 300,
        budget_units: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "input_payload": input_payload,
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
            "priority": priority,
            "timeout_seconds": timeout_seconds,
        }
        if budget_units is not None:
            body["budget_units"] = budget_units
        return self._request("POST", "/tasks", json=body)

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}")

    def wait_for_task(
        self, task_id: str, *, timeout: float = 120, interval: float = 1.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self.get_task(task_id)
            if task["status"] in {"succeeded", "failed", "cancelled", "expired"}:
                return task
            time.sleep(interval)
        raise TimeoutError(f"Задание {task_id} не завершилось за {timeout}s")

    # ── узлы и статистика ────────────────────────────────────────────
    def register_node(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/nodes", json=payload)

    def nodes(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/nodes", params=params)

    def stats(self) -> dict[str, Any]:
        return self._request("GET", "/platform/stats")


    # ── диалоги и сообщения (этап 3) ──────────────────────────────────
    def conversations(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/conversations", params=params)

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/conversations/{conversation_id}")

    def send_message(
        self,
        sender_agent_id: str,
        recipient_agent_id: str,
        content: Any,
        *,
        kind: str = "request",
        conversation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sender_agent_id": sender_agent_id,
            "recipient_agent_id": recipient_agent_id,
            "kind": kind,
            "payload": content if isinstance(content, dict) else {"content": content},
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        return self._request("POST", "/messages", json=body)

    def inbox(self, agent_id: str, **params: Any) -> dict[str, Any]:
        return self._request("GET", f"/agents/{agent_id}/inbox", params=params)

    def discover(self, capability: str, **params: Any) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/discovery", params={"capability": capability, **params}
        )

    def mcp_servers(self, agent_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/agents/{agent_id}/mcp-servers")

    def register_mcp_server(self, agent_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("POST", f"/agents/{agent_id}/mcp-servers", json=payload)

    # ── оркестрация (этап 4) ──────────────────────────────────────────
    def create_plan(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/plans", json=payload)

    def plan(self, plan_id: str) -> dict[str, Any]:
        return self._request("GET", f"/plans/{plan_id}")

    def plans(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/plans", params=params)

    def run_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request("POST", f"/plans/{plan_id}/run")

    def cancel_plan(self, plan_id: str, reason: str = "") -> dict[str, Any]:
        return self._request("POST", f"/plans/{plan_id}/cancel", json={"reason": reason})

    def retry_step(self, plan_id: str, step_key: str) -> dict[str, Any]:
        return self._request("POST", f"/plans/{plan_id}/steps/{step_key}/retry")

    def plan_events(self, plan_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/plans/{plan_id}/events")

    # ── проверка и доверие (этап 5) ───────────────────────────────────
    def request_verification(self, task_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("POST", f"/tasks/{task_id}/verifications", json=payload)

    def verifications(self, task_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/tasks/{task_id}/verifications")

    def verdict(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}/verdict")

    def review(self, task_id: str, **payload: Any) -> dict[str, Any]:
        return self._request("POST", f"/tasks/{task_id}/review", json=payload)

    def trust(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/{agent_id}/trust")

    def reputation(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/{agent_id}/reputation")

    def appeal(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/appeals", json=payload)

    # ── экономика (этап 6) ────────────────────────────────────────────
    def money(self) -> dict[str, Any]:
        return self._request("GET", "/economy/me")

    def accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/economy/accounts")

    def statement(self, account_id: str) -> dict[str, Any]:
        return self._request("GET", f"/economy/accounts/{account_id}/statement")

    def transactions(self, **params: Any) -> list[dict[str, Any]]:
        return self._request("GET", "/economy/transactions", params=params)

    def transfer(
        self,
        from_account_id: str,
        to_account_id: str,
        amount_units: float,
        *,
        comment: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/economy/transfer",
            json={
                "from_account_id": from_account_id,
                "to_account_id": to_account_id,
                "amount_units": amount_units,
                "comment": comment,
                "idempotency_key": idempotency_key or uuid.uuid4().hex,
            },
        )

    def withdraw(self, agent_id: str, amount_units: float) -> dict[str, Any]:
        return self._request(
            "POST", "/economy/withdraw", json={"agent_id": agent_id, "amount_units": amount_units}
        )

    def task_budget(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/economy/tasks/{task_id}/budget")

    def reward_rules(self) -> list[dict[str, Any]]:
        return self._request("GET", "/economy/rules")

    def rewards(self, **params: Any) -> list[dict[str, Any]]:
        return self._request("GET", "/economy/rewards", params=params)

    def verify_ledger(self) -> dict[str, Any]:
        return self._request("POST", "/economy/verify")

    # ── открытая экосистема (этап 8) ──────────────────────────────────
    def compat_check(
        self, agent_id: str, *, sample_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if sample_input is not None:
            body["sample_input"] = sample_input
        return self._request("POST", f"/agents/{agent_id}/compatibility", json=body)

    def compat_history(self, agent_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/agents/{agent_id}/compatibility")

    def templates(self, runtime: str | None = None) -> list[dict[str, Any]]:
        params = {"runtime": runtime} if runtime else None
        return self._request("GET", "/ecosystem/templates", params=params)

    def template(self, slug: str) -> dict[str, Any]:
        return self._request("GET", f"/ecosystem/templates/{slug}")

    def scaffold(self, slug: str, target_dir: str) -> list[str]:
        """Разворачивает шаблон проекта в локальную папку. Возвращает список файлов."""
        from pathlib import Path

        files = self.template(slug)["files"]
        root = Path(target_dir)
        written: list[str] = []
        for item in files:
            path = (root / item["path"]).resolve()
            if not str(path).startswith(str(root.resolve())):
                raise ValueError(f"небезопасный путь в шаблоне: {item['path']}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item["content"], encoding="utf-8")
            written.append(str(path.relative_to(root.resolve())))
        return written

    def integrations(self, **params: Any) -> list[dict[str, Any]]:
        return self._request("GET", "/integrations", params=params)

    def add_integration(self, **payload: Any) -> dict[str, Any]:
        return self._request("POST", "/integrations", json=payload)

    def probe_integration(self, integration_id: str) -> dict[str, Any]:
        return self._request("POST", f"/integrations/{integration_id}/probe")

    def portal(self) -> dict[str, Any]:
        return self._request("GET", "/developer/portal")

    def ecosystem_stats(self) -> dict[str, Any]:
        return self._request("GET", "/ecosystem/stats")


class NodeClient:
    """Клиент узла: подписывает каждый запрос HMAC-SHA256.

    Секрет не передаётся в заголовках: подпись строится от `sha256(secret)`,
    что совпадает с моделью хранения на сервере (там тоже только хеш).
    """

    def __init__(
        self,
        base_url: str,
        node_key: str,
        node_secret: str,
        *,
        api_prefix: str = "/api/v1",
        timeout: float = 30.0,
    ) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.api_prefix = api_prefix
        self.node_key = node_key
        self._key = hashlib.sha256(node_secret.encode()).hexdigest()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> NodeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _signature(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signed_path = (
            path[len(self.api_prefix) :] if path.startswith(self.api_prefix) else path
        )
        body_hash = hashlib.sha256(body or b"").hexdigest()
        canonical = "\n".join(
            [self.node_key, method.upper(), signed_path, timestamp, nonce, body_hash]
        )
        signature = hmac.new(
            bytes.fromhex(self._key), canonical.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "X-Node-Key": self.node_key,
            "X-Node-Signature": signature,
            "X-Node-Timestamp": timestamp,
            "X-Node-Nonce": nonce,
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = self._signature(method, path, body)
        headers["Content-Type"] = "application/json"
        response = self._http.request(
            method, f"{self.api_prefix}{path}", content=body, headers=headers
        )
        if response.status_code >= 400:
            error = (response.json() or {}).get("error", {})
            raise ApiError(
                error.get("message") or f"HTTP {response.status_code}",
                code=error.get("code", "node_error"),
                status=response.status_code,
                details=error.get("details"),
            )
        return response.json()

    def verify(self) -> dict[str, Any]:
        return self.request("POST", "/nodes/me/verify")

    def heartbeat(self, sequence: int, running_tasks: int = 0, **extra: Any) -> dict[str, Any]:
        return self.request(
            "POST",
            "/nodes/me/heartbeat",
            {"sequence": sequence, "running_tasks": running_tasks, "health": "healthy", **extra},
        )

    def claim(
        self, max_tasks: int = 1, capabilities: list[str] | None = None
    ) -> list[dict[str, Any]]:
        response = self.request(
            "POST",
            "/nodes/me/tasks/claim",
            {"max_tasks": max_tasks, "capabilities": capabilities or []},
        )
        return response.get("assignments", [])

    def progress(self, task_id: str, lease_token: str, message: str = "") -> dict[str, Any]:
        return self.request(
            "POST",
            f"/nodes/me/tasks/{task_id}/progress",
            {"lease_token": lease_token, "message": message},
        )

    def result(
        self,
        task_id: str,
        lease_token: str,
        *,
        success: bool,
        output: dict[str, Any] | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/nodes/me/tasks/{task_id}/result",
            {
                "lease_token": lease_token,
                "success": success,
                "output": output or {},
                "error_message": error_message,
                "metrics": metrics or {},
            },
        )
