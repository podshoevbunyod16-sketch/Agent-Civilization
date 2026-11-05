"""API открытой экосистемы (этап 8).

Публичная часть (`/ecosystem/*`) — каталог интеграций и шаблоны, доступны
любому авторизованному участнику. Проверка совместимости запускает реальное
исполнение в песочнице вне процесса API.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.errors import PermissionDeniedError
from app.schemas.common import Message
from app.schemas.ecosystem import (
    CompatibilityCheckRead,
    CompatibilityCheckRequest,
    IntegrationCreate,
    IntegrationRead,
    IntegrationUpdate,
    PortalOverview,
    TemplateRead,
)
from app.services import ecosystem as ecosystem_service
from app.services.ecosystem import template_payload

router = APIRouter(tags=["ecosystem"])


# ── Проверка совместимости ──────────────────────────────────────────────


@router.post(
    "/agents/{agent_id}/compatibility",
    response_model=CompatibilityCheckRead,
    summary="Запустить проверку совместимости агента",
)
async def run_compatibility(
    agent_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    data: CompatibilityCheckRequest | None = None,
) -> CompatibilityCheckRead:
    record = await ecosystem_service.run_compatibility_check(
        session,
        agent_id=agent_id,
        user_id=user.id,
        is_admin=user.is_admin,
        data=data or CompatibilityCheckRequest(),
    )
    return CompatibilityCheckRead.model_validate(record)


@router.get(
    "/agents/{agent_id}/compatibility",
    response_model=list[CompatibilityCheckRead],
    summary="История проверок совместимости агента",
)
async def list_compatibility(
    agent_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[CompatibilityCheckRead]:
    # История проверок содержит примеры входа/выхода владельца — наружу не отдаём.
    from app.services import agents as agent_service

    agent = await agent_service.get_agent(session, agent_id)
    if agent.owner_id != user.id and not user.is_admin:
        raise PermissionDeniedError(
            "История проверок доступна только владельцу", code="compat_forbidden"
        )
    rows = await ecosystem_service.list_compatibility_checks(session, agent_id, limit=limit)
    return [CompatibilityCheckRead.model_validate(row) for row in rows]


# ── Интеграции ──────────────────────────────────────────────────────────


@router.get(
    "/integrations",
    response_model=list[IntegrationRead],
    summary="Каталог интеграций",
)
async def list_integrations(
    session: DbSession,
    user: CurrentUser,
    kind: str | None = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    only_mine: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IntegrationRead]:
    rows = await ecosystem_service.list_integrations(
        session,
        user_id=user.id,
        is_admin=user.is_admin,
        kind=kind,
        q=q,
        only_mine=only_mine,
        limit=limit,
    )
    return [IntegrationRead.model_validate(row) for row in rows]


@router.post(
    "/integrations",
    response_model=IntegrationRead,
    status_code=201,
    summary="Добавить интеграцию",
)
async def create_integration(
    session: DbSession, user: CurrentUser, data: IntegrationCreate
) -> IntegrationRead:
    integration = await ecosystem_service.create_integration(
        session, owner_id=user.id, data=data
    )
    return IntegrationRead.model_validate(integration)


@router.get(
    "/integrations/{integration_id}",
    response_model=IntegrationRead,
    summary="Карточка интеграции",
)
async def get_integration(
    integration_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> IntegrationRead:
    integration = await ecosystem_service.get_integration(session, integration_id)
    if not integration.is_public and integration.owner_id != user.id and not user.is_admin:
        from app.core.errors import NotFoundError

        raise NotFoundError("Интеграция не найдена", code="integration_not_found")
    return IntegrationRead.model_validate(integration)


@router.patch(
    "/integrations/{integration_id}",
    response_model=IntegrationRead,
    summary="Изменить интеграцию",
)
async def update_integration(
    integration_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    data: IntegrationUpdate,
) -> IntegrationRead:
    integration = await ecosystem_service.get_integration(session, integration_id)
    updated = await ecosystem_service.update_integration(
        session, integration, user_id=user.id, is_admin=user.is_admin, data=data
    )
    return IntegrationRead.model_validate(updated)


@router.delete(
    "/integrations/{integration_id}", response_model=Message, summary="Удалить интеграцию"
)
async def delete_integration(
    integration_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> Message:
    integration = await ecosystem_service.get_integration(session, integration_id)
    await ecosystem_service.delete_integration(
        session, integration, user_id=user.id, is_admin=user.is_admin
    )
    return Message(detail="Интеграция удалена")


@router.post(
    "/integrations/{integration_id}/probe",
    response_model=IntegrationRead,
    summary="Проверить доступность интеграции",
)
async def probe_integration(
    integration_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> IntegrationRead:
    integration = await ecosystem_service.get_integration(session, integration_id)
    if integration.owner_id != user.id and not user.is_admin:
        raise PermissionDeniedError(
            "Проверять доступность может только владелец", code="integration_forbidden"
        )
    probed = await ecosystem_service.probe_integration(session, integration)
    return IntegrationRead.model_validate(probed)


# ── Developer Portal ────────────────────────────────────────────────────


@router.get(
    "/developer/portal",
    response_model=PortalOverview,
    summary="Сводка Developer Portal",
)
async def portal(session: DbSession, user: CurrentUser) -> PortalOverview:
    # Шаблоны — статический контент платформы: гарантируем наличие, чтобы
    # портал не показывал «0 шаблонов» на свежей базе.
    await ecosystem_service.ensure_templates(session)
    await session.commit()
    return PortalOverview(**await ecosystem_service.portal_overview(session, user_id=user.id))


@router.get(
    "/ecosystem/templates",
    response_model=list[TemplateRead],
    summary="Шаблоны проектов агентов",
)
async def list_templates(
    session: DbSession, user: CurrentUser, runtime: str | None = None
) -> list[TemplateRead]:
    await ecosystem_service.ensure_templates(session)
    await session.commit()
    rows = await ecosystem_service.list_templates(session, runtime=runtime)
    return [TemplateRead(**template_payload(row)) for row in rows]


@router.get(
    "/ecosystem/templates/{slug}",
    response_model=TemplateRead,
    summary="Шаблон проекта",
)
async def get_template(session: DbSession, user: CurrentUser, slug: str) -> TemplateRead:
    await ecosystem_service.ensure_templates(session)
    await session.flush()
    row = await ecosystem_service.get_template_row(session, slug)
    row.downloads += 1
    await session.commit()
    return TemplateRead(**template_payload(row))


@router.get("/ecosystem/stats", summary="Статистика экосистемы")
async def ecosystem_stats(session: DbSession, user: CurrentUser) -> dict:
    await ecosystem_service.ensure_templates(session)
    await session.commit()
    return await ecosystem_service.ecosystem_stats(session)
