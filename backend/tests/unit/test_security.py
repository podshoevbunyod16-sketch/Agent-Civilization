"""Unit-тесты криптографии, паролей и JWT."""

from __future__ import annotations

import time

import pytest

from app.core.security import (
    TokenError,
    create_token,
    decode_token,
    generate_node_credentials,
    hash_node_secret,
    hash_password,
    password_needs_rehash,
    sign_node_request,
    verify_node_secret,
    verify_node_signature,
    verify_password,
)


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("CorrectHorseBattery1")
    second = hash_password("CorrectHorseBattery1")
    assert first != second, "хеши должны различаться из-за соли"
    assert verify_password("CorrectHorseBattery1", first)
    assert verify_password("CorrectHorseBattery1", second)
    assert not verify_password("WrongPassword1", first)


def test_password_hash_format_and_rehash_flag():
    encoded = hash_password("AnotherPassword9")
    assert encoded.startswith("pbkdf2_sha256$")
    assert password_needs_rehash(encoded) is False
    assert password_needs_rehash("pbkdf2_sha256$1000$c2FsdA==$aGFzaA==") is True
    assert verify_password("x", "garbage") is False


def test_access_token_roundtrip_and_type_enforcement():
    token, expires_at, jti = create_token("user-1", "access", ttl_seconds=60)
    payload = decode_token(token, expected_type="access")
    assert payload["sub"] == "user-1"
    assert payload["jti"] == jti
    assert expires_at.timestamp() == pytest.approx(payload["exp"], abs=1)
    with pytest.raises(TokenError):
        decode_token(token, expected_type="refresh")


def test_expired_token_rejected():
    token, _, _ = create_token("user-1", "access", ttl_seconds=-1)
    with pytest.raises(TokenError):
        decode_token(token, expected_type="access")


def test_token_with_tampered_signature_rejected():
    token, _, _ = create_token("user-1", "access")
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(TokenError):
        decode_token(tampered, expected_type="access")


def test_node_signature_valid_and_invalid():
    node_key, secret = generate_node_credentials()
    assert node_key.startswith("node_")
    assert verify_node_secret(secret, hash_node_secret(secret))
    assert not verify_node_secret("other", hash_node_secret(secret))

    args = dict(
        node_key=node_key,
        method="POST",
        path="/nodes/me/heartbeat",
        timestamp=str(int(time.time())),
        nonce="nonce-1234567890",
        body=b'{"sequence":1}',
    )
    signature = sign_node_request(secret, **args)
    assert verify_node_signature(secret, signature, **args)
    assert not verify_node_signature(secret, "0" * 64, **args)  # неверная подпись
    assert not verify_node_signature(
        secret, signature, **{**args, "path": "/nodes/me/other"}
    )  # подпись привязана к пути
    assert not verify_node_signature(
        secret, signature, **{**args, "body": b'{"sequence":2}'}
    )  # и к телу запроса


def test_node_signature_is_not_reversible_to_secret():
    """Сервер хранит только хеш: подпись вычисляется от хеша, не от секрета."""
    node_key, secret = generate_node_credentials()
    key_hex = hash_node_secret(secret)
    assert secret not in key_hex
    assert len(key_hex) == 64
