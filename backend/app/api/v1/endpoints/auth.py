from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import (
    Ctx,
    CurrentUser,
    DbSession,
    rate_limit_auth,
    rate_limit_critical,
)
from app.core import secrets as secrets_module
from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.metrics import metrics
from app.schemas.agent import AgentListItem
from app.schemas.auth import (
    AccessToken,
    AccountSecurity,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyPublic,
    ChangePasswordRequest,
    EmailRequest,
    LoginRequestV2,
    LogoutRequest,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    SessionPublic,
    TokenConfirmRequest,
    TokenIssued,
    TokenPair,
    TotpDisableRequest,
    TotpEnableRequest,
    TotpEnableResponse,
    TotpSetupResponse,
    UserPublic,
)
from app.schemas.common import Message
from app.services import agents as agent_service
from app.services import audit as audit_service
from app.services import auth as auth_service
from app.services import mailer

router = APIRouter(prefix="/auth", tags=["auth"])

# Незавершённые настройки 2FA (секрет ещё не подтверждён кодом).
# Хранится в процессе: потеря записи при рестарте означает лишь
# необходимость повторить `/2fa/setup` — устройство ещё не подключено.
PENDING_TOTP_TTL_SECONDS = 15 * 60
pending: dict[str, str] = {}
_pending_created: dict[str, float] = {}


def _pending_ttl(key: str) -> None:
    now = time.time()
    _pending_created[key] = now
    for stale in [
        item for item, ts in _pending_created.items() if now - ts > PENDING_TOTP_TTL_SECONDS
    ]:
        _pending_created.pop(stale, None)
        pending.pop(stale, None)


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
async def login(data: LoginRequestV2, session: DbSession, ctx: Ctx) -> TokenPair:
    user, needs_rehash = await auth_service.authenticate(
        session, data.email, data.password, ip_address=ctx.ip, totp_code=data.totp_code
    )
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
async def logout(
    data: LogoutRequest, session: DbSession, user: CurrentUser, ctx: Ctx
) -> Message:
    if data.all_sessions:
        count = await auth_service.revoke_all_sessions(
            session, user.id, reason="logout_all"
        )
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
    "/me/security",
    response_model=AccountSecurity,
    summary="Состояние безопасности аккаунта",
)
async def my_security(session: DbSession, user: CurrentUser) -> AccountSecurity:
    from sqlalchemy import func, select

    from app.db.models.user import ApiKey, RefreshTokenSession

    sessions_count = (
        await session.execute(
            select(func.count())
            .select_from(RefreshTokenSession)
            .where(
                RefreshTokenSession.user_id == user.id,
                RefreshTokenSession.revoked_at.is_(None),
            )
        )
    ).scalar_one()
    keys_count = (
        await session.execute(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        )
    ).scalar_one()
    warnings: list[str] = []
    if not user.email_verified:
        warnings.append("Адрес электронной почты не подтверждён")
    if not user.totp_secret:
        warnings.append("Двухфакторная аутентификация не включена")
    elif len(user.recovery_codes or []) <= 2:
        warnings.append("Осталось мало кодов восстановления: выпустите новые")
    if user.role.value in settings.mfa_roles and not user.totp_secret:
        warnings.append("Для вашей роли 2FA обязательна: подключите устройство")
    return AccountSecurity(
        email_verified=user.email_verified,
        mfa_enabled=bool(user.totp_secret),
        mfa_required=auth_service.mfa_required_for(user),
        active_sessions=int(sessions_count),
        active_api_keys=int(keys_count),
        warnings=warnings,
    )


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


# ══════════════════════════════════════════════════════════════════════
# Этап 10, B: подтверждение email, восстановление доступа, 2FA, сессии
# ══════════════════════════════════════════════════════════════════════
def _dev_token(token: str) -> str | None:
    """Токен возвращается в ответе только в dev-режиме доставки писем.

    Это не «отладочная дыра»: в production конфигурация запрещает
    `EMAIL_DELIVERY=console` при включённом подтверждении адреса, а поле
    `dev_token` остаётся `None`.
    """
    return token if settings.email_delivery == "console" else None


@router.post(
    "/email/request-verification",
    response_model=TokenIssued,
    dependencies=[Depends(rate_limit_auth)],
    summary="Запросить письмо для подтверждения адреса",
)
async def request_email_verification(
    session: DbSession, user: CurrentUser, ctx: Ctx
) -> TokenIssued:
    if user.email_verified:
        return TokenIssued(message="Адрес уже подтверждён")
    token = await auth_service.issue_one_time_token(
        session,
        user,
        auth_service.PURPOSE_EMAIL_VERIFY,
        ttl_seconds=settings.email_verification_ttl_seconds,
        ip_address=ctx.ip,
    )
    subject, body = mailer.verification_message(display_name=user.display_name, token=token)
    delivery = mailer.get_mailer().send(to=user.email, subject=subject, body=body)
    await audit_service.record(
        session,
        action="user.email_verification_requested",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"delivery": delivery},
    )
    await session.commit()
    return TokenIssued(delivery=delivery, dev_token=_dev_token(token))


