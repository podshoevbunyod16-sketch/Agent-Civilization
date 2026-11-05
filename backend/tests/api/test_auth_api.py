"""API-тесты аутентификации и владения сессиями."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import PASSWORD


async def test_register_returns_token_pair_and_user(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "New.Developer@Example.com",
            "password": PASSWORD,
            "display_name": "Новый разработчик",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "new.developer@example.com", "email нормализуется"
    assert body["user"]["role"] == "developer"
    assert body["expires_in"] > 0
    assert "password" not in str(body)


async def test_register_rejects_duplicate_email(client, user):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": user.email.upper(), "password": PASSWORD, "display_name": "Dup"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


@pytest.mark.parametrize(
    "password,reason",
    [
        ("short1", "слишком короткий"),
        ("allletters", "только буквы"),
        ("1234567890123", "только цифры"),
    ],
)
async def test_register_rejects_weak_passwords(client, password, reason):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{uuid.uuid4().hex[:8]}@example.com",
            "password": password,
            "display_name": "Weak",
        },
    )
    assert response.status_code == 422, f"{reason}: {response.text}"


async def test_login_success_and_wrong_password(client, user):
    ok = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert ok.status_code == 200
    assert ok.json()["access_token"]

    bad = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "WrongPassword123"}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"

    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "WrongPassword123"},
    )
    assert unknown.status_code == 401
    # Ответ не должен раскрывать, существует ли пользователь
    assert unknown.json()["error"]["message"] == bad.json()["error"]["message"]


async def test_me_requires_token_and_returns_profile(client, user):
    unauthorized = await client.get("/api/v1/auth/me")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "missing_token"

    me = await client.get("/api/v1/auth/me", headers=user.headers)
    assert me.status_code == 200
    assert me.json()["email"] == user.email

    invalid = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert invalid.status_code == 401


async def test_refresh_rotation_invalidates_old_token(client, user):
    first = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": user.refresh_token}
    )
    assert first.status_code == 200, first.text
    new_refresh = first.json()["refresh_token"]
    assert new_refresh != user.refresh_token

    # Повторное использование старого токена — признак компрометации
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": user.refresh_token}
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "token_reuse_detected"

    # ...и новая сессия тоже отозвана
    after = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_refresh}
    )
    assert after.status_code == 401


async def test_access_token_cannot_be_used_as_refresh(client, user):
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": user.access_token}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_logout_revokes_refresh_token(client, user):
    logout = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": user.refresh_token},
        headers=user.headers,
    )
    assert logout.status_code == 200
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": user.refresh_token}
    )
    assert refresh.status_code == 401


async def test_change_password_revokes_all_sessions(client, user):
    change = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "BrandNewPassword2"},
        headers=user.headers,
    )
    assert change.status_code == 200, change.text

    old_login = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "BrandNewPassword2"},
    )
    assert new_login.status_code == 200

    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": user.refresh_token}
    )
    assert refresh.status_code == 401, "старая сессия должна быть отозвана"


async def test_request_id_header_present(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert len(response.headers["X-Request-ID"]) >= 8

    custom = await client.get(
        "/api/v1/health", headers={"X-Request-ID": "my-trace-id"}
    )
    assert custom.headers["X-Request-ID"] == "my-trace-id"


async def test_audit_log_records_registration_and_login(client, user):
    entries = await client.get("/api/v1/audit", headers=user.headers)
    assert entries.status_code == 200
    actions = {item["action"] for item in entries.json()["items"]}
    assert "user.register" in actions


async def test_openapi_contract_is_available(client):
    schema = await client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    for required in ("/api/v1/auth/login", "/api/v1/agents", "/api/v1/nodes/me/heartbeat"):
        assert required in paths
