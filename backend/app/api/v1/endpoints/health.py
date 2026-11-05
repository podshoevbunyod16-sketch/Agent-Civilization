from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import settings
from app.schemas.common import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok", version=settings.version, environment=settings.environment
    )


@router.get("/health/ready", response_model=HealthResponse, summary="Readiness (БД + Redis)")
async def ready(session: DbSession) -> HealthResponse:
    checks: dict[str, str] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["database"] = f"error: {exc.__class__.__name__}"

    from app.core.redis_client import get_redis

    client = await get_redis()
    if client is None:
        checks["redis"] = "unavailable"
    else:
        try:
            await client.ping()
            checks["redis"] = "ok"
        except Exception:  # pragma: no cover
            checks["redis"] = "error"

    healthy = checks.get("database") == "ok"
    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=settings.version,
        environment=settings.environment,
        checks=checks,
    )
