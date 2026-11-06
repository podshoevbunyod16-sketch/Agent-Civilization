"""MCP-клиент (Model Context Protocol) — подключение инструментов к агентам.

Реализован JSON-RPC 2.0 клиент для HTTP-транспорта MCP:

    initialize → tools/list → tools/call

Клиент не зависит от конкретного MCP-сервера и не требует SDK: протокол —
обычный JSON-RPC по HTTP. Для `stdio`-транспорта используйте внешний
адаптер (он живёт на стороне узла, а не backend).

Безопасность: сетевые вызовы выполняются воркером/шлюзом, токен доступа
передаётся только в заголовке и никогда не попадает в логи или в ответы API.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _allow_private_networks() -> bool:
    from app.core.config import settings

    return settings.ssrf_allow_private


class McpError(Exception):
    """Ошибка MCP-сервера или транспорта."""

    def __init__(self, message: str, *, code: int | str = "mcp_error", data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class McpClient:
    """Минимальный MCP-клиент (JSON-RPC 2.0 поверх HTTP)."""

    def __init__(
        self,
        server_url: str,
        *,
        auth_token: str | None = None,
        timeout: float = 30.0,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # SSRF-проверка (этап 10, A #7): адрес MCP-сервера задаёт
        # пользователь, поэтому он не должен вести во внутреннюю сеть.
        # При подставленном транспорте (тесты, локальный адаптер) сетевого
        # соединения нет — проверять резолв нечего, и безопасный клиент не
        # используется. Для реального транспорта проверка выполняется на
        # каждый запрос, что закрывает DNS-rebinding.
        if transport is None:
            from app.core.net_guard import validate_url

            validate_url(server_url, allow_private=_allow_private_networks())
        self.server_url = server_url
        self.protocol_version = protocol_version
        self._token = auth_token
        self._request_id = 0
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        if transport is not None:
            # Явный транспорт используется в тестах (MockTransport).
            self._client = httpx.AsyncClient(
                timeout=timeout, headers=headers, transport=transport
            )
        else:
            from app.core.net_guard import SafeAsyncClient

            self._client = SafeAsyncClient(
                timeout=timeout,
                headers=headers,
                allow_private=_allow_private_networks(),
            )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> McpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        try:
            response = await self._client.post(self.server_url, json=payload)
        except httpx.HTTPError as exc:
            raise McpError(f"MCP транспорт недоступен: {exc.__class__.__name__}") from exc

        if response.status_code >= 400:
            raise McpError(
                f"MCP вернул HTTP {response.status_code}",
                code=f"http_{response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise McpError("MCP вернул не JSON") from exc

        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            raise McpError(
                str(error.get("message", "MCP error")),
                code=error.get("code", "mcp_error"),
                data=error.get("data"),
            )
        return body.get("result") if isinstance(body, dict) else body

    async def initialize(self) -> dict[str, Any]:
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "agent-civilization", "version": "0.1.0"},
            },
        )
        return result or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list", {})
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        return [
            {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", tool.get("input_schema", {})),
            }
            for tool in tools
            if tool.get("name")
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Вызывает инструмент и нормализует результат.

        Возвращает `{"content": ..., "is_error": bool, "raw": ...}`.
        """
        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        if not isinstance(result, dict):
            return {"content": result, "is_error": False, "raw": result}
        content = result.get("content", [])
        text_parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return {
            "content": text_parts if text_parts else content,
            "is_error": bool(result.get("isError", False)),
            "raw": result,
        }

    async def call_tool_as_json(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Как `call_tool`, но пытается разобрать текстовый ответ как JSON."""
        import json

        result = await self.call_tool(name, arguments)
        parsed: Any = result["content"]
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], str):
            try:
                parsed = json.loads(parsed[0])
            except json.JSONDecodeError:
                parsed = parsed[0]
        return {
            "output": parsed,
            "is_error": result["is_error"],
            "tool": name,
        }


async def call_mcp_tool(
    *,
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    auth_token: str | None = None,
    request_timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Удобная обёртка: один вызов инструмента без явного жизненного цикла."""
    async with McpClient(
        server_url,
        auth_token=auth_token,
        timeout=request_timeout,
        transport=transport,
    ) as client:
        return await client.call_tool_as_json(tool_name, arguments)


async def sync_tools(
    *,
    server_url: str,
    auth_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Синхронизация списка инструментов MCP-сервера."""
    async with McpClient(server_url, auth_token=auth_token, transport=transport) as client:
        await client.initialize()
        return await client.list_tools()
