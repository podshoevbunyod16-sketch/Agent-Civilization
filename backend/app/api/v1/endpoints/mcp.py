"""Stateless MCP HTTP endpoint for using Agent Civilization from AI clients.

Endpoint: POST /api/v1/mcp
Authentication: the same Bearer access token used by the REST API.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from app.api.deps import CurrentUser, DbSession, rate_limit_default
from app.core.errors import AppError, PermissionDeniedError
from app.db.models.agent import AgentStatus
from app.schemas.agent import AgentListItem
from app.schemas.node import TaskPublic, TaskSubmitRequest
from app.schemas.orchestration import PlanCreate, PlanStepCreate
from app.services import agents as agent_service
from app.services import gateway
from app.services import orchestration as orchestration_service

router = APIRouter(tags=["MCP"])
logger = logging.getLogger(__name__)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "find_agents",
        "description": (
            "Найти опубликованных агентов по названию или возможности. "
            "Сначала используй этот инструмент, чтобы выбрать подходящего агента."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Слова из названия или описания",
                },
                "capability": {
                    "type": "string",
                    "description": "Возможность, например text.summarize",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "run_agent",
        "description": (
            "Отправить опубликованному агенту одно задание. Возвращает ID задания; "
            "результат можно проверить через check_task. Нужен запущенный worker."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["agent_id", "input"],
            "properties": {
                "agent_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "ID агента из find_agents",
                },
                "input": {
                    "type": "object",
                    "description": 'JSON для агента, например {"text": "Привет"}',
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "check_task",
        "description": "Посмотреть статус и результат своего задания по ID.",
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "start_workflow",
        "description": (
            "Запустить план из одного или нескольких шагов. Для каждого шага укажи имя "
            "и ID агента. Шаги выполняются по порядку; вернётся ID плана."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "goal", "steps"],
            "properties": {
                "title": {"type": "string", "minLength": 2, "maxLength": 200},
                "goal": {"type": "string", "description": "Что нужно получить в итоге"},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "required": ["name", "agent_id"],
                        "properties": {
                            "name": {"type": "string", "minLength": 2, "maxLength": 200},
                            "agent_id": {"type": "string", "format": "uuid"},
                            "input": {
                                "type": "object",
                                "description": "Дополнительные данные для шага",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "check_workflow",
        "description": "Проверить статус и шаги своего плана по ID.",
        "inputSchema": {
            "type": "object",
            "required": ["plan_id"],
            "properties": {"plan_id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    },
]


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _tool_result(request_id: Any, value: Any, *, error: bool = False) -> JSONResponse:
    data = jsonable_encoder(value)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": error,
        },
    }
    if isinstance(data, dict):
        body["result"]["structuredContent"] = data
    else:
        body["result"]["structuredContent"] = {"items": data}
    return JSONResponse(body)


def _tool_error(request_id: Any, message: str) -> JSONResponse:
    return _tool_result(request_id, {"error": message}, error=True)


async def _invoke_tool(name: str, args: dict[str, Any], session, user) -> Any:
    if name == "find_agents":
        query = args.get("query")
        capability = args.get("capability")
        if query is not None and (not isinstance(query, str) or len(query) > 200):
            raise ValueError("query должен быть текстом не длиннее 200 символов")
        if capability is not None and (not isinstance(capability, str) or len(capability) > 80):
            raise ValueError("capability должна быть коротким текстовым ключом")
        limit = args.get("limit", 10)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit должен быть числом от 1 до 20")
        agents, total = await agent_service.search_agents(
            session,
            viewer=user,
            q=query,
            status=AgentStatus.published,
            capabilities=[capability] if capability else None,
            sort="reputation",
            order="desc",
            limit=limit,
        )
        return {
            "total": total,
            "agents": [
                AgentListItem.model_validate(agent).model_dump(mode="json") for agent in agents
            ],
        }

    if name == "run_agent":
        try:
            agent_id = uuid.UUID(args.get("agent_id", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("agent_id должен быть ID из find_agents") from exc
        payload = args.get("input")
        if not isinstance(payload, dict):
            raise ValueError("input должен быть JSON-объектом")
        task = await gateway.submit_task(
            session,
            user.id,
            TaskSubmitRequest(agent_id=agent_id, input_payload=payload),
        )
        await session.commit()
        return {
            "task_id": str(task.id),
            "status": task.status.value,
            "message": "Задание принято. Используй check_task для проверки результата.",
        }

    if name == "check_task":
        try:
            task_id = uuid.UUID(args.get("task_id", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("task_id должен быть ID задания") from exc
        task = await gateway.get_task(session, task_id)
        if task.requester_id != user.id and not user.is_admin:
            agent = await agent_service.get_agent(session, task.agent_id)
            if agent.owner_id != user.id:
                raise PermissionDeniedError("Нет доступа к этому заданию", code="task_forbidden")
        return TaskPublic.model_validate(task).model_dump(mode="json")

    if name == "start_workflow":
        title = args.get("title")
        goal = args.get("goal")
        if not isinstance(title, str) or not 2 <= len(title.strip()) <= 200:
            raise ValueError("Укажи название плана (от 2 до 200 символов)")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Коротко опиши цель плана")
        steps_input = args.get("steps")
        if not isinstance(steps_input, list) or not 1 <= len(steps_input) <= 20:
            raise ValueError("Добавь от 1 до 20 шагов")
        steps: list[PlanStepCreate] = []
        previous_key: str | None = None
        for index, item in enumerate(steps_input, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Шаг {index} должен быть объектом")
            try:
                agent_id = uuid.UUID(item.get("agent_id", ""))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"Укажи ID агента для шага {index}") from exc
            agent = await agent_service.get_agent(session, agent_id)
            if agent.status is not AgentStatus.published:
                raise ValueError(f"Агент «{agent.name}» не опубликован")
            key = f"step-{index}"
            extra_input = item.get("input", {})
            if not isinstance(extra_input, dict):
                raise ValueError(f"input шага {index} должен быть JSON-объектом")
            steps.append(
                PlanStepCreate(
                    key=key,
                    name=item.get("name", f"Шаг {index}"),
                    agent_id=agent_id,
                    depends_on=[previous_key] if previous_key else [],
                    input_payload={"goal": args.get("goal", ""), **extra_input},
                )
            )
            previous_key = key
        plan_data = PlanCreate(
            title=args.get("title", "Новый план"),
            goal={"description": args.get("goal", "")},
            steps=steps,
            auto_start=True,
        )
        plan = await orchestration_service.create_plan(
            session, requester_id=user.id, data=plan_data
        )
        await orchestration_service.assign_steps(session, plan)
        run_result = await orchestration_service.run_plan(session, plan, actor="mcp")
        await session.commit()
        return {
            "plan_id": str(plan.id),
            "status": plan.status.value,
            "steps": len(steps),
            "launched": run_result["launched"],
            "message": "План создан. Используй check_workflow для проверки хода выполнения.",
        }

    if name == "check_workflow":
        try:
            plan_id = uuid.UUID(args.get("plan_id", ""))
        except (TypeError, ValueError) as exc:
            raise ValueError("plan_id должен быть ID плана") from exc
        plan = await orchestration_service.get_plan(session, plan_id)
        orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
        steps = await orchestration_service.plan_steps(session, plan.id)
        return {
            "plan_id": str(plan.id),
            "title": plan.title,
            "status": plan.status.value,
            "steps": [
                {
                    "key": step.key,
                    "name": step.name,
                    "status": step.status.value,
                    "agent_id": str(step.agent_id) if step.agent_id else None,
                    "task_id": str(step.task_id) if step.task_id else None,
                    "error": step.error,
                }
                for step in steps
            ],
        }

    raise KeyError(name)


@router.post(
    "/mcp",
    summary="MCP: инструменты Agent Civilization",
    dependencies=[Depends(rate_limit_default)],
)
async def mcp_endpoint(payload: dict[str, Any], session: DbSession, user: CurrentUser) -> Response:
    """MCP Streamable HTTP (stateless JSON response), авторизация через Bearer token."""
    request_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return _rpc_error(request_id, -32600, "Invalid Request")

    method = payload["method"]
    if "id" not in payload:
        # MCP notifications carry no response body.
        return Response(status_code=202)

    if method == "initialize":
        params = payload.get("params") or {}
        supported_versions = {"2025-03-26", "2024-11-05"}
        requested_version = params.get("protocolVersion") if isinstance(params, dict) else None
        version = (
            requested_version
            if isinstance(requested_version, str) and requested_version in supported_versions
            else "2025-03-26"
        )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agent-civilization", "version": "1.0.0"},
                    "instructions": (
                        "Ищи агента через find_agents, запускай задачу через run_agent "
                        "или план через start_workflow. Проверяй результат через "
                        "check_task/check_workflow."
                    ),
                },
            }
        )

    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})

    if method != "tools/call":
        return _rpc_error(request_id, -32601, "Method not found")

    params = payload.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _rpc_error(request_id, -32602, "tools/call requires params.name")
    args = params.get("arguments", {})
    if not isinstance(args, dict):
        return _rpc_error(request_id, -32602, "tools/call arguments must be an object")
    try:
        result = await _invoke_tool(params["name"], args, session, user)
    except KeyError:
        return _rpc_error(request_id, -32602, "Unknown tool")
    except (ValueError, PydanticValidationError) as exc:
        return _tool_error(request_id, str(exc))
    except AppError as exc:
        return _tool_error(request_id, exc.message)
    except Exception:
        logger.exception("MCP tool failed: %s", params["name"])
        return _tool_error(request_id, "Инструмент не выполнен. Проверь данные и состояние API.")
    return _tool_result(request_id, result)
