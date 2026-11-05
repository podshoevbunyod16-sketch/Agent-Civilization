"""Криптография, пароли, JWT и подпись запросов инфраструктурных узлов."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from jwt.exceptions import InvalidTokenError

from app.core.config import settings

ALGORITHM = "HS256"
TokenType = Literal["access", "refresh"]

# ── Пароли (PBKDF2-HMAC-SHA256, без внешних зависимостей) ──────────────
_PBKDF2_ROUNDS = 260_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt_b64}${digest_b64}"


# Отдельный пул для PBKDF2. Общий пул asyncio (min(32, cpu+4)) мал: вход —
# единственная массово параллельная CPU-операция, и он вытеснял бы остальные
# задачи. Размер ограничен, чтобы всплеск попыток входа не съел процессор:
# сверху это всё равно ограничено rate-limit'ом (20/мин на IP).
_HASH_POOL_SIZE = max(4, min(64, (os.cpu_count() or 4) * 4))
_hash_pool = ThreadPoolExecutor(max_workers=_HASH_POOL_SIZE, thread_name_prefix="pbkdf2")


async def hash_password_async(password: str) -> str:
    """PBKDF2 (260k раундов) в выделенном пуле потоков.

    Синхронный вызов блокирует event loop на ~10 мс: при нагрузке вход
    упирался в 15 rps независимо от числа реплик API (нагрузочный тест,
    этап 9). После переноса в потоки — 27 rps на том же железе, что уже
    упирается в CPU, а не в event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_hash_pool, hash_password, password)


async def verify_password_async(password: str, encoded: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_hash_pool, verify_password, password, encoded)


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds_raw, salt_raw, digest_raw = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(digest_raw)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds_raw))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_needs_rehash(encoded: str) -> bool:
    try:
        _, rounds_raw, _, _ = encoded.split("$")
        return int(rounds_raw) < _PBKDF2_ROUNDS
    except (ValueError, TypeError):
        return True


# ── Токены ─────────────────────────────────────────────────────────────
class TokenError(Exception):
    """Токен недействителен (подпись/срок/тип)."""


def create_token(
    subject: str,
    token_type: TokenType,
    *,
    ttl_seconds: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime, str]:
    """Возвращает (jwt, expires_at, jti)."""
    now = datetime.now(UTC)
    if ttl_seconds is None:
        ttl_seconds = (
            settings.access_token_ttl_seconds
            if token_type == "access"
            else settings.refresh_token_ttl_seconds
        )
    expires_at = now + timedelta(seconds=ttl_seconds)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": jti,
        "iss": settings.project_name,
    }
    if extra_claims:
        payload.update(extra_claims)
    encoded = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return encoded, expires_at, jti


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[ALGORITHM],
            issuer=settings.project_name,
            options={"require": ["exp", "sub", "type", "jti"]},
        )
    except InvalidTokenError as exc:  # pragma: no cover - defensive
        raise TokenError("invalid token") from exc
    if payload.get("type") != expected_type:
        raise TokenError(f"expected {expected_type} token")
    return payload


def token_fingerprint(jti: str) -> str:
    """Отпечаток jti для хранения в Redis-блоклисте (без утечки значения)."""
    return hashlib.sha256(f"{jti}:{settings.secret_key}".encode()).hexdigest()


# ── Подпись запросов узлов (HMAC-SHA256) ───────────────────────────────
def generate_node_credentials() -> tuple[str, str]:
    """Возвращает (node_key, node_secret). Секрет показывается один раз."""
    return f"node_{secrets.token_hex(8)}", secrets.token_urlsafe(32)


def hash_node_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_node_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_node_secret(secret), stored_hash)


def build_signing_payload(
    *,
    node_key: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    canonical = "\n".join(
        [node_key, method.upper(), path, timestamp, nonce, body_hash]
    )
    return canonical.encode()


def sign_node_request(
    secret: str,
    *,
    node_key: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes = b"",
) -> str:
    payload = build_signing_payload(
        node_key=node_key,
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_node_signature(
    secret: str,
    signature: str,
    *,
    node_key: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes = b"",
) -> bool:
    expected = sign_node_request(
        secret,
        node_key=node_key,
        method=method,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    return hmac.compare_digest(expected, signature)


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
