"""Root-level MCP Streamable HTTP endpoint.

Initialize, discovery and public resource metadata are anonymous; any operation
that reads user data or changes state requires a bearer account/API token.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from app.api.deps import Ctx, DbSession, OptionalUser, rate_limit_default
from app.api.v1.endpoints.mcp import TOOLS, _invoke_tool, _rpc_error, _tool_error, _tool_result
from app.core.errors import AppError
from app.schemas.trust import VerificationCheckConfig
from app.services import api_keys, gateway
from app.services import economy as economy_service
from app.services import trust as trust_service

router = APIRouter(tags=["MCP"])

ALIASES: list[dict[str, Any]] = [
    {
        "name": "list_agents",
        "description": (
            "List published agents and capabilities. RU: Список опубликованных агентов "
            'и возможностей. Example: {"skill":"code.review"}.'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "find_agent_for_capability",
        "description": (
            "Find the best published agent for a capability. RU: Подобрать агента "
            'для возможности. Example: {"capability":"data.analysis"}.'
        ),
        "inputSchema": {
            "type": "object",
            "required": ["capability"],
            "properties": {
                "capability": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task",
        "description": (
            "Submit a task to a published agent. RU: Создать задание агенту. "
            'Example: {"agent_id":"UUID","input":{}}.'
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "input"],
            "properties": {
                "agent_id": {"type": "string", "format": "uuid"},
                "input": {"type": "object"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_task_status",
        "description": (
            "Read the status and output of your task. RU: Статус и результат задания. "
            'Example: {"task_id":"UUID"}.'
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_balance",
        "description": (
            "Read your internal ACU ledger balance. RU: Баланс ACU участника. Example: {}."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "verify_result",
        "description": (
            "Run configured automatic checks for your completed task. RU: Запустить "
            'автоматическую проверку результата. Example: {"task_id":"UUID"}.'
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "run_in_sandbox",
        "description": (
            "Run a command only when a separately configured command allowlist is enabled. "
            "RU: Без allowlist запуск отключён; запрос вернёт явную ошибку."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string", "maxLength": 2000}},
            "additionalProperties": False,
        },
    },
    {
        "name": "search_integrations",
        "description": (
            "Search the currently registered integration catalog. RU: Поиск "
            'зарегистрированных интеграций. Example: {"query":"filesystem"}.'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 100}},
            "additionalProperties": False,
        },
    },
]
ROOT_TOOLS = [*TOOLS, *ALIASES]
RESOURCES = [
    {
        "uri": "civilization://about",
        "name": "Agent Civilization overview",
        "description": "Architecture and honest demo limitations.",
        "mimeType": "text/plain",
    }
]


def _bearer(payload: dict[str, Any], request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, credential = header.partition(" ")
    return credential if scheme.lower() == "bearer" and credential else None


@router.post(
    "/mcp", dependencies=[Depends(rate_limit_default)], summary="MCP Streamable HTTP endpoint"
)
async def root_mcp(
    payload: dict[str, Any], request: Request, session: DbSession, user: OptionalUser, ctx: Ctx
):
    request_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return _rpc_error(request_id, -32600, "Invalid Request")
    if "id" not in payload:
        return Response(status_code=202)
    method = payload["method"]
    if method == "initialize":
        params = payload.get("params") or {}
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in {"2024-11-05", "2025-03-26"} else "2025-03-26"
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False, "subscribe": False},
                    },
                    "serverInfo": {"name": "agent-civilization", "version": "1.0.0"},
                    "instructions": (
                        "Read-only discovery is public. Use a Bearer API key for user-scoped tools."
                    ),
                },
            }
        )
    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": ROOT_TOOLS}})
    if method == "resources/list":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "result": {"resources": RESOURCES}}
        )
    if method == "resources/read":
        uri = (payload.get("params") or {}).get("uri")
        if uri != "civilization://about":
            return _rpc_error(request_id, -32602, "Unknown resource URI")
        text = (
            "Agent Civilization is a real FastAPI/PostgreSQL platform. Demo agents are seeded "
            "code examples. Subprocess sandbox is not a network-isolated security boundary. "
            "External MCP server gateway is not implemented in this release."
        )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]},
            }
        )
    if method != "tools/call":
        return _rpc_error(request_id, -32601, "Method not found")
    token = _bearer(payload, request)
    if user is None and token and token.startswith("ac_live_"):
        user = await api_keys.authenticate_api_key(session, token)
    if user is None:
        return JSONResponse(
            {
                "error": {
                    "code": "authentication_required",
                    "message": "Bearer API key required for this MCP tool",
                }
            },
            status_code=401,
        )
    params = payload.get("params")
    if (
        not isinstance(params, dict)
        or not isinstance(params.get("name"), str)
        or not isinstance(params.get("arguments", {}), dict)
    ):
        return _rpc_error(
            request_id, -32602, "tools/call requires params.name and object arguments"
        )
    name, args = params["name"], params.get("arguments", {})
    try:
        if name in {"list_agents", "find_agent_for_capability"}:
            capability = args.get("skill") or args.get("capability")
            return _tool_result(
                request_id,
                await _invoke_tool(
                    "find_agents",
                    {"capability": capability, "limit": args.get("limit", 10)},
                    session,
                    user,
                    ctx,
                ),
            )
        if name == "create_task":
            return _tool_result(
                request_id, await _invoke_tool("run_agent", args, session, user, ctx)
            )
        if name == "get_task_status":
            return _tool_result(
                request_id, await _invoke_tool("check_task", args, session, user, ctx)
            )
        if name == "get_balance":
            balance = await economy_service.user_economy(session, user.id)
            await session.commit()
            return _tool_result(request_id, balance)
        if name == "verify_result":
            task = await gateway.get_task(session, uuid.UUID(args.get("task_id", "")))
            if task.requester_id != user.id and not user.is_admin:
                return _tool_error(request_id, "Only the task owner may verify this result")
            checks = await trust_service.run_automatic_checks(
                session, task, VerificationCheckConfig().model_dump()
            )
            verdict = await trust_service.list_verifications(session, task.id)
            await session.commit()
            return _tool_result(
                request_id,
                {
                    "task_id": str(task.id),
                    "checks": jsonable_encoder(verdict),
                    "created_checks": len(checks),
                },
            )
        if name == "run_in_sandbox":
            return _tool_error(
                request_id,
                "Sandbox command tool is disabled: no command allowlist is configured. "
                "Use registered agent execution through the worker instead.",
            )
        if name == "search_integrations":
            return _tool_error(
                request_id,
                "Integration search is not available through MCP yet; "
                "the catalog is available in the web UI.",
            )
        return _tool_result(request_id, await _invoke_tool(name, args, session, user, ctx))
    except (ValueError, TypeError, KeyError) as exc:
        return _tool_error(request_id, str(exc))
    except AppError as exc:
        return _tool_error(request_id, exc.message)
    except Exception:
        return _tool_error(
            request_id, "Tool execution failed; inspect API logs using the request ID."
        )
