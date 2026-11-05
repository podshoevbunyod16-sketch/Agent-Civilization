"""Agent Registry — единый реестр агентов.

Правила доступа:
  * читать опубликованных агентов может кто угодно (каталог);
  * черновики видны только владельцу и админам;
  * изменять агента может только владелец (или админ);
  * публикация требует хотя бы одной версии и хотя бы одной возможности.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.db.models.agent import (
    Agent,
    AgentCapability,
    AgentStatus,
    AgentVersion,
    EntrypointType,
)
from app.db.models.capability import Capability
from app.db.models.user import User
from app.schemas.agent import (
    AgentCapabilitiesReplace,
    AgentCapabilityIn,
    AgentCreate,
    AgentUpdate,
    AgentVersionCreate,
)

# Технические/секретные ключи конфигурации, которые нельзя отдавать наружу
SENSITIVE_CONFIG_KEYS = {
    "code", "api_key", "token", "secret", "password", "env", "headers",
    "authorization", "credentials", "private_key",
}


# ── helpers ────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(UTC)


def sanitize_entrypoint_config(config: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (config or {}).items()
        if key.lower() not in SENSITIVE_CONFIG_KEYS
    }


def _with_relations() -> Select[tuple[Agent]]:
    # populate_existing: коллекции перечитываются из БД, иначе после
    # удаления/добавления возможностей в памяти остаётся устаревший список.
    return (
        select(Agent)
        .options(
            selectinload(Agent.capabilities),
            selectinload(Agent.versions),
            selectinload(Agent.owner),
        )
        .execution_options(populate_existing=True)
    )


async def get_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent:
    stmt = _with_relations().where(Agent.id == agent_id)
    agent = (await session.execute(stmt)).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    return agent


async def get_agent_by_slug(session: AsyncSession, slug: str) -> Agent:
    stmt = _with_relations().where(Agent.slug == slug)
    agent = (await session.execute(stmt)).scalars().unique().one_or_none()
    if agent is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    return agent


def ensure_can_view(agent: Agent, viewer: User | None) -> None:
    if agent.status is AgentStatus.published or agent.status is AgentStatus.suspended:
        return
    if viewer is None:
        raise NotFoundError("Агент не найден", code="agent_not_found")
    if viewer.is_admin or agent.owner_id == viewer.id:
        return
    # Не раскрываем существование чужого черновика
    raise NotFoundError("Агент не найден", code="agent_not_found")


def ensure_can_edit(agent: Agent, actor: User) -> None:
    if actor.is_admin:
        return
    if agent.owner_id != actor.id:
        raise PermissionDeniedError("Только владелец агента может его изменять")


async def _ensure_capabilities_exist(
    session: AsyncSession, keys: list[str], created_by: uuid.UUID | None = None
) -> None:
    """Неизвестные ключи регистрируются как «предложенные» возможности.

    Это позволяет разработчикам объявлять новые возможности, не блокируя
    работу, но каталог остаётся под контролем (is_active=True по умолчанию,
    админ может деактивировать).
    """
    if not keys:
        return
    existing = set(
        (
            await session.execute(select(Capability.key).where(Capability.key.in_(keys)))
        ).scalars()
    )
    for key in keys:
        if key not in existing:
            session.add(
                Capability(
                    key=key,
                    name=key.replace("_", " ").replace(".", " ").title(),
                    description="Возможность, объявленная агентом",
                    category=key.split(".")[0] if "." in key else "general",
                    created_by=created_by,
                )
            )


# ── создание / обновление ──────────────────────────────────────────────
async def create_agent(session: AsyncSession, owner: User, data: AgentCreate) -> Agent:
    agent = Agent(
        slug=data.slug,
        name=data.name,
        summary=data.summary,
        description=data.description,
        category=data.category,
        tags=data.tags,
        homepage_url=str(data.homepage_url) if data.homepage_url else None,
        repository_url=str(data.repository_url) if data.repository_url else None,
        accent_color=data.accent_color,
        metadata_json=data.metadata_json,
        owner_id=owner.id,
        status=AgentStatus.draft,
    )
    session.add(agent)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            f"Агент со slug «{data.slug}» уже существует", code="agent_slug_taken"
        ) from exc

    if data.capabilities:
        await _ensure_capabilities_exist(
            session, [c.capability_key for c in data.capabilities], owner.id
        )
        for item in data.capabilities:
            session.add(
                AgentCapability(
                    agent_id=agent.id,
                    capability_key=item.capability_key,
                    declared_level=item.declared_level,
                    note=item.note,
                )
            )
        await session.flush()
    return await get_agent(session, agent.id)


async def update_agent(session: AsyncSession, agent: Agent, data: AgentUpdate) -> Agent:
    payload = data.model_dump(exclude_unset=True, exclude={"change_note"})
    for field, value in payload.items():
        if value is None:
            continue
        if field in {"homepage_url", "repository_url"}:
            value = str(value)
        setattr(agent, field, value)
    await session.flush()
    return await get_agent(session, agent.id)


async def replace_capabilities(
    session: AsyncSession, agent: Agent, payload: AgentCapabilitiesReplace, actor: User
) -> Agent:
    keys = [c.capability_key for c in payload.capabilities]
    await _ensure_capabilities_exist(session, keys, actor.id)

    current = {c.capability_key: c for c in agent.capabilities}
    incoming = {c.capability_key: c for c in payload.capabilities}

    for key, existing in current.items():
        if key not in incoming:
            await session.delete(existing)
    for key, item in incoming.items():
        if key in current:
            current[key].declared_level = item.declared_level
            current[key].note = item.note
            current[key].verified_level = None
            current[key].verified_at = None
        else:
            session.add(
                AgentCapability(
                    agent_id=agent.id,
                    capability_key=key,
                    declared_level=item.declared_level,
                    note=item.note,
                )
            )
    await session.flush()
    return await get_agent(session, agent.id)


# ── версии ─────────────────────────────────────────────────────────────
async def create_version(
    session: AsyncSession, agent: Agent, actor: User, data: AgentVersionCreate
) -> AgentVersion:
    if agent.status is AgentStatus.archived:
        raise ValidationError("Нельзя добавить версию архивированному агенту")

    max_version = (
        await session.execute(
            select(func.coalesce(func.max(AgentVersion.version), 0)).where(
                AgentVersion.agent_id == agent.id
            )
        )
    ).scalar_one()

    version = AgentVersion(
        agent_id=agent.id,
        version=int(max_version) + 1,
        changelog=data.changelog,
        entrypoint_type=data.entrypoint_type,
        entrypoint_config=data.entrypoint_config,
        input_schema=data.input_schema,
        output_schema=data.output_schema,
        required_capabilities=data.required_capabilities,
        resource_limits=data.resource_limits.model_dump(),
        created_by=actor.id,
        published_at=_now() if data.publish else None,
    )
    session.add(version)
    await session.flush()

    agent.current_version = version.version
    if data.publish:
        _publish_agent(agent, actor)
    await session.flush()
    return version


def _publish_agent(agent: Agent, actor: User) -> None:
    if agent.current_version < 1:
        raise ValidationError("Нельзя опубликовать агента без версии", code="agent_without_version")
    agent.status = AgentStatus.published
    agent.published_at = agent.published_at or _now()
    agent.suspended_at = None


async def set_published(
    session: AsyncSession, agent: Agent, actor: User, *, publish: bool, note: str = ""
) -> Agent:
    if publish:
        if agent.current_version < 1:
            raise ValidationError(
                "Сначала добавьте хотя бы одну версию агента", code="agent_without_version"
            )
        if not agent.capabilities:
            raise ValidationError(
                "Нельзя опубликовать агента без объявленных возможностей",
                code="agent_without_capabilities",
            )
        if agent.status is AgentStatus.archived:
            raise ValidationError("Архивированного агента нельзя опубликовать")
        _publish_agent(agent, actor)
        published_version = next(
            (v for v in agent.versions if v.version == agent.current_version), None
        )
        if published_version and published_version.published_at is None:
            published_version.published_at = _now()
    else:
        if agent.status is not AgentStatus.published:
            raise ValidationError("Снять с публикации можно только опубликованного агента")
        agent.status = AgentStatus.draft
        agent.published_at = None
    await session.flush()
    return await get_agent(session, agent.id)


async def suspend_agent(
    session: AsyncSession, agent: Agent, actor: User, reason: str
) -> Agent:
    if agent.status is AgentStatus.suspended:
        raise ValidationError("Агент уже приостановлен")
    agent.status = AgentStatus.suspended
    agent.suspended_at = _now()
    agent.metadata_json = {**(agent.metadata_json or {}), "suspension_reason": reason}
    await session.flush()
    return await get_agent(session, agent.id)


async def resume_agent(session: AsyncSession, agent: Agent, actor: User) -> Agent:
    if agent.status is not AgentStatus.suspended:
        raise ValidationError("Возобновить можно только приостановленного агента")
    if agent.current_version < 1:
        raise ValidationError("У агента нет версии", code="agent_without_version")
    agent.status = AgentStatus.published
    agent.suspended_at = None
    agent.metadata_json = {
        k: v for k, v in (agent.metadata_json or {}).items() if k != "suspension_reason"
    }
    await session.flush()
    return await get_agent(session, agent.id)


async def archive_agent(session: AsyncSession, agent: Agent, actor: User) -> Agent:
    agent.status = AgentStatus.archived
    agent.published_at = None
    await session.flush()
    return await get_agent(session, agent.id)


async def delete_agent(session: AsyncSession, agent: Agent) -> None:
    """Удаляет черновик. История заданий — это аудит, её нельзя терять.

    Если у агента уже есть задания, удаление превратило бы историю в сироту,
    поэтому вместо удаления предлагается архивирование.
    """
    from app.db.models.task import Task

    task_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Task).where(Task.agent_id == agent.id)
            )
        ).scalar_one()
    )
    if task_count:
        raise ConflictError(
            "У агента есть история заданий: используйте архивирование вместо удаления",
            code="agent_has_tasks",
            details={"tasks": task_count},
        )
    await session.delete(agent)
    await session.flush()


async def deprecate_version(
    session: AsyncSession, agent: Agent, version_number: int
) -> AgentVersion:
    version = (
        await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id, AgentVersion.version == version_number
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("Версия не найдена", code="version_not_found")
    version.deprecated_at = _now()
    await session.flush()
    return version


async def rollback_to_version(
    session: AsyncSession, agent: Agent, actor: User, version_number: int
) -> AgentVersion:
    """Возврат на предыдущую версию: создаётся новая версия с конфигом старой.

    История версий неизменяема, поэтому «откат» — это новая версия.
    """
    source = (
        await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id, AgentVersion.version == version_number
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise NotFoundError("Версия не найдена", code="version_not_found")
    clone = AgentVersionCreate(
        changelog=f"Откат к версии {version_number}",
        entrypoint_type=source.entrypoint_type,
        entrypoint_config=source.entrypoint_config,
        input_schema=source.input_schema,
        output_schema=source.output_schema,
        required_capabilities=source.required_capabilities,
        resource_limits=source.resource_limits,
        publish=False,
    )
    return await create_version(session, agent, actor, clone)


# ── поиск ──────────────────────────────────────────────────────────────
async def search_agents(
    session: AsyncSession,
    *,
    viewer: User | None,
    q: str | None = None,
    status: AgentStatus | Literal["any"] | None = None,
    category: str | None = None,
    capabilities: list[str] | None = None,
    owner_id: uuid.UUID | None = None,
    min_reputation: float | None = None,
    verified_only: bool = False,
    sort: str = "created_at",
    order: str = "desc",
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Agent], int]:
    stmt = _with_relations()
    count_stmt = select(func.count(func.distinct(Agent.id))).select_from(Agent)

    if capabilities:
        stmt = stmt.join(AgentCapability, AgentCapability.agent_id == Agent.id)
        count_stmt = count_stmt.join(AgentCapability, AgentCapability.agent_id == Agent.id)

    filters = []

    if capabilities:
        filters.append(AgentCapability.capability_key.in_([c.lower() for c in capabilities]))

    if status == "any":
        if viewer is None:
            filters.append(Agent.status == AgentStatus.published)
        elif not viewer.is_admin:
            filters.append(
                or_(Agent.status == AgentStatus.published, Agent.owner_id == viewer.id)
            )
    elif status is not None:
        filters.append(Agent.status == status)
        if viewer is None:
            filters.append(Agent.status == AgentStatus.published)
    else:
        # По умолчанию — только видимые агенту
        if viewer is None:
            filters.append(Agent.status == AgentStatus.published)
        elif not viewer.is_admin:
            filters.append(
                or_(Agent.status == AgentStatus.published, Agent.owner_id == viewer.id)
            )

    if category:
        filters.append(Agent.category == category)
    if owner_id:
        filters.append(Agent.owner_id == owner_id)
    if min_reputation is not None:
        filters.append(Agent.reputation_score >= min_reputation)
    if verified_only:
        filters.append(Agent.is_verified.is_(True))
    if q:
        pattern = f"%{q.strip().lower()}%"
        filters.append(
            or_(
                func.lower(Agent.name).like(pattern),
                func.lower(Agent.summary).like(pattern),
                func.lower(Agent.description).like(pattern),
                Agent.slug.like(pattern),
            )
        )

    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)

    sort_columns = {
        "created_at": Agent.created_at,
        "published_at": Agent.published_at,
        "reputation": Agent.reputation_score,
        "name": Agent.name,
        "completed_tasks": Agent.completed_tasks,
    }
    column = sort_columns.get(sort, Agent.created_at)
    stmt = stmt.order_by(column.asc() if order == "asc" else column.desc(), Agent.id)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(stmt.offset(offset).limit(limit))
    ).scalars().unique().all()
    return list(rows), total


async def list_capabilities(
    session: AsyncSession, *, category: str | None = None, active_only: bool = True
) -> list[Capability]:
    stmt = select(Capability).order_by(Capability.key)
    if active_only:
        stmt = stmt.where(Capability.is_active.is_(True))
    if category:
        stmt = stmt.where(Capability.category == category)
    return list((await session.execute(stmt)).scalars().all())


async def capability_usage(
    session: AsyncSession, capability_key: str
) -> list[tuple[Agent, AgentCapability]]:
    stmt = (
        select(Agent, AgentCapability)
        .join(AgentCapability, AgentCapability.agent_id == Agent.id)
        .where(
            AgentCapability.capability_key == capability_key,
            Agent.status == AgentStatus.published,
        )
        .order_by(Agent.reputation_score.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


def pick_entrypoint_config(version: AgentVersion, *, for_execution: bool) -> dict[str, Any]:
    """Для исполнения нужен полный конфиг; наружу — только безопасная часть."""
    if for_execution:
        return dict(version.entrypoint_config or {})
    return sanitize_entrypoint_config(version.entrypoint_config)


def entrypoint_supported(version: AgentVersion) -> bool:
    return version.entrypoint_type in {
        EntrypointType.http,
        EntrypointType.python,
        EntrypointType.docker,
        EntrypointType.mcp,
    }


def capability_keys(agent: Agent) -> set[str]:
    return {c.capability_key for c in agent.capabilities}


def capability_rules_matched(item: AgentCapabilityIn) -> bool:  # pragma: no cover
    return 0 <= item.declared_level <= 5
