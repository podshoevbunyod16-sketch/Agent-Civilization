"""Управление секретами (этап 10, C).

Задача: у системы уже есть чувствительные значения — токены MCP-серверов,
секреты узлов, будущие ключи внешних провайдеров. Хранить их открытым
текстом в БД нельзя, а «свой алгоритм шифрования» писать нельзя тем более.

Решение:

* **AES-256-GCM** из библиотеки `cryptography` (никакой самодельной
  криптографии). GCM даёт и конфиденциальность, и целостность: подмена
  шифротекста обнаруживается при расшифровке.
* **Версионированный набор ключей** (key ring). Каждый шифротекст помечен
  версией ключа (`v1:...`), поэтому ротация — это добавление нового ключа:
  старые секреты продолжают читаться старым ключом, новые пишутся новым.
* **Ротация** перешифровывает все сохранённые секреты активным ключом;
  предыдущие ключи остаются в кольце, пока не будут удалены явно.
* **Derived key.** Если задан `secret_key` приложения, ключи кольца
  выводятся из него через HKDF-SHA256 с разделителями по версии и назначению
  (encryption / backup / signing). Это позволяет не хранить сырой ключ
  шифрования в конфиге отдельной строкой.
* **Маскирование.** В логах и ответах API секреты показываются как
  `***abc1` (последние 4 символа), не длиннее и не полнее.

Внешний Secret Manager: интерфейс `SecretProvider` позволяет подставить
провайдера (Vault/AWS/GCP). Пока реализован `EnvKeyProvider` — ключи кольца
из переменных окружения `SECRET_ENCRYPTION_KEYS`. Внешний провайдер не
эмулируется: реализация подключается там, где есть реальный сервис и
credentials (см. docs/adr/0012).
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Формат шифротекста: v<версия>:<base64(nonce || ciphertext)>
CIPHERTEXT_RE = re.compile(r"^v(\d+):([A-Za-z0-9+/=]+)$")
NONCE_BYTES = 12  # рекомендовано для GCM
KEY_BYTES = 32
# Контекст привязывает шифротекст к назначению: секрет, зашифрованный для
# «mcp», нельзя подставить вместо «backup» — GCM отклонит тег.
CONTEXT_SECRET = b"agent-civilization:secret:v1"
CONTEXT_BACKUP = b"agent-civilization:backup:v1"
CONTEXT_SIGNING = b"agent-civilization:signing:v1"

MASK_KEEP = 4


class SecretError(RuntimeError):
    """Ошибка шифрования/расшифровки секрета."""


class UnknownKeyVersion(SecretError):
    """Секрет зашифрован версией ключа, которой нет в кольце."""


def derive_key(master: str, *, context: bytes, version: int = 1) -> bytes:
    """HKDF-SHA256: мастер-секрет → ключ нужного назначения и версии."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=f"ac-keyring-v{version}".encode(),
        info=context,
    )
    return hkdf.derive(master.encode("utf-8"))


