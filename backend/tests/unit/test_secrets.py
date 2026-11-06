"""Этап 10, C: управление секретами — шифрование, версии, ротация, маскирование.

Тесты работают с настоящим AES-GCM: проверяется, что подмена шифротекста,
чужой ключ и чужая «роль» (контекст) отклоняются, а ротация не теряет данные.
"""

from __future__ import annotations

import base64
import json
import logging

import pytest

from app.core import secrets as secrets_module


def _ring(versions=(1,), active=None) -> secrets_module.KeyRing:
    master = "master-key-for-tests-" + "x" * 20
    ring = secrets_module.KeyRing.from_master(master, versions=list(versions,))
    return ring


# ── Шифрование ─────────────────────────────────────────────────────────
def test_encrypt_decrypt_roundtrip():
    ring = _ring()
    token = ring.encrypt("sk-live-секрет-123")
    assert token.startswith("v1:")
    assert "sk-live" not in token
    assert ring.decrypt(token) == "sk-live-секрет-123"


def test_ciphertext_is_randomized():
    ring = _ring()
    # Одинаковый plaintext → разные шифротексты (случайный nonce)
    assert ring.encrypt("same") != ring.encrypt("same")


def test_tampered_ciphertext_rejected():
    ring = _ring()
    token = ring.encrypt("value")
    version, payload = token.split(":", 1)
    raw = bytearray(base64.b64decode(payload))
    raw[-1] ^= 0xFF
    tampered = f"{version}:{base64.b64encode(bytes(raw)).decode()}"
    with pytest.raises(secrets_module.SecretError):
        ring.decrypt(tampered)


def test_wrong_key_rejected():
    first = secrets_module.KeyRing.from_master("master-one-" + "a" * 30)
    second = secrets_module.KeyRing.from_master("master-two-" + "b" * 30)
    with pytest.raises(secrets_module.SecretError):
        second.decrypt(first.encrypt("value"))


def test_context_is_bound_to_ciphertext():
    ring = _ring()
    token = ring.encrypt("value", context=secrets_module.CONTEXT_SECRET)
    # Шифротекст секрета нельзя выдать за бэкап: контекст входит в GCM-тег
    with pytest.raises(secrets_module.SecretError):
        ring.decrypt(token, context=secrets_module.CONTEXT_BACKUP)


def test_empty_secret_rejected():
    with pytest.raises(secrets_module.SecretError):
        _ring().encrypt("")


# ── Версионирование и ротация ──────────────────────────────────────────
def test_rotation_reads_old_and_writes_new():
    old = secrets_module.KeyRing.from_master("master-" + "k" * 30, versions=[1])
    legacy_token = old.encrypt("old-secret")

    rotated = secrets_module.KeyRing.from_master("master-" + "k" * 30, versions=[1, 2])
    assert rotated.active_version == 2
    # Старый секрет читается ключом версии 1
    assert rotated.decrypt(legacy_token) == "old-secret"
    # Новый шифруется версией 2
    assert rotated.encrypt("new-secret").startswith("v2:")

    # Ротация = перешифровка: после неё старый ключ можно убрать
    reencrypted = rotated.encrypt(rotated.decrypt(legacy_token))
    without_v1 = secrets_module.KeyRing(keys={2: rotated.keys[2]}, active_version=2)
    assert without_v1.decrypt(reencrypted) == "old-secret"
    with pytest.raises(secrets_module.UnknownKeyVersion):
        without_v1.decrypt(legacy_token)


def test_unknown_version_has_explicit_error():
    ring = _ring()
    with pytest.raises(secrets_module.UnknownKeyVersion) as exc:
        ring.decrypt("v9:AAAA")
    assert "версией 9" in str(exc.value)


def test_remove_active_key_forbidden():
    ring = _ring(versions=(1, 2))
    with pytest.raises(secrets_module.SecretError):
        ring.remove_key(2)
    ring.remove_key(1)
    assert ring.versions() == [2]


