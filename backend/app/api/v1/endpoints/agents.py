from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    AdminUser,
    Ctx,
    CurrentUser,
    DbSession,
    OptionalUser,
    Paging,
    rate_limit_default,
)
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.db.models.agent import AgentStatus
from app.schemas.agent import (
    AgentCapabilitiesReplace,
    AgentCreate,
    AgentListItem,
    AgentPublic,
    AgentPublishRequest,
    AgentSuspendRequest,
    AgentUpdate,
    AgentVersionCreate,
    AgentVersionPublic,
    AgentVersionPublicSafe,
    CapabilityCreate,
    CapabilityPublic,
)
from app.schemas.common import Page
from app.services import agents as agent_service
from app.services import audit as audit_service

router = APIRouter(tags=["agents"])

SortLiteral = Literal["created_at", "published_at", "reputation", "name", "completed_tasks"]


def _public(agent) -> AgentPublic:
    payload = AgentPublic.model_validate(agent)
    payload.versions = [
        AgentVersionPublicSafe.from_model(v) for v in (agent.versions or [])
    ]
    return payload


# ── Возможности (таксономия) ───────────────────────────────────────────
@router.get(
    "/capabilities",
    response_model=list[CapabilityPublic],
    summary="Реестр возможностей",
    tags=["capabilities"],
)
async def list_capabilities(
    session: DbSession,
    category: Annotated[str | None, Query(max_length=60)] = None,
    include_inactive: bool = False,
) -> list[CapabilityPublic]:
    items = await agent_service.list_capabilities(
        session, category=category, active_only=not include_inactive
    )
    return [CapabilityPublic.model_validate(c) for c in items]


@router.post(
    "/capabilities",
    response_model=CapabilityPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать возможность (админ)",
    tags=["capabilities"],
)
async def create_capability(
    data: CapabilityCreate, session: DbSession, admin: AdminUser, ctx: Ctx
) -> CapabilityPublic:
    from app.core.errors import ConflictError
    from app.db.models.capability import Capability

    existing = await agent_service.list_capabilities(session, active_only=False)
    if any(c.key == data.key for c in existing):
        raise ConflictError(f"Возможность «{data.key}» уже существует", code="capability_exists")
    capability = Capability(
        key=data.key,
        name=data.name,
        description=data.description,
        category=data.category,
        created_by=admin.id,
    )
    session.add(capability)
    await session.flush()
    await audit_service.record(
        session,
        action="capability.create",
        target_type="capability",
        target_id=capability.id,
        actor_id=admin.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"key": data.key},
    )
    await session.commit()
    return CapabilityPublic.model_validate(capability)


@router.get(
    "/capabilities/{key}/agents",
    response_model=list[AgentListItem],
    summary="Опубликованные агенты с данной возможностью",
)
async def agents_by_capability(key: str, session: DbSession) -> list[AgentListItem]:
    pairs = await agent_service.capability_usage(session, key.lower())
    return [AgentListItem.model_validate(agent) for agent, _ in pairs]


