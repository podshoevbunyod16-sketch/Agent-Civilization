"""Открытая экосистема (этап 8): совместимость, интеграции, Developer Portal.

Ключевая идея: «совместимость» здесь — не галочка, которую ставят вручную, а
**реальный прогон объявленного контракта**:

1. вход валидируется по `input_schema` версии;
2. выход валидируется по `output_schema`;
3. `test_code` исполняется в песочнице на реальном примере;
4. лимиты манифеста проверяются на вменяемость и пределы платформы;
5. манифест сверяется с опубликованной версией (не разошёлся ли он с API).

Тот же самый валидатор и та же песочница, что и в этапе 5 — поэтому
«совместимо» означает ровно то, что означает «проверено» при доверии.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.db.models.agent import Agent, AgentStatus
from app.db.models.ecosystem import (
    CheckStatus,
    CompatibilityCheck,
    Integration,
    IntegrationKind,
    IntegrationProtocol,
    ProjectTemplate,
)
from app.db.models.task import Task
from app.sandbox.runner import SandboxRunner, SandboxUnavailableError
from app.schemas.ecosystem import CompatibilityCheckRequest, IntegrationCreate, IntegrationUpdate
from app.services import audit as audit_service
from app.services import templates as template_service
from app.services.trust import evaluate_assertions, validate_against_schema

# Пределы платформы. Единственный источник истины — схема версии агента
# (`app/schemas/agent.py::ResourceLimits`): там они проверяются при создании
# версии. Здесь те же значения нужны, чтобы поймать данные, попавшие в БД
# в обход схемы (миграция, ручная правка, старые записи), и не дать двум
# разным потолкам разойтись. Синхронность закреплена тестом.
PLATFORM_LIMITS = {
    "max_wall_seconds": 7200,
    "max_memory_mb": 32768,
    "max_output_bytes": 10_000_000,
}
MIN_LIMITS = {"max_memory_mb": 64}
DEFAULT_LIMITS = {
    "max_wall_seconds": 30,
    "max_memory_mb": 256,
    "max_output_bytes": 65_536,
}
ALLOWED_ENTRYPOINTS = {"python", "http", "docker", "mcp", "sandbox"}
CHECK_WEIGHTS = {
    "manifest": 1.0,
    "input_schema": 1.5,
    "output_schema": 1.5,
    "test_code": 2.0,
    "limits": 1.0,
}
HTTP_CHECK_TIMEOUT = 5.0


def _now() -> datetime:
    return datetime.now(UTC)


async def _get_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent:
    from app.services import agents as agent_service

    return await agent_service.get_agent(session, agent_id)


def _current_version(agent: Agent) -> Any:
    return next((v for v in agent.versions if v.version == agent.current_version), None)


# ── Проверка совместимости ──────────────────────────────────────────────


def check_manifest(agent: Agent, version: Any) -> dict[str, Any]:
    """Сверка манифеста с опубликованной версией: то, что описано, и то, что есть."""
    entrypoint = version.entrypoint_type
    raw = getattr(entrypoint, "value", str(entrypoint))
    problems: list[str] = []
    if raw not in ALLOWED_ENTRYPOINTS:
        problems.append(f"неизвестный тип точки входа: {raw}")
    if raw in {"python", "docker"} and not (version.entrypoint_config or {}).get("code") and not (
        version.entrypoint_config or {}
    ).get("image"):
        problems.append("нет кода или образа в конфигурации точки входа")
    if raw in {"http", "mcp"} and not (version.entrypoint_config or {}).get("url"):
        problems.append("нет url в конфигурации точки входа")
    if agent.status is not AgentStatus.published:
        problems.append(f"агент не опубликован (статус {agent.status.value})")
    if not version.input_schema:
        problems.append("не объявлена входная схема")
    if not version.output_schema:
        problems.append("не объявлена выходная схема")
    return {
        "kind": "manifest",
        "status": "failed" if problems else "passed",
        "message": "; ".join(problems) if problems else "манифест согласован с версией",
        "details": {
            "entrypoint_type": raw,
            "version": version.version,
            "agent_status": agent.status.value,
        },
    }


def check_limits(version: Any) -> dict[str, Any]:
    limits = dict(DEFAULT_LIMITS)
    limits.update(version.resource_limits or {})
    problems: list[str] = []
    for key, ceiling in PLATFORM_LIMITS.items():
        value = limits.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or value <= 0:
            problems.append(f"{key} должен быть положительным числом")
        elif value > ceiling:
            problems.append(f"{key} = {value} превышает предел платформы {ceiling}")
    for key, floor in MIN_LIMITS.items():
        value = limits.get(key)
        if isinstance(value, (int, float)) and value < floor:
            problems.append(f"{key} = {value} меньше минимума {floor}")
    return {
        "kind": "limits",
        "status": "failed" if problems else "passed",
        "message": "; ".join(problems) if problems else "лимиты в допустимых пределах",
        "details": {"limits": limits, "platform": PLATFORM_LIMITS},
    }


def check_input_schema(version: Any, sample_input: dict[str, Any]) -> dict[str, Any]:
    errors = validate_against_schema(sample_input, version.input_schema or {})
    return {
        "kind": "input_schema",
        "status": "failed" if errors else "passed",
        "message": "; ".join(errors[:5]) if errors else "пример входа соответствует схеме",
        "details": {"errors": errors[:20]},
    }


def check_output_schema(version: Any, sample_output: dict[str, Any] | None) -> dict[str, Any]:
    if sample_output is None:
        return {
            "kind": "output_schema",
            "status": "skipped",
            "message": "выход не получен: проверка выполнения не проводилась",
            "details": {},
        }
    errors = validate_against_schema(sample_output, version.output_schema or {})
    return {
        "kind": "output_schema",
        "status": "failed" if errors else "passed",
        "message": "; ".join(errors[:5]) if errors else "выход соответствует объявленной схеме",
        "details": {"errors": errors[:20]},
    }


async def _execute_python(
    version: Any, sample_input: dict[str, Any], runner: SandboxRunner
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Реальный прогон python-агента в песочнице на примере входа."""
    config = version.entrypoint_config or {}
    code = config.get("code") or ""
    if not str(code).strip():
        return None, {
            "kind": "test_code",
            "status": "skipped",
            "message": "код агента не сохранён на платформе (внешний исполняемый контракт)",
            "details": {},
        }
    try:
        result = await runner.run_python(str(code), sample_input)
    except SandboxUnavailableError as exc:
        return None, {
            "kind": "test_code",
            "status": "error",
            "message": f"песочница недоступна: {exc}",
            "details": {},
        }
    if result.status != "succeeded":
        return None, {
            "kind": "test_code",
            "status": "failed",
            "message": f"агент завершился со статусом {result.status}: {result.error}",
            "details": {"status": result.status},
        }
    return result.output, {
        "kind": "test_code",
        "status": "passed",
        "message": "агент отработал на примере входа",
        "details": {"stdout": (result.stdout or "")[-500:]},
    }


