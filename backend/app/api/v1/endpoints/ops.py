"""Эксплуатация (этап 9): метрики, состояние системы, обслуживание, бэкап.

Разделение доступа осознанное:

* `/metrics` — публичный формат Prometheus, но **без токена** отдаётся только
  если `METRICS_PUBLIC=true` (удобно для scrape внутри кластера). Иначе нужен
  сервисный токен: метрики раскрывают нагрузку и размеры таблиц.
* `/ops/health` и `/ops/maintenance` — админ (или сервисный токен для
  воркеров-демонов).
* `/ops/backup` — админ: экспорт логического снимка в JSON-строки.

Никаких «кнопок перезагрузки»: maintenance выполняет только идемпотентные
операции, которые безопасно повторять (снять истёкшие аренды, пометить
мёртвые узлы, истечь сообщения, обновить gauge'и).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import text

from app.api.deps import AdminUser, Ctx, DbSession, rate_limit_critical
from app.core import db_metrics
from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.metrics import metrics, process_stats
from app.services import audit as audit_service
from app.services import gateway, observability
from app.services import messaging as messaging_service
from app.services import nodes as node_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


def _service_or_admin(authorization: str | None, token: str | None) -> bool:
    expected = settings.internal_service_token
    if not expected:
        return False
    provided = token or ""
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    return provided == expected


async def render_metrics(
    session: DbSession, authorization: str | None, x_service_token: str | None
) -> Response:
    """Общая реализация для `/metrics` (корень) и `/api/v1/metrics`."""
    if not settings.metrics_public and not _service_or_admin(authorization, x_service_token):
        raise AuthenticationError(
            "Метрики требуют сервисный токен (или METRICS_PUBLIC=true)",
            code="metrics_forbidden",
        )
    try:
        await observability.collect_gauges(session)
    except Exception:  # БД недоступна: отдаём метрики процесса, а не 500
        metrics.db_collect_errors += 1
        logger.warning("metrics_collect_failed", extra={"reason": "collect_gauges"})
    # Пул соединений снимается после доменных gauge'ей (иначе они перезапишут
    # его) и независимо от них: при деградировавшей базе `collect_gauges`
    # упадёт, а состояние пула — нет, и именно оно нужно дежурному первым
    # (этап 10, G).
    await db_metrics.collect_pool_metrics()
    body = metrics.render_prometheus()
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get(
    "/metrics",
    summary="Метрики в формате Prometheus",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
async def prometheus_metrics(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_service_token: Annotated[str | None, Header()] = None,
) -> Response:
    return await render_metrics(session, authorization, x_service_token)


def runtime_config() -> dict[str, Any]:
    """Не секретные параметры, при которых реально работает процесс.

    Нужны для разбора инцидентов и для замеров: числа из отчёта о нагрузке
    имеют смысл только вместе с конфигурацией, на которой они получены
    (лимиты, размер пула, интервалы воркеров). Секретов здесь нет — только
    факт их наличия булевыми флагами.
    """
    return {
        "environment": settings.environment,
        "debug": settings.debug,
        "rate_limits_per_minute": {
            "default": settings.rate_limit_default_per_minute,
            "auth": settings.rate_limit_auth_per_minute,
            "write": settings.rate_limit_write_per_minute,
            "critical": settings.rate_limit_critical_per_minute,
            "node": settings.rate_limit_node_per_minute,
        },
        "database_pool": {
            "size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
        },
        "workers": {
            "poll_interval_seconds": settings.worker_poll_interval_seconds,
            "lease_seconds": settings.worker_lease_seconds,
            "maintenance_interval_seconds": settings.maintenance_interval_seconds,
            "backup_interval_seconds": settings.backup_interval_seconds,
        },
        "objectives": {
            "rpo_target_seconds": settings.rpo_target_seconds,
            "rto_target_seconds": settings.rto_target_seconds,
        },
        "sandbox": {
            "backend": settings.sandbox_backend,
            "allow_network": settings.sandbox_allow_network,
        },
        "secrets_configured": {
            "secret_encryption_keys": bool(settings.secret_encryption_keys),
            "backup_encryption_key": bool(settings.backup_encryption_key),
            "internal_service_token": bool(settings.internal_service_token),
        },
    }


@router.get("/ops/health", summary="Состояние системы для эксплуатации")
async def ops_health(session: DbSession, user: AdminUser) -> dict[str, Any]:
    report = await observability.health_report(session)
    report["process"] = process_stats()
    report["database"] = await _database_stats(session)
    report["config"] = runtime_config()
    return report


@router.get("/ops/metrics", summary="Снимок метрик в JSON")
async def ops_metrics(session: DbSession, user: AdminUser) -> dict[str, Any]:
    await observability.collect_gauges(session)
    snapshot = metrics.snapshot()
    snapshot["process"] = process_stats()
    return snapshot


@router.get("/ops/tables", summary="Размеры таблиц")
async def ops_tables(session: DbSession, user: AdminUser) -> dict[str, Any]:
    sizes = await observability.table_sizes(session)
    return {
        "tables": sizes,
        "total_rows": sum(item["rows"] for item in sizes),
    }


@router.post(
    "/ops/maintenance",
    summary="Обслуживание: аренды, просрочка, узлы, сообщения",
)
async def run_maintenance(
    session: DbSession,
    user: AdminUser,
    stale_after_seconds: Annotated[int, Query(ge=30, le=3600)] = 120,
) -> dict[str, Any]:
    """Идемпотентный проход обслуживания.

    В production это делает демон (`python -m app.worker.maintenance`), но
    ручной запуск нужен при разборе инцидента: он не создаёт побочных
    эффектов сверх «вернуть в очередь / пометить offline / закрыть
    просроченное».
    """
    started = time.perf_counter()
    requeued = await gateway.expire_leases(session)
    overdue_closed = await gateway.expire_overdue_tasks(session)
    offline = await node_service.mark_stale_nodes_offline(
        session, stale_after_seconds=stale_after_seconds
    )
    expired = await messaging_service.expire_messages(session)
    await session.commit()
    gauges = await observability.collect_gauges(session)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    metrics.incr("maintenance.runs")
    metrics.observe_duration("maintenance", duration_ms)
    return {
        "requeued_tasks": requeued,
        "overdue_closed": overdue_closed,
        "nodes_offline": offline,
        "expired_messages": expired,
        "duration_ms": duration_ms,
        "gauges": gauges,
    }


@router.get("/ops/backup", summary="Логический снимок данных (JSON Lines)")
async def backup(
    session: DbSession,
    user: AdminUser,
    tables: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=50000)] = 20000,
) -> Response:
    """Экспорт таблиц в JSON Lines, сжатый gzip.

    Это **логический** снимок для переноса и проверки, а не замена
    `pg_dump`/PITR: транзакционной согласованности между таблицами он не
    гарантирует (каждая читается своим запросом). Так и написано в отчёте.
    """
    allowed = {
        "users",
        "agents",
        "agent_versions",
        "agent_capabilities",
        "tasks",
        "task_events",
        "infrastructure_nodes",
        "agent_messages",
        "conversations",
        "accounts",
        "transactions",
        "ledger_entries",
        "reward_grants",
        "compatibility_checks",
        "integrations",
        "project_templates",
    }
    requested = [item.strip() for item in (tables or "").split(",") if item.strip()]
    selected = requested or sorted(allowed)
    unknown = [item for item in selected if item not in allowed]
    if unknown:
        from app.core.errors import ValidationError

        raise ValidationError(
            f"Неизвестные таблицы: {', '.join(unknown)}", code="backup_table_unknown"
        )

    started = time.perf_counter()
    lines: list[str] = []
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    for table in selected:
        # Имена таблиц берутся из белого списка выше, значения — параметрами.
        statement = text(f'SELECT row_to_json(t) FROM "{table}" t LIMIT :lim')
        result = await session.execute(statement.bindparams(lim=limit))
        rows = [row[0] for row in result]
        counts[table] = len(rows)
        for row in rows:
            payload = json.dumps({"table": table, "row": row}, ensure_ascii=False, default=str)
            lines.append(payload)
            digest.update(payload.encode())
    header = json.dumps(
        {
            "format": "agent-civilization-backup",
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "tables": counts,
            "note": "логический снимок, не заменяет pg_dump/PITR",
        },
        ensure_ascii=False,
    )
    body = ("\n".join([header, *lines]) + "\n").encode()
    compressed = gzip.compress(body, compresslevel=6)
    digest.update(compressed)
    metrics.incr("backup.export")
    metrics.observe_duration("backup", (time.perf_counter() - started) * 1000)
    return Response(
        content=compressed,
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="ac-backup.jsonl.gz"',
            "X-Checksum-Sha256": digest.hexdigest(),
            "X-Backup-Rows": str(sum(counts.values())),
        },
    )


@router.get("/ops/readiness", summary="Готовность к нагрузке: узлы и очередь")
async def readiness(session: DbSession, user: AdminUser) -> dict[str, Any]:
    gauges = await observability.collect_gauges(session)
    nodes = await node_service.node_stats(session)
    tasks = await gateway.task_stats(session)
    capacity_ok = gauges["nodes_online"] > 0 or gauges["tasks_queued"] == 0
    return {
        "capacity_ok": capacity_ok,
        "nodes": nodes,
        "tasks": tasks,
        "gauges": gauges,
    }


async def _database_stats(session: DbSession) -> dict[str, Any]:
    started = time.perf_counter()
    row = (
        await session.execute(
            text(
                "SELECT current_setting('max_connections')::int AS max_conn, "
                "(SELECT count(*) FROM pg_stat_activity) AS active, "
                "pg_database_size(current_database()) AS size_bytes"
            )
        )
    ).one()
    latency = round((time.perf_counter() - started) * 1000, 2)
    return {
        "max_connections": int(row[0]),
        "active_connections": int(row[1]),
        "size_bytes": int(row[2]),
        "latency_ms": latency,
    }


__all__ = ["router", "runtime_config"]


# ══════════════════════════════════════════════════════════════════════
# Этап 10, F: резервные копии — создание, список, проверка восстановлением
# ══════════════════════════════════════════════════════════════════════
@router.post(
    "/ops/backups",
    dependencies=[Depends(rate_limit_critical)],
    summary="Создать зашифрованный бэкап (админ)",
)
async def create_backup(session: DbSession, user: AdminUser, ctx: Ctx) -> dict[str, object]:
    """Снимок в отдельный каталог: gzip + AES-256-GCM + манифест.

    Ответ содержит манифест (число строк по таблицам, checksum) — по нему
    можно проверить целостность, не читая файл целиком.
    """
    from app.services import backup as backup_service

    manifest, path = await backup_service.create_backup(session)
    await audit_service.record(
        session,
        action="ops.backup_created",
        target_type="backup",
        # В аудит — имя файла (target_id ограничен 64 символами), полный путь
        # возвращается в ответе и виден в логах воркера/API.
        target_id=path.name if path else None,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"rows": manifest.rows, "checksum": manifest.checksum_plain},
    )
    await session.commit()
    return {"manifest": manifest.to_dict(), "path": str(path) if path else None}


@router.get("/ops/backups", summary="Список бэкапов (админ)")
async def list_backups(user: AdminUser) -> dict[str, object]:
    import asyncio
    from pathlib import Path

    from app.core.config import settings

    def _scan() -> list[dict[str, object]]:
        directory = Path(settings.backup_dir)
        found: list[dict[str, object]] = []
        if directory.exists():
            for path in sorted(directory.glob("*.acbk"), reverse=True)[:100]:
                stat = path.stat()
                found.append(
                    {
                        "file": path.name,
                        "bytes": stat.st_size,
                        "has_manifest": Path(str(path) + ".json").exists(),
                        "created_at": stat.st_mtime,
                    }
                )
        return found

    items = await asyncio.to_thread(_scan)
    return {
        "directory": settings.backup_dir,
        "items": items,
        "keep_days": settings.backup_keep_days,
    }


@router.post(
    "/ops/backups/{filename}/verify",
    dependencies=[Depends(rate_limit_critical)],
    summary="Проверить бэкап реальным восстановлением (админ)",
)
async def verify_backup(
    filename: str, session: DbSession, user: AdminUser, ctx: Ctx
) -> dict[str, object]:
    """Восстанавливает бэкап в отдельную схему и сверяет с манифестом.

    Это и есть «проверенный бэкап»: файл читается, расшифровывается,
    checksum сверяется, строки вставляются в БД и пересчитываются.
    Разрушения нет — данные идут в схему `restore_<timestamp>`.
    """
    from pathlib import Path

    from app.core.config import settings
    from app.core.errors import NotFoundError, ValidationError
    from app.services import backup as backup_service

    # Защита от обхода каталога: имя файла — только базовое имя
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValidationError("Некорректное имя файла", code="backup_name_invalid")
    import asyncio

    path = Path(settings.backup_dir) / filename
    if not await asyncio.to_thread(path.exists):
        raise NotFoundError("Бэкап не найден", code="backup_not_found")

    result = await backup_service.restore_backup(session, path)
    await audit_service.record(
        session,
        action="ops.backup_verified",
        target_type="backup",
        target_id=filename,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"ok": result["ok"], "rows": result["rows_restored"]},
    )
    await session.commit()
    return result


@router.delete(
    "/ops/backups/{filename}",
    dependencies=[Depends(rate_limit_critical)],
    summary="Удалить бэкап (админ)",
)
async def delete_backup(
    filename: str, session: DbSession, user: AdminUser, ctx: Ctx
) -> dict[str, object]:
    from pathlib import Path

    from app.core.config import settings
    from app.core.errors import NotFoundError, ValidationError

    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValidationError("Некорректное имя файла", code="backup_name_invalid")
    import asyncio

    path = Path(settings.backup_dir) / filename
    if not await asyncio.to_thread(path.exists):
        raise NotFoundError("Бэкап не найден", code="backup_not_found")

    def _remove() -> None:
        path.unlink()
        # Манифест удаляется вместе с файлом: «сирота» вводила бы в заблуждение
        manifest = Path(str(path) + ".json")
        if manifest.exists():
            manifest.unlink()

    await asyncio.to_thread(_remove)
    await audit_service.record(
        session,
        action="ops.backup_deleted",
        target_type="backup",
        target_id=filename,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return {"deleted": filename}
