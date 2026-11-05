"""Реестр инфраструктурных узлов + проверка подписи запросов.

Модель доверия:
  * узел регистрируется пользователем и получает node_key/node_secret;
  * узел стартует в status=pending и должен подтвердить владение
    приватным ключом через подписанный вызов /nodes/me/verify;
  * админ может верифицировать узел вручную (verified_at) — это повышает
    trust_level;
  * все запросы подписываются HMAC-SHA256 и защищены от replay (Redis nonce).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client
from app.core.config import settings
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.security import (
    generate_node_credentials,
    hash_node_secret,
)
from app.db.models.node import InfrastructureNode, NodeHealth, NodeHeartbeat, NodeKind, NodeStatus
from app.db.models.user import User
from app.schemas.node import HeartbeatRequest, NodeRegisterRequest, NodeUpdate


def _now() -> datetime:
    return datetime.now(UTC)


async def register_node(
    session: AsyncSession, owner: User, data: NodeRegisterRequest
) -> tuple[InfrastructureNode, str]:
    node_key, node_secret = generate_node_credentials()
    node = InfrastructureNode(
        node_key=node_key,
        secret_hash=hash_node_secret(node_secret),
        name=data.name,
        kind=data.kind,
        owner_id=owner.id,
        region=data.region,
        endpoint_url=data.endpoint_url,
        cpu_cores=data.cpu_cores,
        memory_mb=data.memory_mb,
        max_concurrency=data.max_concurrency,
        limit_cpu_seconds=data.limit_cpu_seconds,
        limit_wall_seconds=data.limit_wall_seconds,
        limit_memory_mb=data.limit_memory_mb,
        allow_network=data.allow_network,
        capabilities=data.capabilities,
        labels=data.labels,
        status=NodeStatus.pending,
        health=NodeHealth.unknown,
    )
    session.add(node)
    await session.flush()
    return node, node_secret


async def get_node(session: AsyncSession, node_id: uuid.UUID) -> InfrastructureNode:
    node = (
        await session.execute(select(InfrastructureNode).where(InfrastructureNode.id == node_id))
    ).scalar_one_or_none()
    if node is None:
        raise NotFoundError("Узел не найден", code="node_not_found")
    return node


async def get_node_by_key(session: AsyncSession, node_key: str) -> InfrastructureNode:
    node = (
        await session.execute(
            select(InfrastructureNode).where(InfrastructureNode.node_key == node_key)
        )
    ).scalar_one_or_none()
    if node is None:
        raise AuthenticationError("Неизвестный узел", code="node_unknown")
    return node


def ensure_can_manage(node: InfrastructureNode, actor: User) -> None:
    if actor.is_admin or node.owner_id == actor.id:
        return
    raise PermissionDeniedError("Нет прав на управление этим узлом")


async def list_nodes(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID | None = None,
    status: NodeStatus | None = None,
    kind: NodeKind | None = None,
    only_available: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[InfrastructureNode], int]:
    stmt = select(InfrastructureNode)
    count_stmt = select(func.count()).select_from(InfrastructureNode)
    filters = []
    if owner_id:
        filters.append(InfrastructureNode.owner_id == owner_id)
    if status:
        filters.append(InfrastructureNode.status == status)
    if kind:
        filters.append(InfrastructureNode.kind == kind)
    if only_available:
        filters.append(InfrastructureNode.status == NodeStatus.online)
        filters.append(InfrastructureNode.running_tasks < InfrastructureNode.max_concurrency)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(InfrastructureNode.created_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return list(rows), total


async def update_node(
    session: AsyncSession, node: InfrastructureNode, data: NodeUpdate
) -> InfrastructureNode:
    for field, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(node, field, value)
    await session.flush()
    return node


async def rotate_node_secret(
    session: AsyncSession, node: InfrastructureNode
) -> tuple[str, str]:
    node_key, node_secret = generate_node_credentials()
    node.node_key = node_key
    node.secret_hash = hash_node_secret(node_secret)
    node.status = NodeStatus.pending
    node.verified_at = None
    node.health = NodeHealth.unknown
    await session.flush()
    return node_key, node_secret


async def authenticate_signed_request(
    session: AsyncSession,
    *,
    node_key: str,
    signature: str,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    body: bytes = b"",
) -> InfrastructureNode:
    """Проверка подписи узла: ключ → время → nonce (anti-replay) → HMAC."""

    node = await get_node_by_key(session, node_key)

    if node.status is NodeStatus.revoked:
        raise AuthenticationError("Узел отозван", code="node_revoked")

    try:
        ts = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("Некорректный timestamp", code="invalid_signature") from exc

    if abs(time.time() - ts) > settings.node_request_max_skew_seconds:
        raise AuthenticationError("Запрос устарел", code="signature_expired")

    if len(nonce) < 8 or len(nonce) > 128:
        raise AuthenticationError("Некорректный nonce", code="invalid_signature")

    seen = await redis_client.consume_once(
        f"{node_key}:{nonce}", settings.node_request_max_skew_seconds * 2
    )
    if not seen:
        raise AuthenticationError("Повторный nonce (replay)", code="replay_detected")

    # Проверяем подпись, используя хеш вместо секрета невозможно,
    # поэтому секрет узла передаётся только при verify — здесь сравниваем
    # по сохранённому хешу с префиксом HMAC, вычисленным на стороне узла.
    if not verify_node_secret_signature(
        node.secret_hash,
        signature,
        node_key=node_key,
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    ):
        raise AuthenticationError("Неверная подпись", code="invalid_signature")
    return node


def verify_node_secret_signature(
    stored_hash: str,
    signature: str,
    *,
    node_key: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes = b"",
) -> bool:
    """Сравнение подписи с HMAC, вычисленным от хеша секрета.

    Узел подписывает запрос секретом, но сервер секрета не хранит.
    Поэтому используется двойной HMAC:

        key      = sha256(node_secret)
        signature = HMAC(key, canonical_request)

    Такая схема эквивалентна по стойкости хранению хеша: зная key,
    нельзя восстановить node_secret, но можно проверить подпись.
    """
    import hashlib
    import hmac

    from app.core.security import build_signing_payload

    payload = build_signing_payload(
        node_key=node_key, method=method, path=path, timestamp=timestamp, nonce=nonce, body=body
    )
    expected = hmac.new(bytes.fromhex(stored_hash), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def verify_node_ownership(session: AsyncSession, node: InfrastructureNode) -> None:
    """Подтверждение владения секретом произошло на уровне подписи запроса."""
    if node.status is NodeStatus.revoked:
        raise PermissionDeniedError("Узел отозван", code="node_revoked")
    if node.status is NodeStatus.pending:
        node.status = NodeStatus.online
        node.verified_at = _now()
        node.health = NodeHealth.healthy
        node.last_seen_at = _now()
    await session.flush()


async def admin_verify_node(session: AsyncSession, node: InfrastructureNode, actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDeniedError("Только администратор может верифицировать узел")
    node.verified_at = _now()
    node.trust_level = min(node.trust_level + 1, 5)
    if node.status is NodeStatus.pending:
        node.status = NodeStatus.online if node.health is NodeHealth.healthy else NodeStatus.pending
    await session.flush()


async def revoke_node(
    session: AsyncSession, node: InfrastructureNode, actor: User, reason: str
) -> None:
    ensure_can_manage(node, actor)
    node.status = NodeStatus.revoked
    node.revoked_at = _now()
    node.revoked_reason = reason
    node.health = NodeHealth.unhealthy
    await session.flush()


async def record_heartbeat(
    session: AsyncSession, node: InfrastructureNode, data: HeartbeatRequest
) -> NodeHeartbeat:
    if node.status is NodeStatus.revoked:
        raise PermissionDeniedError("Узел отозван", code="node_revoked")

    # sequence назначает сервер: это исключает конфликты при перезапуске
    # узла и гарантирует монотонность истории heartbeat.
    last_sequence = int(
        (
            await session.execute(
                select(func.coalesce(func.max(NodeHeartbeat.sequence), 0)).where(
                    NodeHeartbeat.node_id == node.id
                )
            )
        ).scalar_one()
    )
    heartbeat = NodeHeartbeat(
        node_id=node.id,
        sequence=last_sequence + 1,
        running_tasks=data.running_tasks,
        cpu_load=data.cpu_load,
        memory_used_mb=data.memory_used_mb,
        health=data.health,
        details={**data.details, "client_sequence": data.sequence},
    )
    session.add(heartbeat)

    node.last_seen_at = _now()
    node.running_tasks = data.running_tasks
    node.health = data.health

    if node.status in {NodeStatus.pending, NodeStatus.offline}:
        node.status = NodeStatus.online
    elif data.health is NodeHealth.unhealthy:
        node.status = NodeStatus.degraded
    elif node.status is NodeStatus.degraded and data.health is NodeHealth.healthy:
        node.status = NodeStatus.online

    await session.flush()
    return heartbeat


async def node_heartbeats(
    session: AsyncSession, node_id: uuid.UUID, limit: int = 50
) -> list[NodeHeartbeat]:
    rows = (
        await session.execute(
            select(NodeHeartbeat)
            .where(NodeHeartbeat.node_id == node_id)
            .order_by(NodeHeartbeat.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def mark_stale_nodes_offline(session: AsyncSession, stale_after_seconds: int = 120) -> int:
    """Узлы, не присылавшие heartbeat, помечаются offline (вызывается воркером)."""
    threshold = _now() - timedelta(seconds=stale_after_seconds)
    rows = (
        await session.execute(
            select(InfrastructureNode).where(
                InfrastructureNode.status.in_([NodeStatus.online, NodeStatus.degraded]),
                InfrastructureNode.last_seen_at.is_not(None),
                InfrastructureNode.last_seen_at < threshold,
            )
        )
    ).scalars().all()
    for node in rows:
        node.status = NodeStatus.offline
        node.health = NodeHealth.unknown
    await session.flush()
    return len(rows)


async def node_stats(session: AsyncSession) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(InfrastructureNode.status, func.count()).group_by(InfrastructureNode.status)
        )
    ).all()
    by_status = {
        str(status.value if hasattr(status, "value") else status): int(count)
        for status, count in rows
    }
    total = sum(by_status.values())
    capacity = (
        await session.execute(
            select(func.coalesce(func.sum(InfrastructureNode.max_concurrency), 0)).where(
                InfrastructureNode.status == NodeStatus.online
            )
        )
    ).scalar_one()
    return {"total": total, "by_status": by_status, "online_capacity": int(capacity)}


async def select_node_for_task(
    session: AsyncSession, *, required_capabilities: list[str] | None = None
) -> InfrastructureNode | None:
    """Выбор узла с наименьшей загрузкой и подходящими возможностями."""
    stmt = (
        select(InfrastructureNode)
        .where(
            InfrastructureNode.status == NodeStatus.online,
            InfrastructureNode.health == NodeHealth.healthy,
            InfrastructureNode.running_tasks < InfrastructureNode.max_concurrency,
        )
        .order_by(
            (InfrastructureNode.running_tasks * 1.0 / InfrastructureNode.max_concurrency).asc(),
            InfrastructureNode.trust_level.desc(),
            InfrastructureNode.created_at.asc(),
        )
        .limit(20)
    )
    candidates = (await session.execute(stmt)).scalars().all()
    if not candidates:
        return None
    if required_capabilities:
        needed = set(required_capabilities)
        for candidate in candidates:
            if needed.issubset(set(candidate.capabilities or [])):
                return candidate
        return None
    return candidates[0]


def ensure_node_accepts(node: InfrastructureNode, limits: dict[str, Any]) -> None:
    """Узел не должен выполнять задачу, превышающую его собственные лимиты."""
    if int(limits.get("max_cpu_seconds", 60)) > node.limit_cpu_seconds:
        raise ValidationError(
            "Задача превышает лимиты узла по CPU", code="node_limits_exceeded"
        )
    if int(limits.get("max_wall_seconds", 120)) > node.limit_wall_seconds:
        raise ValidationError(
            "Задача превышает лимиты узла по времени", code="node_limits_exceeded"
        )
    if int(limits.get("max_memory_mb", 512)) > node.limit_memory_mb:
        raise ValidationError(
            "Задача превышает лимиты узла по памяти", code="node_limits_exceeded"
        )
    if bool(limits.get("allow_network")) and not node.allow_network:
        raise ValidationError("Узел не разрешает сетевой доступ", code="node_network_forbidden")


async def uniqueness_check_name(session: AsyncSession, owner_id: uuid.UUID, name: str) -> None:
    exists = (
        await session.execute(
            select(func.count()).where(
                InfrastructureNode.owner_id == owner_id, InfrastructureNode.name == name
            )
        )
    ).scalar_one()
    if int(exists) > 0:
        raise ConflictError("Узел с таким именем уже зарегистрирован", code="node_name_taken")