async def _run_declared_test(
    version: Any, sample_input: dict[str, Any], output: dict[str, Any], runner: SandboxRunner
) -> dict[str, Any]:
    """Тест-код из манифеста: самопроверка агента в песочнице."""
    test_code = str((version.entrypoint_config or {}).get("test_code") or "").strip()
    if not test_code:
        return {
            "kind": "assertions",
            "status": "skipped",
            "message": "тест-код не объявлен",
            "details": {},
        }
    try:
        result = await runner.run_python(test_code, {"output": output, "input": sample_input})
    except SandboxUnavailableError as exc:
        return {
            "kind": "assertions",
            "status": "error",
            "message": f"песочница недоступна: {exc}",
            "details": {},
        }
    passed = bool(result.output.get("passed", result.output.get("ok"))) and (
        result.status == "succeeded"
    )
    return {
        "kind": "assertions",
        "status": "passed" if passed else "failed",
        "message": "тест-код пройден" if passed else "тест-код не пройден",
        "details": {"output": result.output, "status": result.status},
    }


def _score(checks: list[dict[str, Any]]) -> tuple[float, int, int]:
    total = 0.0
    earned = 0.0
    passed = 0
    failed = 0
    for item in checks:
        weight = CHECK_WEIGHTS.get(item["kind"], 1.0)
        if item["status"] in {"passed", "skipped"}:
            total += weight
            if item["status"] == "passed":
                earned += weight
                passed += 1
            else:
                # «skipped» не наказывает, но и не подтверждает
                earned += weight * 0.5
        else:
            total += weight
            failed += 1
    score = round(100.0 * earned / total, 2) if total else 0.0
    return score, passed, failed


