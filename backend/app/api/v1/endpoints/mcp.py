"""Stateless MCP HTTP endpoint for using Agent Civilization from AI clients.

Endpoint: POST /api/v1/mcp
Authentication: a Bearer access token or an MCP-scoped API key.
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

from app.api.deps import Ctx, CurrentUser, DbSession, rate_limit_default
from app.core.errors import AppError, PermissionDeniedError, ValidationError
from app.db.models.agent import AgentStatus
from app.db.models.task import TaskStatus
from app.schemas.agent import (
    AgentCapabilitiesReplace,
    AgentCreate,
    AgentListItem,
    AgentPublishRequest,
    AgentSuspendRequest,
    AgentUpdate,
    AgentVersionCreate,
)
from app.schemas.ecosystem import CompatibilityCheckRequest
from app.schemas.node import TaskCancelRequest, TaskPublic, TaskSubmitRequest
from app.schemas.orchestration import PlanCreate, PlanStepCreate
from app.services import agents as agent_service
from app.services import audit as audit_service
from app.services import ecosystem as ecosystem_service
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

TOOLS.extend(
    [
        {
            "name": "list_capabilities",
            "description": "Показать доступные возможности для объявления у агента.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_my_agents",
            "description": "Показать ваших агентов, включая черновики и архивные записи.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_my_agent",
            "description": "Получить карточку своего агента и версии без секретов точки входа.",
            "inputSchema": {
                "type": "object",
                "required": ["agent_id"],
                "properties": {"agent_id": {"type": "string", "format": "uuid"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "create_agent",
            "description": (
                "Создать черновик агента. Затем добавь версию, протестируй её и вызови "
                "publish_agent. Новые capability keys регистрируются как заявленные."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["slug", "name", "summary"],
                "properties": {
                    "slug": {"type": "string", "minLength": 3, "maxLength": 64},
                    "name": {"type": "string", "minLength": 2, "maxLength": 120},
                    "summary": {"type": "string", "minLength": 10, "maxLength": 280},
                    "description": {"type": "string", "maxLength": 20000},
                    "category": {"type": "string", "maxLength": 60},
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "homepage_url": {"type": ["string", "null"]},
                    "repository_url": {"type": ["string", "null"]},
                    "accent_color": {"type": "string"},
                    "capabilities": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "required": ["capability_key"],
                            "properties": {
                                "capability_key": {"type": "string", "maxLength": 80},
                                "declared_level": {"type": "integer", "minimum": 0, "maximum": 5},
                                "note": {"type": ["string", "null"], "maxLength": 280},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "metadata_json": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "update_agent",
            "description": (
                "Обновить метаданные своего агента; версионные поля изменяются отдельной версией."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["agent_id"],
                "properties": {
                    "agent_id": {"type": "string", "format": "uuid"},
                    "name": {"type": "string", "minLength": 2, "maxLength": 120},
                    "summary": {"type": "string", "minLength": 10, "maxLength": 280},
                    "description": {"type": "string", "maxLength": 20000},
                    "category": {"type": "string", "maxLength": 60},
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "homepage_url": {"type": ["string", "null"]},
                    "repository_url": {"type": ["string", "null"]},
                    "accent_color": {"type": "string"},
                    "metadata_json": {"type": "object"},
                    "change_note": {"type": "string", "maxLength": 500},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "replace_agent_capabilities",
            "description": "Заменить заявленные возможности и уровни своего агента.",
            "inputSchema": {
                "type": "object",
                "required": ["agent_id", "capabilities"],
                "properties": {
                    "agent_id": {"type": "string", "format": "uuid"},
                    "capabilities": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "required": ["capability_key"],
                            "properties": {
                                "capability_key": {"type": "string", "maxLength": 80},
                                "declared_level": {"type": "integer", "minimum": 0, "maximum": 5},
                                "note": {"type": ["string", "null"], "maxLength": 280},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "create_agent_version",
            "description": (
                "Добавить неизменяемую версию своего агента. Для обновления опубликованного "
                "агента сначала сними его с публикации; оставь publish=false, вызови test_agent "
                "и опубликуй только после успешной проверки."
            ),
            "inputSchema": {
                "type": "object",
                "required": [
                    "agent_id",
                    "entrypoint_type",
                    "entrypoint_config",
                    "input_schema",
                    "output_schema",
                ],
                "properties": {
                    "agent_id": {"type": "string", "format": "uuid"},
                    "changelog": {"type": "string", "maxLength": 5000},
                    "entrypoint_type": {
                        "type": "string",
                        "enum": ["http", "python", "docker", "mcp"],
                    },
                    "entrypoint_config": {"type": "object"},
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "required_capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 32,
                    },
                    "resource_limits": {
                        "type": "object",
                        "properties": {
                            "max_cpu_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                            "max_wall_seconds": {"type": "integer", "minimum": 1, "maximum": 7200},
                            "max_memory_mb": {"type": "integer", "minimum": 64, "maximum": 32768},
                            "allow_network": {"type": "boolean"},
                            "max_output_bytes": {
                                "type": "integer",
                                "minimum": 1024,
                                "maximum": 10000000,
                            },
                        },
                        "additionalProperties": False,
                    },
                    "publish": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "test_agent",
            "description": (
                "Запустить проверку схем, лимитов и реального исполнения Python-версии на примере "
                "входа. Работает и на черновиках; вывод не публикуется."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["agent_id"],
                "properties": {
                    "agent_id": {"type": "string", "format": "uuid"},
                    "sample_input": {"type": "object"},
                    "imperfect_output": {
                        "type": "object",
                        "description": "Необязательный негативный тест выходной схемы.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "list_agent_versions",
            "description": "Показать историю версий своего агента без конфигурации и секретов.",
            "inputSchema": {
                "type": "object",
                "required": ["agent_id"],
                "properties": {"agent_id": {"type": "string", "format": "uuid"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "publish_agent",
            "description": "Опубликовать своего агента в каталоге или снять его с публикации.",
            "inputSchema": {
                "type": "object",
                "required": ["agent_id", "publish"],
                "properties": {
                    "agent_id": {"type": "string", "format": "uuid"},
                    "publish": {"type": "boolean"},
                    "note": {"type": "string", "maxLength": 500},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "suspend_agent",
            "description": "Приостановить своего опубликованного агента с объяснением причины.",
            "inputSchema": {
                "type": "object",
                "required": ["agent_id", "reason"],
                "properties": {
                    "agent_id": {"type": "string", "format": "uuid"},
                    "reason": {"type": "string", "minLength": 3, "maxLength": 280},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "resume_agent",
            "description": "Возобновить своего приостановленного агента.",
            "inputSchema": {
                "type": "object",
                "required": ["agent_id"],
                "properties": {"agent_id": {"type": "string", "format": "uuid"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "archive_agent",
            "description": (
                "Архивировать своего агента, убрав его из каталога без удаления истории."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["agent_id"],
                "properties": {"agent_id": {"type": "string", "format": "uuid"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_draft_agent",
            "description": (
                "Удалить черновик без истории заданий. Опубликованные агенты сначала нужно снять "
                "с публикации; при наличии истории используйте архивирование."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["agent_id"],
                "properties": {"agent_id": {"type": "string", "format": "uuid"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "rollback_agent_version",
            "description": (
                "Создать новую версию на основе выбранной старой версии; агент должен быть "
                "черновиком, чтобы откат сначала прошёл проверку и не стал активным сразу."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["agent_id", "version"],
                "properties": {
                    "agent_id": {"type": "string", "format": "uuid"},
                    "version": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
        },
    ]
)


TOOLS.extend(
    [
        {
            "name": "list_my_tasks",
            "description": "Показать последние задания, созданные вашим аккаунтом.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "queued",
                            "leased",
                            "running",
                            "succeeded",
                            "failed",
                            "cancelled",
                            "expired",
                        ],
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cancel_task",
            "description": (
                "Отменить своё незавершённое задание и освободить зарезервированный бюджет."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "format": "uuid"},
                    "reason": {"type": "string", "maxLength": 280},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "list_my_workflows",
            "description": "Показать последние планы, созданные вашим аккаунтом.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "cancel_workflow",
            "description": "Отменить свой незавершённый план и пропустить оставшиеся шаги.",
            "inputSchema": {
                "type": "object",
                "required": ["plan_id"],
                "properties": {
                    "plan_id": {"type": "string", "format": "uuid"},
                    "reason": {"type": "string", "maxLength": 280},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "retry_workflow_step",
            "description": "Повторить упавший, заблокированный или пропущенный шаг своего плана.",
            "inputSchema": {
                "type": "object",
                "required": ["plan_id", "step_key"],
                "properties": {
                    "plan_id": {"type": "string", "format": "uuid"},
                    "step_key": {"type": "string", "minLength": 1, "maxLength": 120},
                },
                "additionalProperties": False,
            },
        },
    ]
)


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


def _uuid_arg(args: dict[str, Any], key: str) -> uuid.UUID:
    value = args.get(key)
    try:
        return uuid.UUID(value) if isinstance(value, str) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{key} должен быть корректным UUID") from exc


def _agent_result(agent) -> dict[str, Any]:
    """Возвращает сведения о владельческом агенте, но никогда его конфиги/секреты."""
    return {
        "id": str(agent.id),
        "slug": agent.slug,
        "name": agent.name,
        "summary": agent.summary,
        "description": agent.description,
        "status": agent.status.value,
        "category": agent.category,
        "tags": agent.tags,
        "current_version": agent.current_version,
        "is_verified": agent.is_verified,
        "capabilities": [
            {
                "capability_key": item.capability_key,
                "declared_level": item.declared_level,
                "verified_level": item.verified_level,
            }
            for item in agent.capabilities
        ],
        "versions": [
            {
                "version": version.version,
                "changelog": version.changelog,
                "entrypoint_type": version.entrypoint_type.value,
                "published": version.published_at is not None,
                "created_at": version.created_at,
            }
            for version in agent.versions
        ],
    }


async def _audit_agent_action(
    session, *, user, ctx, action: str, agent_id: uuid.UUID, data: dict[str, Any] | None = None
) -> None:
    await audit_service.record(
        session,
        action=action,
        target_type="agent",
        target_id=agent_id,
        actor_id=user.id,
        ip_address=ctx.ip,
        user_agent=ctx.user_agent,
        request_id=ctx.request_id,
        data=data or {},
    )


async def _invoke_tool(name: str, args: dict[str, Any], session, user, ctx) -> Any:
    if name == "list_capabilities":
        rows = await agent_service.list_capabilities(session, active_only=True)
        return [
            {
                "key": item.key,
                "name": item.name,
                "description": item.description,
                "category": item.category,
            }
            for item in rows
        ]

    if name == "list_my_agents":
        agents, total = await agent_service.search_agents(
            session,
            viewer=user,
            status="any",
            owner_id=user.id,
            sort="created_at",
            order="desc",
            offset=0,
            limit=100,
        )
        return {
            "total": total,
            "agents": [
                AgentListItem.model_validate(agent).model_dump(mode="json") for agent in agents
            ],
        }

    if name == "get_my_agent":
        agent = await agent_service.get_agent(session, _uuid_arg(args, "agent_id"))
        if agent.owner_id != user.id and not user.is_admin:
            raise PermissionDeniedError("Только владелец может просматривать этот агент")
        return _agent_result(agent)

    if name == "create_agent":
        data = AgentCreate.model_validate(args)
        agent = await agent_service.create_agent(session, user, data)
        await _audit_agent_action(
            session,
            user=user,
            ctx=ctx,
            action="agent.create",
            agent_id=agent.id,
            data={
                "slug": agent.slug,
                "capabilities": [c.capability_key for c in agent.capabilities],
            },
        )
        await session.commit()
        return _agent_result(await agent_service.get_agent(session, agent.id))

    if name == "update_agent":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        data = AgentUpdate.model_validate(
            {key: value for key, value in args.items() if key != "agent_id"}
        )
        changed = sorted(data.model_dump(exclude_unset=True, exclude={"change_note"}))
        if not changed:
            raise ValueError("Передай хотя бы одно поле для изменения")
        await agent_service.update_agent(session, agent, data)
        await _audit_agent_action(
            session,
            user=user,
            ctx=ctx,
            action="agent.update",
            agent_id=agent.id,
            data={"fields": changed, "note": data.change_note},
        )
        await session.commit()
        return _agent_result(await agent_service.get_agent(session, agent_id))

    if name == "replace_agent_capabilities":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        data = AgentCapabilitiesReplace.model_validate({"capabilities": args.get("capabilities")})
        await agent_service.replace_capabilities(session, agent, data, user)
        await _audit_agent_action(
            session,
            user=user,
            ctx=ctx,
            action="agent.capabilities.replace",
            agent_id=agent.id,
            data={"capabilities": [item.capability_key for item in data.capabilities]},
        )
        await session.commit()
        return _agent_result(await agent_service.get_agent(session, agent_id))

    if name == "create_agent_version":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        if agent.status is not AgentStatus.draft:
            raise ValidationError(
                "Сначала переведите агента в черновик, чтобы новая версия не стала активной "
                "без проверки",
                code="agent_version_requires_draft",
            )
        data = AgentVersionCreate.model_validate(
            {key: value for key, value in args.items() if key != "agent_id"}
        )
        version = await agent_service.create_version(session, agent, user, data)
        await audit_service.record(
            session,
            action="agent.version.create",
            target_type="agent_version",
            target_id=version.id,
            actor_id=user.id,
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            data={
                "agent_id": str(agent.id),
                "version": version.version,
                "entrypoint_type": version.entrypoint_type.value,
            },
        )
        await session.commit()
        await session.refresh(version)
        agent = await agent_service.get_agent(session, agent_id)
        return {
            "agent_id": str(agent.id),
            "version_id": str(version.id),
            "version": version.version,
            "entrypoint_type": version.entrypoint_type.value,
            "published": version.published_at is not None,
            "agent_status": agent.status.value,
            "note": "Конфигурация точки входа скрыта; сохранённые коды и секреты не возвращаются.",
        }

    if name == "test_agent":
        agent_id = _uuid_arg(args, "agent_id")
        test_data = CompatibilityCheckRequest.model_validate(
            {key: value for key, value in args.items() if key != "agent_id"}
        )
        check = await ecosystem_service.run_compatibility_check(
            session,
            agent_id=agent_id,
            user_id=user.id,
            is_admin=user.is_admin,
            data=test_data,
            allow_draft=True,
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
        )
        return {
            "check_id": str(check.id),
            "agent_id": str(check.agent_id),
            "version_id": str(check.version_id) if check.version_id else None,
            "status": check.status.value,
            "score": check.score,
            "passed_count": check.passed_count,
            "failed_count": check.failed_count,
            "checks": check.checks,
            "sample_input": check.sample_input,
            "sample_output": check.sample_output,
            "error": check.error,
            "duration_ms": check.duration_ms,
        }

    if name == "list_agent_versions":
        agent = await agent_service.get_agent(session, _uuid_arg(args, "agent_id"))
        if agent.owner_id != user.id and not user.is_admin:
            raise PermissionDeniedError("Только владелец может просматривать версии")
        return [
            {
                "version": version.version,
                "changelog": version.changelog,
                "entrypoint_type": version.entrypoint_type.value,
                "published": version.published_at is not None,
                "deprecated": version.deprecated_at is not None,
                "created_at": version.created_at,
            }
            for version in agent.versions
        ]

    if name == "publish_agent":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        data = AgentPublishRequest.model_validate(
            {"publish": args.get("publish"), "note": args.get("note", "")}
        )
        await agent_service.set_published(
            session, agent, user, publish=data.publish, note=data.note
        )
        await _audit_agent_action(
            session,
            user=user,
            ctx=ctx,
            action="agent.publish" if data.publish else "agent.unpublish",
            agent_id=agent.id,
            data={"note": data.note, "version": agent.current_version},
        )
        await session.commit()
        return _agent_result(await agent_service.get_agent(session, agent_id))

    if name == "suspend_agent":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        data = AgentSuspendRequest.model_validate({"reason": args.get("reason")})
        await agent_service.suspend_agent(session, agent, user, data.reason)
        await _audit_agent_action(
            session,
            user=user,
            ctx=ctx,
            action="agent.suspend",
            agent_id=agent.id,
            data={"reason": data.reason},
        )
        await session.commit()
        return _agent_result(await agent_service.get_agent(session, agent_id))

    if name == "resume_agent":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        await agent_service.resume_agent(session, agent, user)
        await _audit_agent_action(
            session, user=user, ctx=ctx, action="agent.resume", agent_id=agent.id
        )
        await session.commit()
        return _agent_result(await agent_service.get_agent(session, agent_id))

    if name == "archive_agent":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        await agent_service.archive_agent(session, agent, user)
        await _audit_agent_action(
            session, user=user, ctx=ctx, action="agent.archive", agent_id=agent.id
        )
        await session.commit()
        return _agent_result(await agent_service.get_agent(session, agent_id))

    if name == "delete_draft_agent":
        agent_id = _uuid_arg(args, "agent_id")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        if agent.status is AgentStatus.published:
            raise ValidationError(
                "Опубликованного агента нельзя удалить: сначала снимите его с публикации",
                code="agent_published",
            )
        if agent.status is not AgentStatus.draft:
            raise ValidationError(
                "Удалять можно только черновики; для закрытия используйте архивирование",
                code="agent_not_draft",
            )
        slug = agent.slug
        await agent_service.delete_agent(session, agent)
        await audit_service.record(
            session,
            action="agent.delete",
            target_type="agent",
            target_id=agent_id,
            actor_id=user.id,
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            data={"slug": slug},
        )
        await session.commit()
        return {"deleted": True, "agent_id": str(agent_id)}

    if name == "rollback_agent_version":
        agent_id = _uuid_arg(args, "agent_id")
        version_number = args.get("version")
        if (
            isinstance(version_number, bool)
            or not isinstance(version_number, int)
            or version_number < 1
        ):
            raise ValueError("version должен быть целым числом от 1")
        agent = await agent_service.get_agent(session, agent_id)
        agent_service.ensure_can_edit(agent, user)
        if agent.status is not AgentStatus.draft:
            raise ValidationError(
                "Сначала переведите агента в черновик, чтобы откат не стал активным без проверки",
                code="agent_version_requires_draft",
            )
        new_version = await agent_service.rollback_to_version(session, agent, user, version_number)
        await audit_service.record(
            session,
            action="agent.version.rollback",
            target_type="agent_version",
            target_id=new_version.id,
            actor_id=user.id,
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            data={"agent_id": str(agent.id), "from_version": version_number},
        )
        await session.commit()
        return {
            "agent_id": str(agent_id),
            "version_id": str(new_version.id),
            "version": new_version.version,
            "entrypoint_type": new_version.entrypoint_type.value,
            "published": new_version.published_at is not None,
        }

    if name == "list_my_tasks":
        status_filter = args.get("status")
        task_status = TaskStatus(status_filter) if status_filter else None
        items, total = await gateway.list_tasks(
            session,
            requester_id=user.id,
            status=task_status,
            offset=0,
            limit=100,
        )
        return {
            "total": total,
            "tasks": [TaskPublic.model_validate(task).model_dump(mode="json") for task in items],
        }

    if name == "cancel_task":
        task_id = _uuid_arg(args, "task_id")
        data = TaskCancelRequest.model_validate({"reason": args.get("reason", "")})
        task = await gateway.get_task(session, task_id)
        await gateway.cancel_task(session, task, user.id, data.reason)
        await audit_service.record(
            session,
            action="task.cancel",
            target_type="task",
            target_id=task.id,
            actor_id=user.id,
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            data={"reason": data.reason},
        )
        await session.commit()
        return TaskPublic.model_validate(task).model_dump(mode="json")

    if name == "list_my_workflows":
        plans, total = await orchestration_service.list_plans(
            session,
            requester_id=user.id,
            offset=0,
            limit=100,
            is_admin=False,
        )
        return {
            "total": total,
            "workflows": [
                {
                    "plan_id": str(plan.id),
                    "title": plan.title,
                    "status": plan.status.value,
                    "created_at": plan.created_at,
                    "finished_at": plan.finished_at,
                    "error": plan.error,
                }
                for plan in plans
            ],
        }

    if name == "cancel_workflow":
        plan_id = _uuid_arg(args, "plan_id")
        reason = args.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 280:
            raise ValueError("reason должен быть строкой не длиннее 280 символов")
        plan = await orchestration_service.get_plan(session, plan_id)
        orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
        await orchestration_service.cancel_plan(session, plan, reason=reason)
        await audit_service.record(
            session,
            action="plan.cancel",
            target_type="plan",
            target_id=plan.id,
            actor_id=user.id,
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            data={"reason": reason},
        )
        await session.commit()
        return {"plan_id": str(plan.id), "status": plan.status.value}

    if name == "retry_workflow_step":
        plan_id = _uuid_arg(args, "plan_id")
        step_key = args.get("step_key")
        if not isinstance(step_key, str) or not 1 <= len(step_key) <= 120:
            raise ValueError("step_key должен содержать от 1 до 120 символов")
        plan = await orchestration_service.get_plan(session, plan_id)
        orchestration_service.ensure_plan_access(plan, user.id, is_admin=user.is_admin)
        step = await orchestration_service.retry_step(session, plan, step_key)
        await audit_service.record(
            session,
            action="plan.step_retry",
            target_type="plan",
            target_id=plan.id,
            actor_id=user.id,
            ip_address=ctx.ip,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            data={"step": step_key},
        )
        await session.commit()
        return {
            "plan_id": str(plan.id),
            "step_key": step.key,
            "task_id": str(step.task_id) if step.task_id else None,
            "step_status": step.status.value,
            "plan_status": plan.status.value,
        }

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
                raise ValueError("Один из выбранных агентов не опубликован")
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
async def mcp_endpoint(
    payload: dict[str, Any], session: DbSession, user: CurrentUser, ctx: Ctx
) -> Response:
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
                        "Управляй своим агентом через create_agent, create_agent_version, "
                        "test_agent и publish_agent. Ищи опубликованных агентов через "
                        "find_agents, запускай их через run_agent и проверяй задания и планы "
                        "через check_task/check_workflow."
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
        result = await _invoke_tool(params["name"], args, session, user, ctx)
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
