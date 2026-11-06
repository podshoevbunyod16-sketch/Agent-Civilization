"""Регистрация, вход, сессии, 2FA, одноразовые токены (этапы B и C).

Ключевые решения этапа 10:

* **Сессия — сущность сервера.** `refresh_token_sessions.session_id` попадает
  в access-токен (`sid`). Поэтому выход, отзыв админом и reuse-детекция
  действуют немедленно, а не «когда истечёт access-токен».
* **Перебор паролей.** Кроме IP-лимита (ratelimit) есть счётчик неудач по
  аккаунту и блокировка на `login_lockout_seconds`. Ответ на несуществующий
  email неотличим от неверного пароля (одинаковый код и время).
* **2FA (TOTP).** Секрет шифруется AES-GCM; коды восстановления — хеши,
  одноразовые. 2FA обязательна только для ролей из `MFA_ENFORCED_ROLES`
  (по умолчанию админ) и только при уже зарегистрированном устройстве:
  иначе включение требования заблокировало бы вход.
* **Одноразовые токены** (подтверждение email, сброс пароля): 32 байта
  энтропии, в БД только хеш, TTL, single-use.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets as secrets_module
from app.core.config import settings
from app.core.errors import AuthenticationError, ConflictError, ValidationError
from app.core.metrics import metrics
from app.core.security import (
    TokenError,
    create_token,
    decode_token,
    hash_password,
    hash_password_async,
    password_needs_rehash,
    verify_password_async,
)
from app.db.models.user import (
    ApiKey,
    LoginAttempt,
    OneTimeToken,
    RefreshTokenSession,
    User,
    UserRole,
)
from app.schemas.auth import RegisterRequest

# Фиктивный хеш: выравнивает время ответа для несуществующих email
_DUMMY_HASH = hash_password("dummy-password-for-timing-equalization-1")

PURPOSE_EMAIL_VERIFY = "email_verify"
PURPOSE_PASSWORD_RESET = "password_reset"
MAX_RECOVERY_CODES = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_one_time(token: str) -> str:
    """Хеш одноразового токена с «перчинкой»: дампа БД недостаточно."""
    return hmac.new(
        settings.secret_key.encode(), token.encode(), hashlib.sha256
    ).hexdigest()


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == email.lower().strip()))
    ).scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def register_user(session: AsyncSession, data: RegisterRequest) -> User:
    email = data.email.lower().strip()
    existing = await get_user_by_email(session, email)
    if existing is not None:
        raise ConflictError("Пользователь с таким email уже существует", code="email_taken")

    user = User(
        email=email,
        password_hash=await hash_password_async(data.password),
        display_name=data.display_name,
        role=UserRole.developer,
        # Аккаунт создаётся неподтверждённым всегда: факт подтверждения — это
        # отдельное событие с токеном, а не следствие настройки. Требование
        # подтверждения (`EMAIL_VERIFICATION_REQUIRED`) лишь расширяет список
        # операций, доступных без него (VerifiedUser).
        email_verified=False,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "Пользователь с таким email уже существует", code="email_taken"
        ) from exc
    return user


# ── Защита от перебора ─────────────────────────────────────────────────
async def _recent_failures(session: AsyncSession, identifier: str, kind: str) -> int:
    since = _now() - timedelta(seconds=settings.login_lockout_seconds)
    rows = await session.execute(
        select(LoginAttempt)
        .where(
            LoginAttempt.identifier == identifier,
            LoginAttempt.kind == kind,
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at >= since,
        )
        .limit(settings.login_max_attempts + 1)
    )
    return len(rows.scalars().all())


async def _record_attempt(
    session: AsyncSession, *, identifier: str, kind: str, success: bool, ip: str | None
) -> None:
    session.add(
        LoginAttempt(
            identifier=identifier, kind=kind, success=success, ip_address=(ip or None)
        )
    )
    # Чистим историю того же ключа: таблица не должна расти бесконечно.
    cutoff = _now() - timedelta(seconds=settings.login_lockout_seconds * 4)
    await session.execute(
        LoginAttempt.__table__.delete().where(LoginAttempt.created_at < cutoff)
    )


async def assert_not_locked(session: AsyncSession, identifier: str, kind: str = "email") -> None:
    failures = await _recent_failures(session, identifier, kind)
    if failures >= settings.login_max_attempts:
        metrics.incr("auth_login_locked")
        raise AuthenticationError(
            "Слишком много неудачных попыток входа. Попробуйте позже",
            code="account_locked",
            details={"retry_after_seconds": settings.login_lockout_seconds},
        )


# ── Вход ───────────────────────────────────────────────────────────────
async def authenticate(
    session: AsyncSession,
    email: str,
    password: str,
    *,
    ip_address: str | None = None,
    totp_code: str | None = None,
) -> tuple[User, bool]:
    """Возвращает (user, needs_rehash). Не раскрывает, что именно неверно.

    Порядок: блокировка по аккаунту → пароль → 2FA → сброс счётчика.
    Неудача на любом шаге увеличивает счётчик одинаково, поэтому по ответу
    нельзя отличить «нет такого email» от «неверный пароль» и «неверный код».
    """
    normalized = email.lower().strip()
    await assert_not_locked(session, normalized)
    user = await get_user_by_email(session, normalized)
    if user is None:
        await verify_password_async(password, _DUMMY_HASH)  # constant-ish time
        await _record_attempt(
            session, identifier=normalized, kind="email", success=False, ip=ip_address
        )
        await session.commit()
        metrics.incr("auth_login_failed")
        raise AuthenticationError("Неверный email или пароль", code="invalid_credentials")
    if not await verify_password_async(password, user.password_hash):
        await _record_attempt(
            session, identifier=normalized, kind="email", success=False, ip=ip_address
        )
        user.failed_login_count = (user.failed_login_count or 0) + 1
        await session.commit()
        metrics.incr("auth_login_failed")
        raise AuthenticationError("Неверный email или пароль", code="invalid_credentials")
    if not user.is_active:
        raise AuthenticationError("Учётная запись отключена", code="account_disabled")
    if user.locked_until is not None and user.locked_until > _now():
        raise AuthenticationError(
            "Учётная запись временно заблокирована", code="account_locked"
        )
    if user.totp_secret:
        if not totp_code:
            # Отдельный код: клиент должен запросить второй фактор, а не
            # показывать «неверный пароль» — иначе пользователь в тупике.
            metrics.incr("auth_mfa_required")
            raise AuthenticationError(
                "Требуется код двухфакторной аутентификации", code="mfa_required"
            )
        if not verify_totp(user, totp_code):
            if not await consume_recovery_code(session, user, totp_code):
                await _record_attempt(
                    session, identifier=normalized, kind="email", success=False, ip=ip_address
                )
                await session.commit()
                metrics.incr("auth_mfa_failed")
                raise AuthenticationError("Неверный код 2FA", code="invalid_mfa_code")
    await _record_attempt(
        session, identifier=normalized, kind="email", success=True, ip=ip_address
    )
    user.failed_login_count = 0
    user.locked_until = None
    return user, password_needs_rehash(user.password_hash)


def mfa_required_for(user: User) -> bool:
    """Нужен ли второ�� фактор при входе.

    Требование по роли действует только для тех, у кого устройство уже
    подключено; для админа без TOTP — отдельное предупреждение в `/auth/me`
    и в `/ops/health`, но не блокировка (иначе админ потеряет доступ).
    """
    if user.totp_secret:
        return True
    return user.role.value in settings.mfa_roles


# ── Сессии и выдача токенов ────────────────────────────────────────────
async def issue_token_pair(
    session: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, str, datetime]:
    session_id = uuid.uuid4().hex
    refresh, refresh_expires, refresh_jti = create_token(
        str(user.id), "refresh", extra_claims={"sid": session_id}
    )
    access, _, _ = create_token(
        str(user.id),
        "access",
        extra_claims={"role": user.role.value, "sid": session_id},
    )

    session.add(
        RefreshTokenSession(
            user_id=user.id,
            jti=refresh_jti,
            session_id=session_id,
            expires_at=refresh_expires,
            user_agent=(user_agent or "")[:255] or None,
            ip_address=(ip_address or "")[:64] or None,
            last_used_at=_now(),
        )
    )
    user.last_login_at = _now()
    await session.flush()
    return access, refresh, refresh_expires


async def resolve_access_token(
    session: AsyncSession, payload: dict, user_id: uuid.UUID
) -> tuple[User, RefreshTokenSession | None]:
    """Проверяет access-токен против серверной сессии.

    Токены, выданные до этапа 10, не имеют `sid`: они принимаются до
    истечения (обратная совместимость), но после первой ротации клиент
    получит токен нового формата с привязкой к сессии.
    """
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise AuthenticationError("Пользователь не найден", code="invalid_token")
    if not user.is_active:
        raise AuthenticationError("Учётная запись отключена", code="account_disabled")
    sid = payload.get("sid")
    if not sid:
        return user, None
    stored = (
        await session.execute(
            select(RefreshTokenSession).where(RefreshTokenSession.session_id == sid)
        )
    ).scalar_one_or_none()
    if stored is None or stored.user_id != user.id:
        raise AuthenticationError("Сессия не найдена", code="session_not_found")
    if stored.revoked_at is not None:
        raise AuthenticationError("Сессия отозвана", code="session_revoked")
    if stored.expires_at <= _now():
        raise AuthenticationError("Сессия истекла", code="session_expired")
    return user, stored


async def touch_session(session: AsyncSession, record: RefreshTokenSession) -> None:
    """Отметка активности сессии (раз в минуту, чтобы не писать на каждый запрос)."""
    now = _now()
    if record.last_used_at is None or (now - record.last_used_at) > timedelta(minutes=1):
        record.last_used_at = now


async def rotate_refresh_token(
    session: AsyncSession,
    refresh_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str, str]:
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise AuthenticationError("Недействительный refresh-токен", code="invalid_token") from exc

    jti = payload["jti"]
    stored = (
        await session.execute(
            select(RefreshTokenSession).where(RefreshTokenSession.jti == jti).with_for_update()
        )
    ).scalar_one_or_none()

    if stored is None:
        raise AuthenticationError("Сессия не найдена", code="session_not_found")

    if stored.revoked_at is not None:
        # Повторное использование отозванного токена → компрометация:
        # отзываем все сессии пользователя.
        await revoke_all_sessions(session, stored.user_id, reason="token_reuse")
        # Отзыв сессий фиксируется отдельной транзакцией: откат из-за
        # ошибки аутентификации не должен отменять защитную меру.
        await session.commit()
        metrics.incr("auth_refresh_reuse_detected")
        raise AuthenticationError(
            "Refresh-токен уже использован, все сессии отозваны", code="token_reuse_detected"
        )

    if stored.expires_at <= _now():
        raise AuthenticationError("Сессия истекла", code="session_expired")

    user = await get_user_by_id(session, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthenticationError("Пользователь недоступен", code="account_disabled")

    stored.revoked_at = _now()
    stored.revoked_reason = "rotated"
    # Переиспользуем session_id: клиент продолжает одну и ту же сессию,
    # список устройств не «плодится» при каждом обновлении токена.
    session_id = stored.session_id or uuid.uuid4().hex
    refresh, refresh_expires, refresh_jti = create_token(
        str(user.id), "refresh", extra_claims={"sid": session_id}
    )
    access, _, _ = create_token(
        str(user.id),
        "access",
        extra_claims={"role": user.role.value, "sid": session_id},
    )
    session.add(
        RefreshTokenSession(
            user_id=user.id,
            jti=refresh_jti,
            session_id=session_id,
            expires_at=refresh_expires,
            user_agent=(user_agent or "")[:255] or None,
            ip_address=(ip_address or "")[:64] or None,
            last_used_at=_now(),
        )
    )
    stored.replaced_by_jti = refresh_jti
    await session.flush()
    return user, access, refresh


async def revoke_session(
    session: AsyncSession, refresh_token: str, *, reason: str = "logout"
) -> bool:
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except TokenError:
        return False
    stored = (
        await session.execute(
            select(RefreshTokenSession).where(RefreshTokenSession.jti == payload["jti"])
        )
    ).scalar_one_or_none()
    if stored is None or stored.revoked_at is not None:
        return False
    stored.revoked_at = _now()
    stored.revoked_reason = reason
    await session.flush()
    return True


async def revoke_all_sessions(
    session: AsyncSession, user_id: uuid.UUID, *, reason: str = "revoked"
) -> int:
    result = await session.execute(
        update(RefreshTokenSession)
        .where(
            RefreshTokenSession.user_id == user_id,
            RefreshTokenSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now(), revoked_reason=reason)
    )
    await session.flush()
    return int(result.rowcount or 0)


async def list_sessions(
    session: AsyncSession, user: User, *, current_sid: str | None = None
) -> list[RefreshTokenSession]:
    rows = await session.execute(
        select(RefreshTokenSession)
        .where(
            RefreshTokenSession.user_id == user.id,
            RefreshTokenSession.revoked_at.is_(None),
            RefreshTokenSession.expires_at > _now(),
        )
        .order_by(RefreshTokenSession.created_at.desc())
        .limit(100)
    )
    sessions_list = list(rows.scalars().all())
    for item in sessions_list:
        item.is_current = item.session_id == current_sid  # type: ignore[attr-defined]
    return sessions_list


async def revoke_session_by_id(
    session: AsyncSession, user: User, session_row_id: uuid.UUID
) -> bool:
    """Отзыв конкретной сессии пользователя (только своей: user_id в условии)."""
    stored = (
        await session.execute(
            select(RefreshTokenSession).where(
                RefreshTokenSession.id == session_row_id,
                RefreshTokenSession.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if stored is None or stored.revoked_at is not None:
        return False
    stored.revoked_at = _now()
    stored.revoked_reason = "revoked_by_user"
    await session.flush()
    return True


async def change_password(
    session: AsyncSession, user: User, current_password: str, new_password: str
) -> None:
    if not await verify_password_async(current_password, user.password_hash):
        raise AuthenticationError("Текущий пароль неверен", code="invalid_credentials")
    user.password_hash = await hash_password_async(new_password)
    await revoke_all_sessions(session, user.id, reason="password_changed")
    await session.flush()


# ── Одноразовые токены ─────────────────────────────────────────────────
async def issue_one_time_token(
    session: AsyncSession,
    user: User,
    purpose: str,
    *,
    ttl_seconds: int,
    ip_address: str | None = None,
    meta: dict | None = None,
) -> str:
    """Создаёт токен и гасит прежние активные токены того же назначения."""
    await session.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.purpose == purpose,
            OneTimeToken.used_at.is_(None),
            OneTimeToken.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    token = secrets.token_urlsafe(32)  # 256 бит энтропии
    session.add(
        OneTimeToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_hash_one_time(token),
            expires_at=_now() + timedelta(seconds=ttl_seconds),
            requested_ip=(ip_address or "")[:64] or None,
            meta=meta or {},
        )
    )
    await session.flush()
    return token


async def consume_one_time_token(
    session: AsyncSession, token: str, purpose: str
) -> User:
    """Проверяет и «сжигает» токен. Ошибки не различают причину отказа."""
    stored = (
        await session.execute(
            select(OneTimeToken)
            .where(
                OneTimeToken.token_hash == _hash_one_time(token),
                OneTimeToken.purpose == purpose,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if stored is None:
        raise ValidationError("Ссылка недействительна", code="invalid_token")
    if stored.used_at is not None:
        metrics.incr("auth_token_reuse_detected")
        raise ValidationError("Ссылка уже использована", code="token_used")
    if stored.revoked_at is not None:
        raise ValidationError("Ссылка отозвана", code="token_revoked")
    if stored.expires_at <= _now():
        raise ValidationError("Срок действия ссылки истёк", code="token_expired")
    user = await get_user_by_id(session, stored.user_id)
    if user is None:
        raise ValidationError("Ссылка недействительна", code="invalid_token")
    stored.used_at = _now()
    await session.flush()
    return user


# ── TOTP ───────────────────────────────────────────────────────────────
def _totp(user: User):
    import pyotp

    if not user.totp_secret:
        raise ValidationError("2FA не настроена", code="mfa_not_configured")
    secret = secrets_module.decrypt_secret(user.totp_secret)
    return pyotp.TOTP(secret)


def generate_totp_secret(user: User) -> tuple[str, str]:
    """Возвращает (зашифрованный секрет, otpauth-URI для QR)."""
    import pyotp

    secret = pyotp.random_base32()
    encrypted = secrets_module.encrypt_secret(secret)
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name=settings.totp_issuer
    )
    return encrypted, uri


def verify_totp(user: User, code: str) -> bool:
    if not user.totp_secret:
        return False
    code = (code or "").strip().replace(" ", "")
    try:
        return bool(_totp(user).verify(code, valid_window=settings.totp_window))
    except (ValidationError, secrets_module.SecretError):
        return False


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """Возвращает (коды в открытом виде, хеши для хранения)."""
    codes = [f"{secrets.token_hex(5)}-{secrets.token_hex(5)}" for _ in range(MAX_RECOVERY_CODES)]
    hashes = [hashlib.sha256(code.encode()).hexdigest() for code in codes]
    return codes, hashes


async def consume_recovery_code(session: AsyncSession, user: User, code: str) -> bool:
    """Одноразовый код восстановления: при успехе удаляется из списка."""
    normalized = (code or "").strip().lower()
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    stored = user.recovery_codes or []
    match = next((item for item in stored if hmac.compare_digest(item, digest)), None)
    if match is None:
        return False
    user.recovery_codes = [item for item in stored if item != match]
    await session.flush()
    metrics.incr("auth_recovery_code_used")
    return True


async def enable_totp(session: AsyncSession, user: User, secret_encrypted: str) -> list[str]:
    """Включает 2FA и возвращает коды восстановления (показываются один раз)."""
    user.totp_secret = secret_encrypted
    user.totp_enabled_at = _now()
    codes, hashes = generate_recovery_codes()
    user.recovery_codes = hashes
    await session.flush()
    return codes


async def disable_totp(session: AsyncSession, user: User, *, actor_is_admin: bool) -> None:
    if user.role.value in settings.mfa_roles and not actor_is_admin:
        raise ValidationError(
            "Для этой роли 2FA обязательна: отключить может только администратор",
            code="mfa_required_for_role",
        )
    user.totp_secret = None
    user.totp_enabled_at = None
    user.recovery_codes = []
    await session.flush()


# ── API-ключи ──────────────────────────────────────────────────────────
API_KEY_PREFIX = "ac_"


async def issue_api_key(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    scopes: list[str],
    expires_in_days: int | None = None,
) -> tuple[ApiKey, str]:
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    prefix = raw[:11]  # ac_ + 8 символов: достаточно для поиска, бесполезно для подбора
    record = ApiKey(
        user_id=user.id,
        name=name[:80],
        prefix=prefix,
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        scopes=scopes,
        expires_at=_now() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    session.add(record)
    await session.flush()
    return record, raw


async def resolve_api_key(session: AsyncSession, raw: str) -> User | None:
    if not raw.startswith(API_KEY_PREFIX):
        return None
    prefix = raw[:11]
    candidates = (
        await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
    ).scalars().all()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    for candidate in candidates:
        if not hmac.compare_digest(candidate.key_hash, digest):
            continue
        if candidate.revoked_at is not None:
            return None
        if candidate.expires_at is not None and candidate.expires_at <= _now():
            return None
        candidate.last_used_at = _now()
        return await get_user_by_id(session, candidate.user_id)
    return None


def token_ttl() -> int:
    return settings.access_token_ttl_seconds
