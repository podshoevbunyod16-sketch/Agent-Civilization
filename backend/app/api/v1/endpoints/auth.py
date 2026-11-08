from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import Ctx, CurrentUser, DbSession, rate_limit_auth
from app.core.config import settings
from app.schemas.agent import AgentListItem
from app.schemas.auth import (
    AccessToken,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyPublic,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserPublic,
)
from app.schemas.common import Message
from app.services import agents as agent_service
from app.services import audit as audit_service
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_public(user) -> UserPublic:
    return UserPublic.model_validate(user)


@router.post(
    "/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_auth)],
    summary="Регистрация разработчика",
)
async def register(
    data: RegisterRequest, session: DbSession, ctx: Ctx, request: Request
) -> TokenPair:
    user = await auth_service.register_user(session, data)
    # Стартовый бюджет участника (внутренние единицы ACU, не деньги).
    # Идемпотентно по ключу signup:<user_id>, повторная регистрация невозможна.
    from app.services import economy as economy_service

    await economy_service.ensure_signup_grant(session, user.id)
    access, refresh, _ = await auth_service.issue_token_pair(
        session, user, user_agent=ctx.user_agent, ip_address=ctx.ip
    )
    await audit_service.record(
        session,
        action="user.register",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        user_agent=ctx.user_agent,
        request_id=ctx.request_id,
    )
    await session.commit()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_seconds,
        user=_user_public(user),
    )


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit_auth)],
    summary="Вход по email и паролю",
)
async def login(data: LoginRequest, session: DbSession, ctx: Ctx) -> TokenPair:
    user, needs_rehash = await auth_service.authenticate(session, data.email, data.password)
    if needs_rehash:
        from app.core.security import hash_password

        user.password_hash = hash_password(data.password)
    access, refresh, _ = await auth_service.issue_token_pair(
        session, user, user_agent=ctx.user_agent, ip_address=ctx.ip
    )
    await audit_service.record(
        session,
        action="user.login",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        user_agent=ctx.user_agent,
        request_id=ctx.request_id,
    )
    await session.commit()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_seconds,
        user=_user_public(user),
    )


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit_auth)],
    summary="Обновление пары токенов (с ротацией refresh)",
)
async def refresh_token(data: RefreshRequest, session: DbSession, ctx: Ctx) -> TokenPair:
    user, access, refresh = await auth_service.rotate_refresh_token(
        session, data.refresh_token, user_agent=ctx.user_agent, ip_address=ctx.ip
    )
    await session.commit()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_seconds,
        user=_user_public(user),
    )


@router.post("/logout", response_model=Message, summary="Выход (отзыв сессии)")
async def logout(data: LogoutRequest, session: DbSession, user: CurrentUser, ctx: Ctx) -> Message:
    if data.all_sessions:
        count = await auth_service.revoke_all_sessions(session, user.id)
        detail = f"Отозвано сессий: {count}"
    elif data.refresh_token:
        await auth_service.revoke_session(session, data.refresh_token)
        detail = "Сессия отозвана"
    else:
        detail = "Нечего отзывать"
    await audit_service.record(
        session,
        action="user.logout",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"all_sessions": data.all_sessions},
    )
    await session.commit()
    return Message(detail=detail)


@router.get("/me", response_model=UserPublic, summary="Текущий пользователь")
async def me(user: CurrentUser) -> UserPublic:
    return _user_public(user)


@router.get(
    "/me/agents",
    response_model=list[AgentListItem],
    summary="Мои агенты (включая черновики)",
)
async def my_agents(session: DbSession, user: CurrentUser) -> list[AgentListItem]:
    items, _ = await agent_service.search_agents(
        session, viewer=user, status="any", owner_id=user.id, limit=100
    )
    return [AgentListItem.model_validate(a) for a in items]


@router.post(
    "/change-password",
    response_model=AccessToken,
    dependencies=[Depends(rate_limit_auth)],
    summary="Смена пароля (отзывает все сессии)",
)
async def change_password(
    data: ChangePasswordRequest, session: DbSession, user: CurrentUser, ctx: Ctx
) -> AccessToken:
    await auth_service.change_password(session, user, data.current_password, data.new_password)
    access, _, _ = await auth_service.issue_token_pair(
        session, user, user_agent=ctx.user_agent, ip_address=ctx.ip
    )
    await audit_service.record(
        session,
        action="user.password_change",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return AccessToken(access_token=access, expires_in=settings.access_token_ttl_seconds)


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_auth)],
    summary="Создать долгоживущий API-ключ (секрет показывается один раз)",
)
async def create_api_key(
    data: ApiKeyCreate,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> ApiKeyCreated:
    from app.services import api_keys as api_key_service

    record, secret = await api_key_service.create_api_key(session, user=user, data=data)
    await audit_service.record(
        session,
        action="api_key.create",
        target_type="api_key",
        target_id=record.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        user_agent=ctx.user_agent,
        request_id=ctx.request_id,
        data={"name": record.name, "key_prefix": record.key_prefix},
    )
    await session.commit()
    return ApiKeyCreated(**ApiKeyPublic.model_validate(record).model_dump(), api_key=secret)


@router.get(
    "/api-keys",
    response_model=list[ApiKeyPublic],
    summary="Мои API-ключи (без секретов)",
)
async def list_api_keys(session: DbSession, user: CurrentUser) -> list[ApiKeyPublic]:
    from app.services import api_keys as api_key_service

    rows = await api_key_service.list_api_keys(session, user_id=user.id)
    return [ApiKeyPublic.model_validate(row) for row in rows]


@router.delete(
    "/api-keys/{key_id}",
    response_model=ApiKeyPublic,
    dependencies=[Depends(rate_limit_auth)],
    summary="Отозвать API-ключ",
)
async def revoke_api_key(
    key_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    ctx: Ctx,
) -> ApiKeyPublic:
    from app.services import api_keys as api_key_service

    record = await api_key_service.revoke_api_key(session, key_id=key_id, user_id=user.id)
    await audit_service.record(
        session,
        action="api_key.revoke",
        target_type="api_key",
        target_id=record.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        user_agent=ctx.user_agent,
        request_id=ctx.request_id,
        data={"name": record.name, "key_prefix": record.key_prefix},
    )
    await session.commit()
    return ApiKeyPublic.model_validate(record)