# ── Каталог ────────────────────────────────────────────────────────────
@router.get(
    "/agents",
    response_model=Page[AgentListItem],
    summary="Поиск агентов с фильтрами",
    dependencies=[Depends(rate_limit_default)],
)
async def search_agents(
    session: DbSession,
    viewer: OptionalUser,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[
        Literal["any", "draft", "published", "suspended", "archived"] | None,
        Query(alias="status"),
    ] = None,
    category: Annotated[str | None, Query(max_length=60)] = None,
    capability: Annotated[list[str] | None, Query()] = None,
    owner_id: uuid.UUID | None = None,
    min_reputation: Annotated[float | None, Query(ge=0, le=100)] = None,
    verified_only: bool = False,
    sort: SortLiteral = "created_at",
    order: Literal["asc", "desc"] = "desc",
) -> Page[AgentListItem]:
    status_value: AgentStatus | str | None
    if status_filter == "any":
        status_value = "any"
    elif status_filter is None:
        status_value = None
    else:
        status_value = AgentStatus(status_filter)

    items, total = await agent_service.search_agents(
        session,
        viewer=viewer,
        q=q,
        status=status_value,
        category=category,
        capabilities=capability or [],
        owner_id=owner_id,
        min_reputation=min_reputation,
        verified_only=verified_only,
        sort=sort,
        order=order,
        offset=paging.offset,
        limit=paging.limit,
    )
    return Page.build(
        [AgentListItem.model_validate(a) for a in items],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post(
    "/agents",
    response_model=AgentPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Создать агента (черновик)",
)
async def create_agent(
    data: AgentCreate, session: DbSession, user: CurrentUser, ctx: Ctx
) -> AgentPublic:
    agent = await agent_service.create_agent(session, user, data)
    await audit_service.record(
        session,
        action="agent.create",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        user_agent=ctx.user_agent,
        request_id=ctx.request_id,
        data={"slug": agent.slug, "capabilities": [c.capability_key for c in agent.capabilities]},
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent.id)
    return _public(agent)


@router.get("/agents/{agent_id}", response_model=AgentPublic, summary="Карточка агента")
async def get_agent(
    agent_id: uuid.UUID, session: DbSession, viewer: OptionalUser
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_view(agent, viewer)
    return _public(agent)


@router.get(
    "/agents/by-slug/{slug}", response_model=AgentPublic, summary="Карточка агента по slug"
)
async def get_agent_by_slug(slug: str, session: DbSession, viewer: OptionalUser) -> AgentPublic:
    agent = await agent_service.get_agent_by_slug(session, slug)
    agent_service.ensure_can_view(agent, viewer)
    return _public(agent)


@router.patch("/agents/{agent_id}", response_model=AgentPublic, summary="Обновить агента")
async def update_agent(
    agent_id: uuid.UUID,
    data: AgentUpdate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    changed = sorted(data.model_dump(exclude_unset=True, exclude={"change_note"}).keys())
    await agent_service.update_agent(session, agent, data)
    await audit_service.record(
        session,
        action="agent.update",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"fields": changed, "note": data.change_note},
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent_id)
    return _public(agent)


@router.put(
    "/agents/{agent_id}/capabilities",
    response_model=AgentPublic,
    summary="Заменить набор возможностей агента",
)
async def replace_capabilities(
    agent_id: uuid.UUID,
    data: AgentCapabilitiesReplace,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    await agent_service.replace_capabilities(session, agent, data, user)
    await audit_service.record(
        session,
        action="agent.capabilities.replace",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"capabilities": [c.capability_key for c in data.capabilities]},
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent_id)
    return _public(agent)


@router.post(
    "/agents/{agent_id}/publish",
    response_model=AgentPublic,
    summary="Публикация / снятие с публикации",
)
async def publish_agent(
    agent_id: uuid.UUID,
    data: AgentPublishRequest,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    await agent_service.set_published(
        session, agent, user, publish=data.publish, note=data.note
    )
    await audit_service.record(
        session,
        action="agent.publish" if data.publish else "agent.unpublish",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"note": data.note, "version": agent.current_version},
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent_id)
    return _public(agent)


@router.post(
    "/agents/{agent_id}/suspend", response_model=AgentPublic, summary="Приостановить агента"
)
async def suspend_agent(
    agent_id: uuid.UUID,
    data: AgentSuspendRequest,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    await agent_service.suspend_agent(session, agent, user, data.reason)
    await audit_service.record(
        session,
        action="agent.suspend",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"reason": data.reason},
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent_id)
    return _public(agent)


@router.post(
    "/agents/{agent_id}/resume", response_model=AgentPublic, summary="Возобновить агента"
)
async def resume_agent(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    await agent_service.resume_agent(session, agent, user)
    await audit_service.record(
        session,
        action="agent.resume",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent_id)
    return _public(agent)


@router.post(
    "/agents/{agent_id}/archive", response_model=AgentPublic, summary="Архивировать агента"
)
async def archive_agent(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    await agent_service.archive_agent(session, agent, user)
    await audit_service.record(
        session,
        action="agent.archive",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent_id)
    return _public(agent)


@router.delete(
    "/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Удалить черновик агента",
)
async def delete_agent(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> Response:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    if agent.status is AgentStatus.published:
        raise ValidationError(
            "Опубликованного агента нельзя удалить: сначала снимите с публикации",
            code="agent_published",
        )
    await audit_service.record(
        session,
        action="agent.delete",
        target_type="agent",
        target_id=agent.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"slug": agent.slug},
    )
    await agent_service.delete_agent(session, agent)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Версии ─────────────────────────────────────────────────────────────
@router.post(
    "/agents/{agent_id}/versions",
    response_model=AgentVersionPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Создать версию агента",
)
async def create_version(
    agent_id: uuid.UUID,
    data: AgentVersionCreate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AgentVersionPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    version = await agent_service.create_version(session, agent, user, data)
    await audit_service.record(
        session,
        action="agent.version.create",
        target_type="agent_version",
        target_id=version.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={
            "agent_id": str(agent.id),
            "version": version.version,
            "entrypoint_type": version.entrypoint_type.value,
            "published": data.publish,
        },
    )
    await session.commit()
    await session.refresh(version)
    return AgentVersionPublic.model_validate(version)


@router.get(
    "/agents/{agent_id}/versions",
    response_model=list[AgentVersionPublicSafe],
    summary="История версий агента",
)
async def list_versions(
    agent_id: uuid.UUID, session: DbSession, viewer: OptionalUser
) -> list[AgentVersionPublicSafe]:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_view(agent, viewer)
    is_owner = viewer is not None and (viewer.is_admin or viewer.id == agent.owner_id)
    if is_owner:
        return [AgentVersionPublic.model_validate(v) for v in agent.versions]
    return [AgentVersionPublicSafe.from_model(v) for v in agent.versions]


@router.get(
    "/agents/{agent_id}/versions/{version_number}",
    response_model=AgentVersionPublicSafe,
    summary="Детали версии",
)
async def get_version(
    agent_id: uuid.UUID,
    version_number: int,
    session: DbSession,
    viewer: OptionalUser,
) -> AgentVersionPublicSafe:
    from sqlalchemy import select

    from app.db.models.agent import AgentVersion

    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_view(agent, viewer)
    version = (
        await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id, AgentVersion.version == version_number
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("Версия не найдена", code="version_not_found")
    is_owner = viewer is not None and (viewer.is_admin or viewer.id == agent.owner_id)
    if is_owner:
        return AgentVersionPublic.model_validate(version)
    return AgentVersionPublicSafe.from_model(version)


@router.post(
    "/agents/{agent_id}/versions/{version_number}/deprecate",
    response_model=AgentVersionPublicSafe,
    summary="Пометить версию устаревшей",
)
async def deprecate_version(
    agent_id: uuid.UUID,
    version_number: int,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AgentVersionPublicSafe:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    version = await agent_service.deprecate_version(session, agent, version_number)
    await audit_service.record(
        session,
        action="agent.version.deprecate",
        target_type="agent_version",
        target_id=version.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"agent_id": str(agent.id), "version": version_number},
    )
    await session.commit()
    return AgentVersionPublicSafe.from_model(version)


@router.post(
    "/agents/{agent_id}/versions/{version_number}/rollback",
    response_model=AgentVersionPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Откат на версию (создаёт новую версию)",
)
async def rollback_version(
    agent_id: uuid.UUID,
    version_number: int,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> AgentVersionPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent_service.ensure_can_edit(agent, user)
    new_version = await agent_service.rollback_to_version(
        session, agent, user, version_number
    )
    await audit_service.record(
        session,
        action="agent.version.rollback",
        target_type="agent_version",
        target_id=new_version.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"agent_id": str(agent.id), "from_version": version_number},
    )
    await session.commit()
    await session.refresh(new_version)
    return AgentVersionPublic.model_validate(new_version)


# ── Админ: верификация агента ──────────────────────────────────────────
@router.post(
    "/agents/{agent_id}/verify",
    response_model=AgentPublic,
    summary="Верификация агента (админ)",
)
async def verify_agent(
    agent_id: uuid.UUID,
    session: DbSession,
    admin: AdminUser,
    ctx: Ctx,
    verified: bool = True,
) -> AgentPublic:
    agent = await agent_service.get_agent(session, agent_id)
    agent.is_verified = verified
    await session.flush()
    await audit_service.record(
        session,
        action="agent.verify" if verified else "agent.unverify",
        target_type="agent",
        target_id=agent.id,
        actor_id=admin.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    agent = await agent_service.get_agent(session, agent_id)
    return _public(agent)


@router.get(
    "/agents/{agent_id}/entrypoint",
    summary="Внутренний конфиг точки входа (только владелец/админ)",
    response_model=dict[str, Any],
)
async def agent_entrypoint(
    agent_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> dict[str, Any]:
    agent = await agent_service.get_agent(session, agent_id)
    if not (user.is_admin or agent.owner_id == user.id):
        raise PermissionDeniedError("Только владелец может видеть внутренний конфиг")
    version = next(
        (v for v in agent.versions if v.version == agent.current_version), None
    )
    if version is None:
        raise NotFoundError("У агента нет версии", code="version_not_found")
    return {
        "version": version.version,
        "entrypoint_type": version.entrypoint_type.value,
        "config": agent_service.pick_entrypoint_config(version, for_execution=True),
        "resource_limits": version.resource_limits,
    }
