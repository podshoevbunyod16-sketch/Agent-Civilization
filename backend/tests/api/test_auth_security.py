"""Этап 10, B: подтверждение email, восстановление доступа, 2FA, сессии, API-ключи.

Каждый тест проверяет реальное поведение через HTTP API и состояние БД:
никаких заглушек вместо проверяемого механизма.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from tests.conftest import PASSWORD, register_user


async def _login(client, email: str, password: str = PASSWORD, totp_code: str | None = None):
    body: dict = {"email": email, "password": password}
    if totp_code:
        body["totp_code"] = totp_code
    return await client.post("/api/v1/auth/login", json=body)


def _totp_now(secret: str) -> str:
    import pyotp

    return pyotp.TOTP(secret).now()


# ── Подтверждение email ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_email_verification_full_flow(client, user):
    # По умолчанию подтверждение не требуется, но адрес остаётся неподтверждённым
    assert (await client.get("/api/v1/auth/me", headers=user.headers)).json()[
        "email_verified"
    ] is False

    requested = await client.post(
        "/api/v1/auth/email/request-verification", headers=user.headers
    )
    assert requested.status_code == 200, requested.text
    token = requested.json()["dev_token"]
    assert token and len(token) >= 40  # 32 байта энтропии в base64url

    verified = await client.post("/api/v1/auth/email/verify", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert verified.json()["email_verified"] is True

    me = await client.get("/api/v1/auth/me", headers=user.headers)
    assert me.json()["email_verified"] is True


@pytest.mark.asyncio
async def test_email_token_is_single_use(client, user):
    token = (
        await client.post("/api/v1/auth/email/request-verification", headers=user.headers)
    ).json()["dev_token"]
    assert (
        await client.post("/api/v1/auth/email/verify", json={"token": token})
    ).status_code == 200
    reused = await client.post("/api/v1/auth/email/verify", json={"token": token})
    assert reused.status_code in (400, 422)
    assert reused.json()["error"]["code"] == "token_used"


@pytest.mark.asyncio
async def test_new_token_revokes_previous(client, user):
    first = (
        await client.post("/api/v1/auth/email/request-verification", headers=user.headers)
    ).json()["dev_token"]
    second = (
        await client.post("/api/v1/auth/email/request-verification", headers=user.headers)
    ).json()["dev_token"]
    assert first != second
    stale = await client.post("/api/v1/auth/email/verify", json={"token": first})
    assert stale.status_code in (400, 422)
    assert stale.json()["error"]["code"] == "token_revoked"


@pytest.mark.asyncio
async def test_token_stored_only_as_hash(client, user, session):
    from app.db.models.user import OneTimeToken

    token = (
        await client.post("/api/v1/auth/email/request-verification", headers=user.headers)
    ).json()["dev_token"]
    stored = (await session.execute(select(OneTimeToken))).scalars().all()
    assert len(stored) == 1
    # В БД нет токена открытым текстом — только хеш
    assert token not in stored[0].token_hash
    assert len(stored[0].token_hash) == 64


# ── Восстановление доступа ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_password_reset_flow_revokes_sessions(client, user):
    issued = await client.post("/api/v1/auth/password/forgot", json={"email": user.email})
    assert issued.status_code == 200, issued.text
    token = issued.json()["dev_token"]
    assert token

    new_password = "BrandNewPass123"
    reset = await client.post(
        "/api/v1/auth/password/reset", json={"token": token, "new_password": new_password}
    )
    assert reset.status_code == 200, reset.text

    # Старые сессии отозваны: access-токен больше не работает
    stale = await client.get("/api/v1/auth/me", headers=user.headers)
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "session_revoked"

    # Старый пароль не подходит, новый — работает
    assert (await _login(client, user.email)).status_code == 401
    assert (await _login(client, user.email, new_password)).status_code == 200


@pytest.mark.asyncio
async def test_password_reset_is_single_use(client, user):
    token = (
        await client.post("/api/v1/auth/password/forgot", json={"email": user.email})
    ).json()["dev_token"]
    body = {"token": token, "new_password": "AnotherPass123"}
    assert (await client.post("/api/v1/auth/password/reset", json=body)).status_code == 200
    again = await client.post("/api/v1/auth/password/reset", json=body)
    assert again.status_code in (400, 422)
    assert again.json()["error"]["code"] == "token_used"


@pytest.mark.asyncio
async def test_forgot_password_does_not_leak_account_existence(client):
    known = await register_user(client)
    existing = await client.post("/api/v1/auth/password/forgot", json={"email": known.email})
    unknown = await client.post(
        "/api/v1/auth/password/forgot", json={"email": f"nobody_{uuid.uuid4().hex[:6]}@example.com"}
    )
    # Одинаковый код и одинаковый текст: перечислить пользователей нельзя
    assert existing.status_code == unknown.status_code == 200
    assert existing.json()["message"] == unknown.json()["message"]
    # Токен выпускается только для существующего адреса
    assert existing.json()["dev_token"]
    assert unknown.json()["dev_token"] is None


@pytest.mark.asyncio
async def test_expired_token_rejected(client, user, session):
    from datetime import UTC, datetime, timedelta

    from app.db.models.user import OneTimeToken

    token = (
        await client.post("/api/v1/auth/email/request-verification", headers=user.headers)
    ).json()["dev_token"]
    record = (await session.execute(select(OneTimeToken))).scalar_one()
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    response = await client.post("/api/v1/auth/email/verify", json={"token": token})
    assert response.status_code in (400, 422)
    assert response.json()["error"]["code"] == "token_expired"


# ── Защита от перебора ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bruteforce_lockout_by_account(client, user):
    from app.core.config import settings

    for _ in range(settings.login_max_attempts):
        response = await _login(client, user.email, "WrongPassword123")
        assert response.status_code == 401
    # После серии неудач блокируется даже верный пароль
    blocked = await _login(client, user.email)
    assert blocked.status_code == 401
    assert blocked.json()["error"]["code"] == "account_locked"
    assert blocked.json()["error"]["details"]["retry_after_seconds"] > 0


@pytest.mark.asyncio
async def test_unknown_email_locked_the_same_way(client):
    from app.core.config import settings

    email = f"ghost_{uuid.uuid4().hex[:6]}@example.com"
    for _ in range(settings.login_max_attempts):
        await _login(client, email, "WrongPassword123")
    blocked = await _login(client, email, "WrongPassword123")
    # Ответ на несуществующий адрес неотличим от существующего
    assert blocked.json()["error"]["code"] == "account_locked"


@pytest.mark.asyncio
async def test_successful_login_resets_counter(client, user):
    await _login(client, user.email, "WrongPassword123")
    assert (await _login(client, user.email)).status_code == 200
    # После успешного входа серия неудач сброшена
    for _ in range(3):
        await _login(client, user.email, "WrongPassword123")
    assert (await _login(client, user.email)).status_code == 200


# ── 2FA (TOTP) ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_totp_full_flow(client, user):
    setup = await client.post("/api/v1/auth/2fa/setup", headers=user.headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")

    # До подтверждения кодом 2FA не активна
    early = await client.get("/api/v1/auth/me/security", headers=user.headers)
    assert early.json()["mfa_enabled"] is False

    enabled = await client.post(
        "/api/v1/auth/2fa/enable", json={"code": _totp_now(secret)}, headers=user.headers
    )
    assert enabled.status_code == 200, enabled.text
    recovery_codes = enabled.json()["recovery_codes"]
    assert len(recovery_codes) == 10

    security = await client.get("/api/v1/auth/me/security", headers=user.headers)
    assert security.json()["mfa_enabled"] is True

    # Вход без кода требует второй фактор
    no_code = await _login(client, user.email)
    assert no_code.status_code == 401
    assert no_code.json()["error"]["code"] == "mfa_required"

    # Неверный код отклоняется
    bad = await _login(client, user.email, totp_code="000000")
    assert bad.json()["error"]["code"] == "invalid_mfa_code"

    # Верный код пускает
    ok = await _login(client, user.email, totp_code=_totp_now(secret))
    assert ok.status_code == 200, ok.text

    # Код восстановления работает один раз
    code = recovery_codes[0]
    first = await _login(client, user.email, totp_code=code)
    assert first.status_code == 200, first.text
    second = await _login(client, user.email, totp_code=code)
    assert second.json()["error"]["code"] == "invalid_mfa_code"


@pytest.mark.asyncio
async def test_totp_secret_encrypted_at_rest(client, user, session):
    from app.db.models.user import User

    setup = await client.post("/api/v1/auth/2fa/setup", headers=user.headers)
    secret = setup.json()["secret"]
    await client.post(
        "/api/v1/auth/2fa/enable", json={"code": _totp_now(secret)}, headers=user.headers
    )
    stored = (
        await session.execute(select(User).where(User.id == uuid.UUID(user.id)))
    ).scalar_one()
    # В БД шифротекст с версией ключа, а не base32-секрет
    assert stored.totp_secret is not None
    assert stored.totp_secret.startswith("v")
    assert secret not in stored.totp_secret
    # Коды восстановления — хеши, не открытые значения
    assert all(len(code) == 64 for code in stored.recovery_codes)


@pytest.mark.asyncio
async def test_totp_disable_requires_valid_code(client, user):
    setup = await client.post("/api/v1/auth/2fa/setup", headers=user.headers)
    secret = setup.json()["secret"]
    await client.post(
        "/api/v1/auth/2fa/enable", json={"code": _totp_now(secret)}, headers=user.headers
    )
    bad = await client.post(
        "/api/v1/auth/2fa/disable", json={"code": "123456"}, headers=user.headers
    )
    assert bad.status_code in (400, 422)
    ok = await client.post(
        "/api/v1/auth/2fa/disable", json={"code": _totp_now(secret)}, headers=user.headers
    )
    assert ok.status_code == 200
    assert (await _login(client, user.email)).status_code == 200


@pytest.mark.asyncio
async def test_admin_role_warns_when_mfa_missing(client, admin_user):
    security = await client.get("/api/v1/auth/me/security", headers=admin_user.headers)
    body = security.json()
    assert body["mfa_required"] is True
    assert body["mfa_enabled"] is False
    assert any("2FA" in warning for warning in body["warnings"])


@pytest.mark.asyncio
async def test_enable_without_setup_rejected(client, user):
    response = await client.post(
        "/api/v1/auth/2fa/enable", json={"code": "123456"}, headers=user.headers
    )
    assert response.status_code in (400, 422)
    assert response.json()["error"]["code"] in ("mfa_setup_not_started", "invalid_mfa_code")


# ── Сессии ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sessions_listing_and_revoke(client, user):
    second_login = await _login(client, user.email)
    assert second_login.status_code == 200
    second = second_login.json()

    sessions_response = await client.get("/api/v1/auth/sessions", headers=user.headers)
    assert sessions_response.status_code == 200, sessions_response.text
    sessions_list = sessions_response.json()
    assert len(sessions_list) == 2
    assert sum(1 for item in sessions_list if item["is_current"]) == 1
    # В ответе есть всё для распознавания устройства и нет refresh-токена
    for item in sessions_list:
        assert "user_agent" in item and "ip_address" in item
        assert "refresh" not in " ".join(item.keys())

    other = next(item for item in sessions_list if not item["is_current"])
    revoked = await client.delete(
        f"/api/v1/auth/sessions/{other['id']}", headers=user.headers
    )
    assert revoked.status_code == 200

    # Отозванная сессия больше не может обновлять токены
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert refresh.status_code == 401

    remaining = await client.get("/api/v1/auth/sessions", headers=user.headers)
    assert len(remaining.json()) == 1


@pytest.mark.asyncio
async def test_access_token_bound_to_session(client, user, session):
    from sqlalchemy import update

    from app.db.models.user import RefreshTokenSession

    # Отзываем сессию «в обход» API: access-токен должен умереть сразу
    await session.execute(
        update(RefreshTokenSession).values(revoked_at=__import__("datetime").datetime.now(
            __import__("datetime").UTC))
    )
    await session.commit()
    response = await client.get("/api/v1/auth/me", headers=user.headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_revoked"


@pytest.mark.asyncio
async def test_cannot_revoke_foreign_session(client, user, other_user):
    other_sessions = await client.get("/api/v1/auth/sessions", headers=other_user.headers)
    victim_session_id = other_sessions.json()[0]["id"]
    response = await client.delete(
        f"/api/v1/auth/sessions/{victim_session_id}", headers=user.headers
    )
    assert response.status_code == 404
    # Сессия другого пользователя жива
    assert (await client.get("/api/v1/auth/me", headers=other_user.headers)).status_code == 200


@pytest.mark.asyncio
async def test_refresh_reuse_detection_revokes_all(client, user):
    login_again = await _login(client, user.email)
    refresh_token = login_again.json()["refresh_token"]
    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200

    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "token_reuse_detected"

    # Все сессии пользователя отозваны, включая исходную
    assert (await client.get("/api/v1/auth/me", headers=user.headers)).status_code == 401


# ── API-ключи ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_api_key_lifecycle(client, user):
    created = await client.post(
        "/api/v1/auth/api-keys",
        json={"name": "CI", "scopes": ["read"]},
        headers=user.headers,
    )
    assert created.status_code == 201, created.text
    raw = created.json()["key"]
    assert raw.startswith("ac_")
    key_id = created.json()["api_key"]["id"]

    # Ключ работает вместо JWT
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert me.status_code == 200
    assert me.json()["email"] == user.email

    # В листинге нет открытого ключа, только префикс
    listed = await client.get("/api/v1/auth/api-keys", headers=user.headers)
    assert listed.status_code == 200
    assert raw not in listed.text
    assert listed.json()[0]["prefix"] == raw[:11]

    revoked = await client.delete(f"/api/v1/auth/api-keys/{key_id}", headers=user.headers)
    assert revoked.status_code == 200
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
    ).status_code == 401


@pytest.mark.asyncio
async def test_api_key_cannot_revoke_foreign(client, user, other_user):
    created = await client.post(
        "/api/v1/auth/api-keys", json={"name": "mine"}, headers=other_user.headers
    )
    key_id = created.json()["api_key"]["id"]
    response = await client.delete(f"/api/v1/auth/api-keys/{key_id}", headers=user.headers)
    assert response.status_code == 404
    raw = created.json()["key"]
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
    ).status_code == 200


@pytest.mark.asyncio
async def test_expired_api_key_rejected(client, user, session):
    from datetime import UTC, datetime, timedelta

    from app.db.models.user import ApiKey

    created = await client.post(
        "/api/v1/auth/api-keys", json={"name": "old"}, headers=user.headers
    )
    raw = created.json()["key"]
    record = (await session.execute(select(ApiKey))).scalar_one()
    record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
    ).status_code == 401


@pytest.mark.asyncio
async def test_garbage_api_key_like_token_rejected(client):
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer ac_notarealkey"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