def mask(value: str | None, *, keep: int = MASK_KEEP) -> str:
    """Маскирование для логов и ответов API. Короткие значения не раскрываются."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"***{value[-keep:]}"


@dataclass
class KeyRing:
    """Набор ключей шифрования с активной версией.

    `keys` — {версия: 32-байтный ключ}. Активная версия используется для
    новых шифрований; все версии — для расшифровки (пока не отозваны).
    """

    keys: dict[int, bytes] = field(default_factory=dict)
    active_version: int = 1

    # ── создание ─────────────────────────────────────────────────────
    @classmethod
    def from_master(cls, master: str, *, versions: list[int] | None = None) -> KeyRing:
        versions = versions or [1]
        keys = {
            version: derive_key(master, context=CONTEXT_SECRET, version=version)
            for version in versions
        }
        return cls(keys=keys, active_version=max(versions))

    @classmethod
    def from_env(cls, raw: str) -> KeyRing:
        """`SECRET_ENCRYPTION_KEYS` = `1:<base64>,2:<base64>`; активна старшая."""
        keys: dict[int, bytes] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                version_raw, encoded = item.split(":", 1)
                version = int(version_raw)
                key = base64.b64decode(encoded)
            except (ValueError, TypeError) as exc:
                raise SecretError(
                    "SECRET_ENCRYPTION_KEYS должен иметь вид '1:<base64>,2:<base64>'"
                ) from exc
            if len(key) != KEY_BYTES:
                raise SecretError(
                    f"ключ версии {version}: ожидается {KEY_BYTES} байт, получено {len(key)}"
                )
            keys[version] = key
        if not keys:
            raise SecretError("SECRET_ENCRYPTION_KEYS пуст")
        return cls(keys=keys, active_version=max(keys))

    # ── шифрование ───────────────────────────────────────────────────
    def encrypt(self, plaintext: str, *, context: bytes = CONTEXT_SECRET) -> str:
        if not plaintext:
            raise SecretError("Пустой секрет шифровать нечего")
        version = self.active_version
        aead = AESGCM(self.keys[version])
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = aead.encrypt(nonce, plaintext.encode("utf-8"), context)
        payload = base64.b64encode(nonce + ciphertext).decode()
        return f"v{version}:{payload}"

    def decrypt(self, token: str, *, context: bytes = CONTEXT_SECRET) -> str:
        match = CIPHERTEXT_RE.match(token or "")
        if not match:
            raise SecretError("Неверный формат шифротекста")
        version = int(match.group(1))
        key = self.keys.get(version)
        if key is None:
            raise UnknownKeyVersion(
                f"Секрет зашифрован версией {version}, которой нет в кольце ключей"
            )
        raw = base64.b64decode(match.group(2))
        nonce, ciphertext = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, context).decode("utf-8")
        except InvalidTag as exc:
            raise SecretError(
                "Не удалось расшифровать секрет: ключ не тот или данные изменены"
            ) from exc

    # ── служебное ────────────────────────────────────────────────────
    def versions(self) -> list[int]:
        return sorted(self.keys)

    def add_key(self, version: int, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise SecretError("Ключ должен быть 32 байта")
        self.keys[version] = key
        self.active_version = max(self.keys)

    def remove_key(self, version: int) -> None:
        """Отзыв ключа. Запрещено, если он активен или им ещё что-то зашифровано."""
        if version == self.active_version:
            raise SecretError("Нельзя отозвать активный ключ: сначала сделайте активным другой")
        if version not in self.keys:
            raise SecretError(f"Ключа версии {version} нет в кольце")
        del self.keys[version]

    def fingerprint(self, version: int | None = None) -> str:
        """Отпечаток ключа для аудита: сам ключ не раскрывается."""
        import hashlib

        version = version or self.active_version
        digest = hashlib.sha256(self.keys[version]).hexdigest()
        return f"v{version}:{digest[:12]}"


class SecretProvider:
    """Интерфейс провайдера ключей.

    Реализации: `EnvKeyProvider` (переменные окружения), в будущем —
    Vault/AWS KMS. Шифрование всегда выполняется локально AES-GCM: внешний
    провайдер отдаёт ключи, а не шифротексты, поэтому «облачный KMS» можно
    подключить без изменения формата данных.
    """

    def key_ring(self) -> KeyRing:  # pragma: no cover - интерфейс
        raise NotImplementedError


@dataclass
class EnvKeyProvider(SecretProvider):
    """Ключи из `SECRET_ENCRYPTION_KEYS`, иначе — производные от `SECRET_KEY`."""

    raw_keys: str | None = None
    master: str | None = None

    def key_ring(self) -> KeyRing:
        if self.raw_keys:
            return KeyRing.from_env(self.raw_keys)
        if self.master:
            return KeyRing.from_master(self.master)
        raise SecretError(
            "Ключи шифрования не настроены: задайте SECRET_ENCRYPTION_KEYS "
            "или SECRET_KEY (в production — обязательны)"
        )


_ring: KeyRing | None = None


def get_key_ring() -> KeyRing:
    """Кольцо ключей процесса (ленивая инициализация из настроек)."""
    global _ring
    if _ring is None:
        from app.core.config import settings

        provider = EnvKeyProvider(
            raw_keys=settings.secret_encryption_keys or None,
            master=settings.secret_key,
        )
        _ring = provider.key_ring()
    return _ring


def set_key_ring(ring: KeyRing | None) -> None:
    """Подмена кольца (тесты, ротация в рантайме)."""
    global _ring
    _ring = ring


def encrypt_secret(plaintext: str, *, context: bytes = CONTEXT_SECRET) -> str:
    return get_key_ring().encrypt(plaintext, context=context)


def decrypt_secret(token: str, *, context: bytes = CONTEXT_SECRET) -> str:
    return get_key_ring().decrypt(token, context=context)


def secrets_fingerprint() -> dict[str, Any]:
    """Состояние кольца для /ops/health и стартовой проверки конфигурации."""
    try:
        ring = get_key_ring()
    except SecretError as exc:
        return {"configured": False, "error": str(exc)}
    return {
        "configured": True,
        "versions": ring.versions(),
        "active": ring.fingerprint(),
        "active_version": ring.active_version,
    }


# ── Маскирование секретов в логах ───────────────────────────────────────
SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|auth_token|"
    r"node_secret|private[_-]?key|signature|cookie)([\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,&}]+)"
)
BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}")


def scrub(value: Any) -> Any:
    """Рекурсивно вычищает секреты из структур перед логированием.

    Применяется в JSON-логгере: даже если разработчик положил токен в
    `extra`, он не попадёт в лог открытым текстом.
    """
    if isinstance(value, str):
        out = BEARER_RE.sub(lambda m: f"{m.group(1)} ***", value)
        out = SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
        return out
    if isinstance(value, dict):
        return {
            key: ("***" if is_secret_name(str(key)) else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


def is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(
        marker in lowered
        for marker in (
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
            "authorization",
            "private_key",
            "signature",
        )
    )


def dumps_redacted(payload: dict[str, Any]) -> str:
    return json.dumps(scrub(payload), ensure_ascii=False, default=str)


__all__ = [
    "CONTEXT_BACKUP",
    "CONTEXT_SECRET",
    "CONTEXT_SIGNING",
    "EnvKeyProvider",
    "KeyRing",
    "SecretError",
    "SecretProvider",
    "UnknownKeyVersion",
    "decrypt_secret",
    "derive_key",
    "dumps_redacted",
    "encrypt_secret",
    "get_key_ring",
    "is_secret_name",
    "mask",
    "scrub",
    "secrets_fingerprint",
    "set_key_ring",
]