async def run_compatibility_check(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    data: CompatibilityCheckRequest,
    runner: SandboxRunner | None = None,
) -> CompatibilityCheck:
    agent = await _get_agent(session, agent_id)
    if agent.owner_id != user_id and not is_admin:
        raise PermissionDeniedError("Проверка доступна только владельцу агента", code="forbidden")
    version = _current_version(agent)
    if version is None:
        raise ValidationError("У агента нет опубликованной версии", code="agent_without_version")

    sample_input = data.sample_input or dict(
        (version.entrypoint_config or {}).get("sample_input") or {}
    )
    if not sample_input:
        sample_input = {"input": {"text": "проверка совместимости"}}

    record = CompatibilityCheck(
        agent_id=agent.id,
        version_id=version.id,
        requested_by=user_id,
        status=CheckStatus.running,
        sample_input=sample_input,
        is_negative=data.imperfect_output is not None,
    )
    session.add(record)
    await session.flush()

    started = time.perf_counter()
    sandbox = runner or SandboxRunner.from_limits(version.resource_limits)
    checks: list[dict[str, Any]] = []
    sample_output: dict[str, Any] | None = None

    if data.imperfect_output is not None:
        # Явно заданный («испорченный») выход: проверяем, что схема это поймает.
        sample_output = data.imperfect_output
        source = {
            "kind": "test_code",
            "status": "passed",
            "message": "выход предоставлен вызывающим (негативный тест)",
            "details": {},
        }
    else:
        raw = getattr(version.entrypoint_type, "value", str(version.entrypoint_type))
        if raw in {"python"}:
            sample_output, source = await _execute_python(version, sample_input, sandbox)
        elif raw == "http":
            sample_output, source = None, {
                "kind": "test_code",
                "status": "skipped",
                "message": "HTTP-агент: исполнение происходит на узле, здесь проверяется контракт",
                "details": {},
            }
        else:
            sample_output, source = None, {
                "kind": "test_code",
                "status": "skipped",
                "message": f"тип {raw}: исполнение проверяется на узле",
                "details": {},
            }

    checks.append(check_manifest(agent, version))
    checks.append(check_limits(version))
    checks.append(check_input_schema(version, sample_input))
    checks.append(source)
    if sample_output is not None:
        checks.append(await _run_declared_test(version, sample_input, sample_output, sandbox))
    else:
        checks.append(
            {
                "kind": "assertions",
                "status": "skipped",
                "message": "нет выхода для самопроверки",
                "details": {},
            }
        )
    checks.append(check_output_schema(version, sample_output))

    score, passed, failed = _score(checks)
    any_failed = any(item["status"] in {"failed", "error"} for item in checks)
    record.status = CheckStatus.failed if any_failed else CheckStatus.passed
    record.score = score
    record.passed_count = passed
    record.failed_count = failed
    record.checks = checks
    record.sample_output = sample_output
    record.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    record.finished_at = _now()
    if any_failed:
        record.error = "; ".join(
            item["message"] for item in checks if item["status"] in {"failed", "error"}
        )[:2000]

    # Совместимость влияет на витрину, но не заменяет проверку результатов:
    # агент остаётся «заявленным», пока его работа не подтверждена (этап 5).
    await session.flush()
    await audit_service.record(
        session,
        action="ecosystem.compat_check",
        target_type="agent",
        target_id=agent.id,
        actor_id=user_id,
        outcome="success" if record.status is CheckStatus.passed else "failure",
        data={"score": score, "passed": passed, "failed": failed},
    )
    await session.commit()
    return record