@router.post(
    "/email/verify",
    response_model=UserPublic,
    dependencies=[Depends(rate_limit_auth)],
    summary="Подтвердить адрес по токену",
)
async def verify_email(data: TokenConfirmRequest, session: DbSession, ctx: Ctx) -> UserPublic:
    user = await auth_service.consume_one_time_token(
        session, data.token, auth_service.PURPOSE_EMAIL_VERIFY
    )
    user.email_verified = True
    await audit_service.record(
        session,
        action="user.email_verified",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return _user_public(user)


@router.post(
    "/password/forgot",
    response_model=TokenIssued,
    dependencies=[Depends(rate_limit_auth)],
    summary="Запросить восстановление доступа",
)
async def forgot_password(data: EmailRequest, session: DbSession, ctx: Ctx) -> TokenIssued:
    """Ответ одинаков для существующего и несуществующего адреса."""
    user = await auth_service.get_user_by_email(session, data.email)
    if user is None or not user.is_active:
        # Выравниваем время ответа: работа с БД и «отправка» в console-режиме
        # для несуществующего адреса не выполняются, но ответ неотличим.
        metrics.incr("auth_password_reset_requested", outcome="unknown_email")
        return TokenIssued()
    token = await auth_service.issue_one_time_token(
        session,
        user,
        auth_service.PURPOSE_PASSWORD_RESET,
        ttl_seconds=settings.password_reset_ttl_seconds,
        ip_address=ctx.ip,
    )
    subject, body = mailer.password_reset_message(display_name=user.display_name, token=token)
    delivery = mailer.get_mailer().send(to=user.email, subject=subject, body=body)
    await audit_service.record(
        session,
        action="user.password_reset_requested",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"delivery": delivery},
    )
    await session.commit()
    metrics.incr("auth_password_reset_requested", outcome="ok")
    return TokenIssued(delivery=delivery, dev_token=_dev_token(token))


@router.post(
    "/password/reset",
    response_model=Message,
    dependencies=[Depends(rate_limit_auth)],
    summary="Сбросить пароль по одноразовому токену",
)
async def reset_password(
    data: PasswordResetRequest, session: DbSession, ctx: Ctx
) -> Message:
    user = await auth_service.consume_one_time_token(
        session, data.token, auth_service.PURPOSE_PASSWORD_RESET
    )
    from app.core.security import hash_password_async

    user.password_hash = await hash_password_async(data.new_password)
    # Все сессии отзываются: если пароль меняют из-за компрометации,
    # активные сессии атакующего должны умереть вместе со старым паролем.
    revoked = await auth_service.revoke_all_sessions(
        session, user.id, reason="password_reset"
    )
    await audit_service.record(
        session,
        action="user.password_reset",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"sessions_revoked": revoked},
    )
    await session.commit()
    return Message(detail=f"Пароль изменён, отозвано сессий: {revoked}")


# ── 2FA (TOTP) ─────────────────────────────────────────────────────────
@router.post(
    "/2fa/setup",
    response_model=TotpSetupResponse,
    dependencies=[Depends(rate_limit_critical)],
    summary="Начать подключение 2FA (секрет + otpauth-URI)",
)
async def setup_2fa(session: DbSession, user: CurrentUser) -> TotpSetupResponse:
    """Секрет возвращается один раз и ещё не активен.

    Он сохраняется в БД только после подтверждения кодом (`/2fa/enable`),
    поэтому случайно включить 2FA с неподключённым устройством нельзя.
    """
    encrypted, uri = auth_service.generate_totp_secret(user)
    pending[key := f"totp_pending:{user.id}"] = encrypted
    _pending_ttl(key)
    return TotpSetupResponse(
        secret=secrets_module.decrypt_secret(encrypted),
        otpauth_uri=uri,
        issuer=settings.totp_issuer,
    )


