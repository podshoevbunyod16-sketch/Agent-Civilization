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


def _ip_in_trusted_networks(ip: str) -> bool:
    import ipaddress

    networks = settings.trusted_proxy_networks
    if not networks:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in networks:
        try:
            if address in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """Адрес клиента с учётом только доверенных прокси (этап 10, A #4).

    Раньше `X-Forwarded-For` принимался от любого источника: клиент ставил
    произвольный IP и обходил лимиты (перебор паролей с «нового» адреса на
    каждый запрос). Теперь заголовок читается, только если непосредственный
    пир соединения входит в `TRUSTED_PROXIES`; из цепочки берётся последний
    адрес, добавленный уже недоверенной частью, — это и есть клиент.
    """
    peer = (request.client.host if request.client else "") or ""
    if not peer or not _ip_in_trusted_networks(peer):
        return (peer or "unknown")[:64]
    forwarded = request.headers.get("x-forwarded-for") or ""
    chain = [item.strip() for item in forwarded.split(",") if item.strip()]
    if not chain:
        return peer[:64]
    # Идём справа налево: пропускаем доверенные прокси, первый недоверенный —
    # клиент. Значения левее подконтрольны клиенту и доверять им нельзя.
    for candidate in reversed(chain):
        if not _ip_in_trusted_networks(candidate):
            return candidate[:64]
    return chain[0][:64]


async def get_request_context(request: Request) -> RequestContext:
    return RequestContext(
        request_id=getattr(request.state, "request_id", None),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    )


def _limit_identifier(request: Request) -> str:
    """Ключ лимита: пользователь, если он известен, иначе IP.

    Глобальный middleware ставит `request.state.user_id` из access-токена до
    вызова эндпоинта, поэтому аутентифицированные запросы ограничиваются по
    аккаунту (сменой IP лимит не обойти), анонимные — по IP.
    """
    user_id = getattr(request.state, "user_id", None)
    return f"u:{user_id}" if user_id else f"ip:{client_ip(request)}"


async def rate_limit_default(request: Request) -> None:
    await ratelimit.check_default(_limit_identifier(request))


async def rate_limit_auth(request: Request) -> None:
    await ratelimit.check_auth(f"ip:{client_ip(request)}")


async def rate_limit_write(request: Request) -> None:
    await ratelimit.check_write(_limit_identifier(request))


async def rate_limit_critical(request: Request) -> None:
    await ratelimit.check_critical(_limit_identifier(request))


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
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: DbSession,
) -> User:
    if credentials is None:
        raise AuthenticationError("Требуется авторизация", code="missing_token")
    raw = credentials.credentials
    # API-ключ (`ac_...`) — альтернатива JWT для автоматизации. Проверяется
    # по хешу в БД; сессии у него нет, отзывается отдельно (этап 10, B).
    if raw.startswith(auth_service.API_KEY_PREFIX):
        api_user = await auth_service.resolve_api_key(session, raw)
        if api_user is None or not api_user.is_active:
            raise AuthenticationError("Недействительный API-ключ", code="invalid_api_key")
        request.state.user = api_user
        request.state.user_id = api_user.id
        return api_user
    try:
        payload = decode_token(raw, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError) as exc:
        raise AuthenticationError("Недействительный токен", code="invalid_token") from exc
    # Привязка access-токена к сессии: отозванная сессия (logout, reuse-
    # детекция, админ-отзыв) перестаёт работать немедленно. До этапа 10
    # проверялся только подпись и активность пользователя — токен жил до
    # истечения даже после явного выхода.
    user, session_record = await auth_service.resolve_access_token(session, payload, user_id)
    if session_record is not None:
        await auth_service.touch_session(session, session_record)
    request.state.user = user
    request.state.user_id = user.id
    if session_record is not None:
        request.state.session_id = session_record.session_id
    return user


async def require_service_token(
    x_service_token: Annotated[str | None, Header(alias="X-Service-Token")] = None,
) -> None:
    """Защита внутреннего API доставки сообщений.

    Токен сравнивается в постоянное время. Если токен не настроен,
    внутренний API недоступен (fail-closed) — случайно открытым он не станет.
    """
    from app.core.security import constant_time_compare

    expected = settings.internal_service_token
    if not expected:
        raise PermissionDeniedError(
            "Внутренний API отключён: не задан INTERNAL_SERVICE_TOKEN",
            code="internal_api_disabled",
        )
    if not x_service_token or not constant_time_compare(x_service_token, expected):
        raise PermissionDeniedError("Неверный сервисный токен", code="invalid_service_token")


async def require_verified_email(user: CurrentUser) -> User:
    """Подтверждение адреса для операций с деньгами и секретами.

    Действует только при `EMAIL_VERIFICATION_REQUIRED=true` (в этой
    конфигурации регистрация выдаёт неподтверждённый аккаунт). Проверка
    стоит на операциях, где анонимный/подставной адрес особенно опасен:
    вывод средств, регистрация узла, MCP-сервер, API-ключи.
    """
    if settings.email_verification_required and not user.email_verified:
        raise PermissionDeniedError(
            "Подтвердите адрес электронной почты, чтобы выполнить это действие",
            code="email_verification_required",
        )
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
VerifiedUser = Annotated[User, Depends(require_verified_email)]
ServiceAuth = Annotated[None, Depends(require_service_token)]
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
    "ServiceAuth",
    "RequestContext",
    "SignedNode",
    "client_ip",
    "rate_limit_auth",
    "rate_limit_default",
]