async def list_compatibility_checks(
    session: AsyncSession, agent_id: uuid.UUID, *, limit: int = 20
) -> list[CompatibilityCheck]:
    rows = await session.execute(
        select(CompatibilityCheck)
        .where(CompatibilityCheck.agent_id == agent_id)
        .order_by(CompatibilityCheck.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    return list(rows.scalars())


# ── Интеграции ──────────────────────────────────────────────────────────


def _validate_url(url: str) -> str:
    value = (url or "").strip()
    if not value.startswith(("http://", "https://")):
        raise ValidationError("Адрес интеграции должен начинаться с http:// или https://")
    if len(value) > 500:
        raise ValidationError("Слишком длинный адрес интеграции")
    return value


async def create_integration(
    session: AsyncSession, *, owner_id: uuid.UUID, data: IntegrationCreate
) -> Integration:
    existing = await session.execute(
        select(Integration).where(
            Integration.owner_id == owner_id, Integration.slug == data.slug
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("Интеграция с таким slug уже есть", code="integration_slug_taken")
    integration = Integration(
        owner_id=owner_id,
        slug=data.slug,
        name=data.name,
        summary=data.summary,
        description=data.description,
        kind=IntegrationKind(data.kind),
        protocol=IntegrationProtocol(data.protocol),
        base_url=_validate_url(data.base_url),
        docs_url=_validate_url(data.docs_url) if data.docs_url else None,
        manifest=data.manifest or {},
        capabilities=data.capabilities or [],
        is_public=data.is_public,
    )
    session.add(integration)
    await session.flush()
    await audit_service.record(
        session,
        action="ecosystem.integration_create",
        target_type="integration",
        target_id=integration.id,
        actor_id=owner_id,
        data={"slug": integration.slug, "kind": integration.kind.value},
    )
    await session.commit()
    return integration


async def get_integration(session: AsyncSession, integration_id: uuid.UUID) -> Integration:
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise NotFoundError("Интеграция не найдена", code="integration_not_found")
    return integration


async def update_integration(
    session: AsyncSession,
    integration: Integration,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    data: IntegrationUpdate,
) -> Integration:
    if integration.owner_id != user_id and not is_admin:
        raise PermissionDeniedError(
            "Изменять интеграцию может только владелец", code="integration_forbidden"
        )
    payload = data.model_dump(exclude_unset=True)
    for field in ("kind", "protocol"):
        if field in payload and payload[field] is not None:
            payload[field] = (
                IntegrationKind(payload[field])
                if field == "kind"
                else IntegrationProtocol(payload[field])
            )
    if payload.get("base_url"):
        payload["base_url"] = _validate_url(payload["base_url"])
    if payload.get("docs_url"):
        payload["docs_url"] = _validate_url(payload["docs_url"])
    for key, value in payload.items():
        if value is not None:
            setattr(integration, key, value)
    await session.flush()
    await audit_service.record(
        session,
        action="ecosystem.integration_update",
        target_type="integration",
        target_id=integration.id,
        actor_id=user_id,
        data={"fields": sorted(payload.keys())},
    )
    await session.commit()
    return integration


async def delete_integration(
    session: AsyncSession, integration: Integration, *, user_id: uuid.UUID, is_admin: bool
) -> None:
    if integration.owner_id != user_id and not is_admin:
        raise PermissionDeniedError(
            "Удалять интеграцию может только владелец", code="integration_forbidden"
        )
    await audit_service.record(
        session,
        action="ecosystem.integration_delete",
        target_type="integration",
        target_id=integration.id,
        actor_id=user_id,
        data={"slug": integration.slug},
    )
    await session.delete(integration)
    await session.commit()


async def list_integrations(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    is_admin: bool,
    kind: str | None = None,
    q: str | None = None,
    only_mine: bool = False,
    limit: int = 50,
) -> list[Integration]:
    stmt = select(Integration)
    if only_mine:
        stmt = stmt.where(Integration.owner_id == user_id)
    elif not is_admin:
        # Публичный каталог + собственные черновики
        stmt = stmt.where(
            (Integration.is_public.is_(True)) | (Integration.owner_id == user_id)
        )
    if kind:
        stmt = stmt.where(Integration.kind == IntegrationKind(kind))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            (Integration.name.ilike(pattern))
            | (Integration.summary.ilike(pattern))
            | (Integration.slug.ilike(pattern))
        )
    stmt = stmt.order_by(Integration.is_verified.desc(), Integration.created_at.desc())
    stmt = stmt.limit(max(1, min(limit, 200)))
    rows = await session.execute(stmt)
    return list(rows.scalars())


async def probe_integration(session: AsyncSession, integration: Integration) -> Integration:
    """Реальная проверка доступности внешней платформы.

    Выполняется с сервера, поэтому адрес не должен указывать на внутреннюю
    сеть: приватные диапазоны и localhost запрещены (SSRF-защита).
    """
    # При проверке доступности неразрешимое имя — это не SSRF, а обычный
    # сбой сети: он должен записаться как failed-проверка, а не как отказ
    # «внутренний адрес запрещён». Приватные IP отклоняются по-прежнему.
    url = _assert_public_url(integration.base_url, allow_unresolved=True)
    integration.check_count += 1
    started = time.perf_counter()
    try:
        from app.core.net_guard import SafeAsyncClient

        async with SafeAsyncClient(
            timeout=HTTP_CHECK_TIMEOUT,
            allow_private=settings.ssrf_allow_private,
        ) as client:
            response = await client.get(url)
        latency = round((time.perf_counter() - started) * 1000, 2)
        integration.last_checked_at = _now()
        integration.last_latency_ms = latency
        integration.last_status = str(response.status_code)
        ok = response.status_code < 500
        integration.last_error = None if ok else f"HTTP {response.status_code}"
        integration.is_verified = response.status_code < 500
        if not integration.is_verified:
            integration.failure_count += 1
    except Exception as exc:  # сеть, DNS, таймаут
        integration.last_checked_at = _now()
        integration.last_latency_ms = None
        integration.last_status = None
        integration.last_error = str(exc)[:300]
        integration.is_verified = False
        integration.failure_count += 1
    await session.flush()
    await session.commit()
    return integration


def _assert_public_url(url: str, *, allow_unresolved: bool = False) -> str:
    """Проверка адреса интеграции на SSRF (этап 10, A #7).

    Раньше это был строковый блоклист префиксов: `nip.io`, `*.local`,
    `172.16-31.`, IPv6-mapped и редиректы проходили насквозь. Теперь имя
    разрешается в IP и проверяется по диапазонам (см. app/core/net_guard).
    """
    from app.core.net_guard import BlockedAddressError, validate_url

    try:
        validate_url(
            url,
            allow_private=settings.ssrf_allow_private,
            allow_unresolved=allow_unresolved,
        )
    except BlockedAddressError as exc:
        raise ValidationError(
            f"Проверка внутренних адресов запрещена: {exc}",
            code="integration_private_address",
        ) from exc
    return url


# ── Developer Portal ────────────────────────────────────────────────────


async def ensure_templates(session: AsyncSession) -> int:
    """Идемпотентная загрузка шаблонов в БД (вызывается seed и API)."""
    created = 0
    for item in template_service.PROJECT_TEMPLATES:
        existing = await session.execute(
            select(ProjectTemplate).where(ProjectTemplate.slug == item["slug"])
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(
                ProjectTemplate(
                    slug=item["slug"],
                    name=item["name"],
                    summary=item["summary"],
                    description=item.get("description", ""),
                    runtime=item["runtime"],
                    language=item["language"],
                    files=item["files"],
                    manifest=item["manifest"],
                    requires_capabilities=item["requires_capabilities"],
                )
            )
            created += 1
        else:
            row.name = item["name"]
            row.summary = item["summary"]
            row.runtime = item["runtime"]
            row.language = item["language"]
            row.files = item["files"]
            row.manifest = item["manifest"]
            row.requires_capabilities = item["requires_capabilities"]
    await session.flush()
    return created


def template_payload(row: ProjectTemplate) -> dict[str, Any]:
    return {
        "id": row.id,
        "slug": row.slug,
        "name": row.name,
        "summary": row.summary,
        "description": row.description,
        "runtime": row.runtime,
        "language": row.language,
        "files": row.files,
        "manifest": row.manifest,
        "requires_capabilities": row.requires_capabilities,
        "downloads": row.downloads,
        "created_at": row.created_at,
    }


async def list_templates(
    session: AsyncSession, *, runtime: str | None = None
) -> list[ProjectTemplate]:
    stmt = select(ProjectTemplate).where(ProjectTemplate.is_active.is_(True))
    if runtime:
        stmt = stmt.where(ProjectTemplate.runtime == runtime)
    stmt = stmt.order_by(ProjectTemplate.runtime, ProjectTemplate.slug)
    rows = await session.execute(stmt)
    return list(rows.scalars())


async def get_template_row(session: AsyncSession, slug: str) -> ProjectTemplate:
    row = (
        await session.execute(select(ProjectTemplate).where(ProjectTemplate.slug == slug))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Шаблон не найден", code="template_not_found")
    return row


async def portal_overview(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """Сводка Developer Portal: чему не хватает моим агентам, чтобы стать совместимыми."""
    agents = list(
        (
            await session.execute(
                select(Agent).where(Agent.owner_id == user_id).order_by(Agent.created_at.desc())
            )
        ).scalars()
    )
    items = []
    for agent in agents:
        last = (
            await session.execute(
                select(CompatibilityCheck)
                .where(
                    CompatibilityCheck.agent_id == agent.id,
                    CompatibilityCheck.is_negative.is_(False),
                )
                .order_by(CompatibilityCheck.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        last_negative = (
            await session.execute(
                select(CompatibilityCheck)
                .where(
                    CompatibilityCheck.agent_id == agent.id,
                    CompatibilityCheck.is_negative.is_(True),
                )
                .order_by(CompatibilityCheck.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        items.append(
            {
                "agent_id": agent.id,
                "name": agent.name,
                "slug": agent.slug,
                "status": agent.status.value,
                "current_version": agent.current_version,
                "last_check": (
                    {
                        "status": last.status.value,
                        "score": last.score,
                        "finished_at": last.finished_at,
                    }
                    if last
                    else None
                ),
                "last_negative_check": (
                    {
                        "status": last_negative.status.value,
                        "finished_at": last_negative.finished_at,
                    }
                    if last_negative
                    else None
                ),
            }
        )
    ready = sum(1 for item in items if (item["last_check"] or {}).get("status") == "passed")
    return {
        "agents": items,
        "agents_total": len(items),
        "agents_ready": ready,
        "templates": len(await list_templates(session)),
        "integrations": int(
            (
                await session.execute(
                    select(func.count()).select_from(Integration).where(
                        Integration.owner_id == user_id
                    )
                )
            ).scalar_one()
        ),
    }


async def ecosystem_stats(session: AsyncSession) -> dict[str, Any]:
    agent_rows = (
        await session.execute(select(Agent.status, func.count()).group_by(Agent.status))
    ).all()
    check_rows = (
        await session.execute(
            select(CompatibilityCheck.status, func.count()).group_by(CompatibilityCheck.status)
        )
    ).all()
    integration_rows = (
        await session.execute(
            select(Integration.kind, func.count()).group_by(Integration.kind)
        )
    ).all()
    total_tasks = int((await session.execute(select(func.count()).select_from(Task))).scalar_one())
    return {
        "agents": {
            "total": sum(int(count) for _, count in agent_rows),
            "by_status": {
                getattr(status, "value", str(status)): int(count) for status, count in agent_rows
            },
        },
        "checks": {
            "total": sum(int(count) for _, count in check_rows),
            "by_status": {
                getattr(status, "value", str(status)): int(count) for status, count in check_rows
            },
        },
        "integrations": {
            "total": sum(int(count) for _, count in integration_rows),
            "by_kind": {
                getattr(kind, "value", str(kind)): int(count) for kind, count in integration_rows
            },
        },
        "templates": int(
            (await session.execute(select(func.count()).select_from(ProjectTemplate))).scalar_one()
        ),
        "tasks": total_tasks,
    }


__all__ = [
    "ALLOWED_ENTRYPOINTS",
    "DEFAULT_LIMITS",
    "PLATFORM_LIMITS",
    "check_input_schema",
    "check_limits",
    "check_manifest",
    "check_output_schema",
    "create_integration",
    "delete_integration",
    "ecosystem_stats",
    "ensure_templates",
    "evaluate_assertions",
    "get_integration",
    "get_template_row",
    "list_compatibility_checks",
    "list_integrations",
    "list_templates",
    "portal_overview",
    "probe_integration",
    "run_compatibility_check",
    "template_payload",
    "update_integration",
]
