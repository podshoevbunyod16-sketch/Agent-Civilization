"""API-key lifecycle and authentication tests."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db.models.api_key import ApiKey
from app.services.api_keys import token_hash


async def test_create_key_use_list_and_revoke(client, user, session):
    created = await client.post(
        "/api/v1/auth/api-keys",
        headers=user.headers,
        json={"name": "MCP from phone", "expires_in_days": 90},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["api_key"]
    assert secret.startswith("ac_live_")
    assert body["key_prefix"] == secret[:16]

    stored = await session.scalar(select(ApiKey).where(ApiKey.id == uuid.UUID(body["id"])))
    assert stored is not None
    assert stored.key_hash == token_hash(secret)
    assert stored.key_hash != secret

    # API keys are purpose-limited: they cannot become general REST sessions.
    forbidden = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {secret}"})
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "api_key_scope"

    listed = await client.get("/api/v1/auth/api-keys", headers=user.headers)
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert len(listed_body) == 1
    assert listed_body[0]["id"] == body["id"]
    assert "api_key" not in listed_body[0]
    assert "key_hash" not in listed_body[0]

    revoked = await client.delete(f"/api/v1/auth/api-keys/{body['id']}", headers=user.headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    denied = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {secret}"})
    assert denied.status_code == 401


async def test_expired_key_is_rejected_by_mcp(client, user, session):
    created = await client.post(
        "/api/v1/auth/api-keys",
        headers=user.headers,
        json={"name": "Expired MCP key"},
    )
    assert created.status_code == 201, created.text

    await session.execute(
        update(ApiKey)
        .where(ApiKey.id == uuid.UUID(created.json()["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    denied = await client.post(
        "/api/v1/mcp",
        headers={"Authorization": f"Bearer {created.json()['api_key']}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_api_key"


async def test_users_cannot_list_or_revoke_another_users_keys(client, user, other_user):
    created = await client.post(
        "/api/v1/auth/api-keys",
        headers=user.headers,
        json={"name": "Private key"},
    )
    key_id = created.json()["id"]

    listed = await client.get("/api/v1/auth/api-keys", headers=other_user.headers)
    assert listed.status_code == 200
    assert listed.json() == []

    revoked = await client.delete(f"/api/v1/auth/api-keys/{key_id}", headers=other_user.headers)
    assert revoked.status_code == 404