def test_from_env_parses_ring():
    key1 = base64.b64encode(b"1" * 32).decode()
    key2 = base64.b64encode(b"2" * 32).decode()
    ring = secrets_module.KeyRing.from_env(f"1:{key1},2:{key2}")
    assert ring.versions() == [1, 2]
    assert ring.active_version == 2
    assert ring.decrypt(ring.encrypt("x")) == "x"


def test_from_env_rejects_bad_key_length():
    short = base64.b64encode(b"short").decode()
    with pytest.raises(secrets_module.SecretError):
        secrets_module.KeyRing.from_env(f"1:{short}")


def test_fingerprint_does_not_leak_key():
    ring = _ring()
    fingerprint = ring.fingerprint()
    assert fingerprint.startswith("v1:")
    assert len(fingerprint.split(":")[1]) == 12
    assert base64.b64encode(ring.keys[1]).decode()[:8] not in fingerprint


# ── Маскирование ───────────────────────────────────────────────────────
def test_mask_keeps_only_tail():
    assert secrets_module.mask("sk-live-abcdef123") == "***f123"
    assert secrets_module.mask("ab") == "**"
    assert secrets_module.mask(None) == ""


def test_scrub_removes_secrets_from_structures():
    payload = {
        "password": "p",
        "api_key": "k",
        "nested": {"auth_token": "zzz", "keep": "visible"},
        "items": [{"secret": "s"}],
        "note": "Authorization: Bearer abcdefghijklmnop",
        "text": "token=abc123&x=1",
    }
    cleaned = secrets_module.scrub(payload)
    assert cleaned["nested"]["auth_token"] == "***"
    assert cleaned["nested"]["keep"] == "visible"
    assert cleaned["items"][0]["secret"] == "***"
    assert "abcdefghijklmnop" not in cleaned["note"]
    assert "abc123" not in cleaned["text"]
    # Структура не ломается: json-сериализация проходит
    json.dumps(cleaned)


def test_json_logger_filters_secrets(caplog):
    from app.core.logging import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="login with token=%s",
        args=("supersecrettoken",),
        exc_info=None,
    )
    record.auth_token = "plain-secret-value"
    output = formatter.format(record)
    assert "plain-secret-value" not in output
    assert "supersecrettoken" not in output
    payload = json.loads(output)
    assert payload["auth_token"] == "***"
    assert payload["message"].startswith("login with")


def test_log_context_adds_correlation_id():
    from app.core.logging import JsonFormatter, clear_log_context, set_log_context

    set_log_context(request_id="req-123", user_id="user-1")
    try:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        from app.core.logging import ContextFilter

        ContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["correlation_id"] == "req-123"
        assert payload["user_id"] == "user-1"
    finally:
        clear_log_context()


# ── MCP-токены в БД ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mcp_token_encrypted_in_db(client, session):
    from sqlalchemy import select

    from app.db.models.messaging import McpServer
    from tests.conftest import (
        create_agent,
        register_user,  # noqa: F401  (единая точка импорта)
    )

    user = await register_user(client)
    agent = await create_agent(client, user)
    response = await client.put(
        f"/api/v1/agents/{agent['id']}/mcp",
        json={
            "name": "tools",
            "server_url": "https://mcp.example.com/sse",
            "transport": "http",
            "auth_token": "mcp-plain-token",
        },
        headers=user.headers,
    )
    assert response.status_code in (200, 201), response.text
    assert response.json()["has_auth_token"] is True
    # Публичный ответ не раскрывает токен
    assert "mcp-plain-token" not in response.text

    stored = (await session.execute(select(McpServer))).scalar_one()
    assert stored.auth_token is not None
    assert stored.auth_token.startswith("v")
    assert "mcp-plain-token" not in stored.auth_token
    # И расшифровывается тем же ключом (путь доставки воркеру)
    from app.services.messaging import decrypt_mcp_token

    assert decrypt_mcp_token(stored.auth_token) == "mcp-plain-token"


def test_legacy_plaintext_token_still_readable():
    from app.services.messaging import decrypt_mcp_token

    # Значение, сохранённое до этапа 10, читается как есть
    assert decrypt_mcp_token("plain-old-token") == "plain-old-token"
    assert decrypt_mcp_token(None) is None