@router.post(
    "/2fa/enable",
    response_model=TotpEnableResponse,
    dependencies=[Depends(rate_limit_critical)],
    summary="Подтвердить код и включить 2FA",
)
async def enable_2fa(
    data: TotpEnableRequest, session: DbSession, user: CurrentUser, ctx: Ctx
) -> TotpEnableResponse:
    key = f"totp_pending:{user.id}"
    encrypted = pending.get(key)
    if not encrypted:
        raise ValidationError(
            "Сначала запросите настройку 2FA (/auth/2fa/setup)",
            code="mfa_setup_not_started",
        )
    probe = type("Probe", (), {"totp_secret": encrypted})()
    if not auth_service.verify_totp(probe, data.code):  # type: ignore[arg-type]
        metrics.incr("auth_mfa_setup_failed")
        raise ValidationError("Код неверен: проверьте время на устройстве", code="invalid_mfa_code")
    codes = await auth_service.enable_totp(session, user, encrypted)
    pending.pop(key, None)
    await audit_service.record(
        session,
        action="user.mfa_enabled",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return TotpEnableResponse(recovery_codes=codes)


@router.post(
    "/2fa/disable",
    response_model=Message,
    dependencies=[Depends(rate_limit_critical)],
    summary="Отключить 2FA (нужен текущий код)",
)
async def disable_2fa(
    data: TotpDisableRequest, session: DbSession, user: CurrentUser, ctx: Ctx
) -> Message:
    if not user.totp_secret:
        return Message(detail="2FA не была включена")
    if not auth_service.verify_totp(user, data.code):
        raise ValidationError("Неверный код 2FA", code="invalid_mfa_code")
    await auth_service.disable_totp(session, user, actor_is_admin=user.is_admin)
    await audit_service.record(
        session,
        action="user.mfa_disabled",
        target_type="user",
        target_id=user.id,
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return Message(detail="2FA отключена")


# ── Сессии и API-ключи ─────────────────────────────────────────────────
@router.get(
    "/sessions",
    response_model=list[SessionPublic],
    summary="Активные сессии (устройства)",
)
async def list_sessions(
    request: Request, session: DbSession, user: CurrentUser
) -> list[SessionPublic]:
    current_sid = getattr(request.state, "session_id", None)
    rows = await auth_service.list_sessions(session, user, current_sid=current_sid)
    await session.commit()
    return [
        SessionPublic(
            id=row.id,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            is_current=bool(getattr(row, "is_current", False)),
        )
        for row in rows
    ]


@router.delete(
    "/sessions/{session_row_id}",
    response_model=Message,
    summary="Отозвать сессию по идентификатору",
)
async def revoke_user_session(
    session_row_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> Message:
    revoked = await auth_service.revoke_session_by_id(session, user, session_row_id)
    await audit_service.record(
        session,
        action="user.session_revoked",
        target_type="session",
        target_id=str(session_row_id),
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"revoked": revoked},
    )
    await session.commit()
    if not revoked:
        raise NotFoundError("Сессия не найдена или уже отозвана", code="session_not_found")
    return Message(detail="Сессия отозвана")


@router.get("/api-keys", response_model=list[ApiKeyPublic], summary="Мои API-ключи")
async def list_api_keys(session: DbSession, user: CurrentUser) -> list[ApiKeyPublic]:
    from sqlalchemy import select

    from app.db.models.user import ApiKey

    rows = (
        await session.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user.id)
            .order_by(ApiKey.created_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return [ApiKeyPublic.model_validate(row) for row in rows]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_critical)],
    summary="Создать API-ключ",
)
async def create_api_key(
    data: ApiKeyCreate, session: DbSession, user: CurrentUser, ctx: Ctx
) -> ApiKeyCreated:
    record, raw = await auth_service.issue_api_key(
        session,
        user,
        name=data.name,
        scopes=data.scopes,
        expires_in_days=data.expires_in_days,
    )
    await audit_service.record(
        session,
        action="user.api_key_created",
        target_type="api_key",
        target_id=str(record.id),
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
        data={"name": record.name, "scopes": record.scopes},
    )
    await session.commit()
    return ApiKeyCreated(key=raw, api_key=ApiKeyPublic.model_validate(record))


@router.delete(
    "/api-keys/{key_id}",
    response_model=Message,
    dependencies=[Depends(rate_limit_critical)],
    summary="Отозвать API-ключ",
)
async def revoke_api_key(
    key_id: uuid.UUID, session: DbSession, user: CurrentUser, ctx: Ctx
) -> Message:
    from sqlalchemy import select

    from app.db.models.user import ApiKey

    record = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("Ключ не найден", code="api_key_not_found")
    if record.revoked_at is None:
        from datetime import UTC, datetime

        record.revoked_at = datetime.now(UTC)
    await audit_service.record(
        session,
        action="user.api_key_revoked",
        target_type="api_key",
        target_id=str(record.id),
        actor_id=user.id,
        ip_address=ctx.ip,
        request_id=ctx.request_id,
    )
    await session.commit()
    return Message(detail="Ключ отозван")
