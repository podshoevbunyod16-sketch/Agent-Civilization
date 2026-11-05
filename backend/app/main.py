"""Точка сборки FastAPI-приложения."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.metrics import metrics
from app.core.redis_client import close_redis
from app.db.session import dispose_engine

logger = logging.getLogger(__name__)

DESCRIPTION = """
**Agent Civilization API** — реестр AI-агентов, инфраструктура исполнения и очередь заданий.

### Возможности
* Регистрация и авторизация разработчиков (JWT, ротация refresh-токенов)
* Agent Registry: создание, версии, возможности, публикация, приостановка
* Capabilities Registry и поиск агентов с фильтрами
* Execution Gateway: учёт инфраструктурных узлов, аренда задач, retry
* Журнал аудита для всех критических операций

### Аутентификация
`Authorization: Bearer <access_token>`.

Инфраструктурные узлы используют HMAC-подпись запросов:
`X-Node-Key`, `X-Node-Signature`, `X-Node-Timestamp`, `X-Node-Nonce`.

### Контракт не зависит от языка
Спецификация OpenAPI генерируется автоматически: клиентов можно
писать на Python, TypeScript, Go, Rust, Java — система не привязана
к конкретному стеку или AI-провайдеру.
"""


_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LONG_ID_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_NUMBER_RE = re.compile(r"(?<=/)\d+(?=/|$)")


def _route_template(request: Request) -> str:
    """Шаблон маршрута без идентификаторов.

    Кардинальность меток в Prometheus обязана быть ограниченной: иначе каждый
    UUID задания создаёт новую серию. `scope["route"]` здесь недоступен —
    маршрутизация происходит внутри стека middleware, — поэтому путь
    нормализуется сам: UUID, длинные hex-идентификаторы и числа заменяются
    плейсхолдерами. Это даёт стабильную метку вида
    `/api/v1/agents/{id}/versions`.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    normalized = _UUID_RE.sub("{id}", request.url.path)
    normalized = _LONG_ID_RE.sub("{id}", normalized)
    normalized = _NUMBER_RE.sub("{n}", normalized)
    return normalized[:120]


class MetricsMiddleware(BaseHTTPMiddleware):
    """Считает запросы и длительность; сам `/metrics` не учитывает."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/metrics", "/health", "/health/ready"}:
            return await call_next(request)
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        metrics.observe_request(
            method=request.method,
            route=_route_template(request),
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id[:64]
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Response-Time-Ms"] = str(duration_ms)
        if request.url.path.startswith(settings.api_prefix):
            logger.info(
                "http_request",
                extra={
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings.validate_for_runtime()
    logger.info(
        "app_started",
        extra={"env": settings.environment, "version": settings.version},
    )

    # Фоновый слушатель Redis Pub/Sub для репликации событий между подами
    listener_task = None
    try:
        from app.core.events import event_bus
        from app.core.redis_client import get_redis

        client = await get_redis()
        if client is not None:
            import asyncio

            async def _listen() -> None:
                from app.core.events import CHANNEL

                pubsub = client.pubsub()
                await pubsub.subscribe(CHANNEL)
                try:
                    async for message in pubsub.listen():
                        if message.get("type") == "message":
                            await event_bus.receive_remote(message.get("data") or "")
                except Exception:  # pragma: no cover
                    pass
                finally:
                    try:
                        await pubsub.unsubscribe(CHANNEL)
                        await pubsub.aclose()
                    except Exception:  # pragma: no cover
                        pass

            listener_task = asyncio.create_task(_listen())
        app.state.redis_listener = listener_task
    except Exception as exc:  # pragma: no cover
        logger.warning("redis_listener_failed", extra={"error": str(exc)})

    yield

    if listener_task is not None:
        listener_task.cancel()
    await close_redis()
    await dispose_engine()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.project_name,
        version=settings.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Agent Civilization", "url": "https://github.com/"},
        license_info={"name": "MIT"},
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-Ms"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    # Метрики регистрируются последними, чтобы измерять полный путь запроса
    # (включая gzip и контекст), но не учитывать сам scrape.
    app.add_middleware(MetricsMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/metrics", include_in_schema=False, response_class=Response)
    async def root_metrics(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_service_token: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Prometheus scrape в корне: стандартный путь, ожидаемый сборщиком."""
        from app.api.v1.endpoints.ops import render_metrics
        from app.db.session import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            del request
            return await render_metrics(session, authorization, x_service_token)

    @app.get("/", tags=["root"], summary="Информация о сервисе")
    async def root() -> dict:
        return {
            "name": settings.project_name,
            "version": settings.version,
            "environment": settings.environment,
            "api": settings.api_prefix,
            "docs": "/docs",
            "health": f"{settings.api_prefix}/health",
        }

    return app


app = create_app()
