"""Зависимости FastAPI: текущий пользователь, подписанный узел, пагинация."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AuthenticationError, PermissionDeniedError, RateLimitError
from app.core.security import TokenError, decode_token
from app.db.models.node import InfrastructureNode
from app.db.models.user import User, UserRole
from app.db.session import get_db
from app.services import auth as auth_service
from app.services import nodes as node_service
from app.services import ratelimit

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


@dataclass(slots=True)
class RequestContext:
    request_id: str | None
    ip: str | None
    user_agent: str | None


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Первый адрес в цепочке — клиент (за доверенным прокси)
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


async def get_request_context(request: Request) -> RequestContext:
    return RequestContext(
        request_id=getattr(request.state, "request_id", None),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    )


async def rate_limit_default(request: Request) -> None:
    await ratelimit.check_default(client_ip(request))


async def rate_limit_auth(request: Request) -> None:
    await ratelimit.check_auth(client_ip(request))


async def get_current_user_optional(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: DbSession,
) -> User | None:
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError):
        return None
    user = await auth_service.get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        return None
    return user


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: DbSession,
) -> User:
    if credentials is None:
        raise AuthenticationError("Требуется авторизация", code="missing_token")
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError) as exc:
        raise AuthenticationError("Недействительный токен", code="invalid_token") from exc
    user = await auth_service.get_user_by_id(session, user_id)
    if user is None:
        raise AuthenticationError("Пользователь не найден", code="invalid_token")
    if not user.is_active:
        raise AuthenticationError("Учётная запись отключена", code="account_disabled")
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if user.role is not UserRole.admin:
        raise PermissionDeniedError("Требуются права администратора")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user_optional)]
AdminUser = Annotated[User, Depends(require_admin)]
Ctx = Annotated[RequestContext, Depends(get_request_context)]


# ── Подписанные запросы инфраструктурных узлов ────────────────────────
async def get_signed_node(
    request: Request,
    session: DbSession,
    x_node_key: Annotated[str | None, Header(alias="X-Node-Key")] = None,
    x_node_signature: Annotated[str | None, Header(alias="X-Node-Signature")] = None,
    x_node_timestamp: Annotated[str | None, Header(alias="X-Node-Timestamp")] = None,
    x_node_nonce: Annotated[str | None, Header(alias="X-Node-Nonce")] = None,
) -> InfrastructureNode:
    if not all([x_node_key, x_node_signature, x_node_timestamp, x_node_nonce]):
        raise AuthenticationError(
            "Требуются заголовки подписи узла (X-Node-Key/Signature/Timestamp/Nonce)",
            code="missing_node_signature",
        )
    body = b""
    try:
        body = await request.body()
    except Exception:  # pragma: no cover
        body = b""
    path = request.url.path
    if settings.api_prefix and path.startswith(settings.api_prefix):
        path = path[len(settings.api_prefix):] or "/"
    return await node_service.authenticate_signed_request(
        session,
        node_key=x_node_key,
        signature=x_node_signature,
        timestamp=x_node_timestamp,
        nonce=x_node_nonce,
        method=request.method,
        path=path,
        body=body,
    )


SignedNode = Annotated[InfrastructureNode, Depends(get_signed_node)]


class Pagination:
    def __init__(
        self,
        page: int = Query(1, ge=1, le=100_000),
        page_size: int = Query(
            None, ge=1, le=settings.registry_page_size_max, alias="page_size"
        ),
    ) -> None:
        self.page = page
        self.page_size = page_size or settings.registry_page_size_default

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


Paging = Annotated[Pagination, Depends(Pagination)]

__all__ = [
    "AdminUser",
    "Ctx",
    "CurrentUser",
    "DbSession",
    "OptionalUser",
    "Paging",
    "Pagination",
    "RateLimitError",
    "RequestContext",
    "SignedNode",
    "client_ip",
    "rate_limit_auth",
    "rate_limit_default",
]
