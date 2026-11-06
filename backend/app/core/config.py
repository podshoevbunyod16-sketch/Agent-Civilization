"""Application configuration.

Все настройки читаются из переменных окружения (или `.env`).
Секреты никогда не попадают в код и не логируются.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Core ──────────────────────────────────────────────────────────
    project_name: str = "Agent Civilization"
    version: str = "0.1.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # ── Security ──────────────────────────────────────────────────────
    secret_key: str = "dev-insecure-secret-key-change-me-please-32"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    password_min_length: int = 10
    node_request_max_skew_seconds: int = 60

    # ── Datastores ────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_civilization"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # ── Сетевые ограничения (этап 10, A) ─────────────────────────────
    # Разрешать исходящие запросы во внутренние сети. Только для dev/test:
    # в production проверка конфигурации это запрещает.
    ssrf_allow_private: bool = False
    # Таймаут и предельный размер ответа для проверок внешних адресов.
    outbound_timeout_seconds: float = 10.0
    outbound_max_redirects: int = 0

    # ── Секреты (этап 10, C) ──────────────────────────────────────────
    # Кольцо ключей шифрования секретов: "1:<base64 32 байта>,2:<base64>".
    # Активна старшая версия; старые нужны для чтения уже сохранённых секретов.
    # Если не задано — ключи выводятся из SECRET_KEY (годится для dev/test).
    secret_encryption_keys: str = ""
    # Мастер-ключ для бэкапов (отдельный от SECRET_KEY: компрометация бэкапа
    # не должна раскрывать сессии и наоборот). Пусто — выводится из SECRET_KEY.
    backup_encryption_key: str = ""

    # ── Аутентификация и учётные записи (этап 10, B) ──────────────────
    # Подтверждение email: срок жизни токена и режим.
    email_verification_required: bool = False
    email_verification_ttl_seconds: int = 60 * 60 * 24
    # Сброс пароля: короткий срок жизни (одноразовая ссылка).
    password_reset_ttl_seconds: int = 60 * 30
    # TOTP: длина кода, окно допуска (шагов по 30 с), issuer для приложений.
    totp_issuer: str = "Agent Civilization"
    totp_window: int = 1
    # 2FA обязательна для ролей через запятую (по умолчанию только админ,
    # и только при наличии настроенного TOTP: принудительное включение без
    # регистрации устройства заблокировало бы доступ).
    mfa_enforced_roles: str = "admin"
    # Блокировка входа после серии неудач (per-account, в дополнение к IP-лимиту).
    login_max_attempts: int = 10
    login_lockout_seconds: int = 15 * 60
    # Доставка писем: console (dev/test) | smtp (production).
    email_delivery: Literal["console", "smtp"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@agent-civilization.local"
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    # Базовый адрес фронтенда для ссылок в письмах.
    public_base_url: str = "http://localhost:5173"

    # Доверенные прокси: только для них берётся X-Forwarded-For.
    # Пусто — заголовку не доверяем вовсе (иначе лимиты обходятся подменой).
    trusted_proxies: str = ""

    # ── Наблюдаемость (этап 9) ────────────────────────────────────────
    # Метрики раскрывают нагрузку и размеры таблиц, поэтому по умолчанию
    # закрыты сервисным токеном. Внутри кластера можно открыть scrape.
    metrics_public: bool = False

    # Демон обслуживания (этап 9): как часто снимать истёкшие аренды и
    # помечать мёртвые узлы. Значения по умолчанию рассчитаны на 60-секундную
    # аренду: проход каждые 30 с возвращает задачу не позже чем через ~90 с.
    maintenance_interval_seconds: float = 30.0
    node_stale_after_seconds: int = 120

    # ── Rate limiting ─────────────────────────────────────────────────
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = 240
    rate_limit_auth_per_minute: int = 20
    # Изменяющие и критические операции лимитируются отдельно: цена ошибки
    # выше, чем у чтения (деньги, секреты, бэкап).
    rate_limit_write_per_minute: int = 120
    rate_limit_critical_per_minute: int = 20
    # Подписанные запросы узла: heartbeat/claim идут часто, но с известной
    # частотой, поэтому лимит защищает от «шумного соседа» без ложных срабатываний.
    rate_limit_node_per_minute: int = 1200

    # ── CORS (JSON-список или строка через запятую) ───────────────────
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:8000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8000",
        ]
    )

    # ── Execution / sandbox ───────────────────────────────────────────
    sandbox_backend: Literal["noop", "subprocess", "docker"] = "subprocess"
    sandbox_max_cpu_seconds: int = 10
    sandbox_max_memory_mb: int = 256
    sandbox_max_wall_seconds: int = 30
    sandbox_max_output_bytes: int = 65536
    sandbox_allow_network: bool = False
    sandbox_python_image: str = "python:3.12-slim"
    worker_poll_interval_seconds: float = 2.0
    worker_lease_seconds: int = 60
    artifact_storage_dir: str = "./var/artifacts"

    # ── Внутренние сервисы ────────────────────────────────────────────
    # Сервисный токен для внутреннего API доставки сообщений (message worker).
    # В production задаётся отдельным секретом; при пустом значении
    # внутренний API отключён (403), чтобы не открыть его случайно.
    internal_service_token: str = ""
    message_max_attempts: int = 3
    message_worker_poll_interval_seconds: float = 1.0

    # ── Registry ──────────────────────────────────────────────────────
    registry_page_size_default: int = 20
    registry_page_size_max: int = 100

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def trusted_proxy_networks(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxies.split(",") if item.strip()]

    @property
    def mfa_roles(self) -> set[str]:
        return {item.strip() for item in self.mfa_enforced_roles.split(",") if item.strip()}

    def validate_for_runtime(self) -> None:
        """Проверки, которые должны падать при старте в продакшене.

        Правило: в production приложение не запускается в небезопасной
        конфигурации. Молчаливое «оно же работает» здесь дороже падения:
        отказ старта — громкий и исправимый сигнал.
        """
        problems: list[str] = []
        for origin in self.cors_origins:
            if origin == "*":
                problems.append("CORS_ORIGINS не должен содержать '*' (разрешает любой сайт)")
        if self.is_production:
            if "change-me" in self.secret_key.lower() or len(self.secret_key) < 32:
                problems.append("SECRET_KEY должен быть длинным случайным значением")
            if self.debug:
                problems.append("DEBUG должен быть выключен в production")
            if not self.internal_service_token:
                problems.append(
                    "INTERNAL_SERVICE_TOKEN обязателен: без него внутренний API "
                    "отключён и воркеры доставки не работают"
                )
            problems.extend(self.extra_secret_key_problems())
            if not self.cors_origins or any(
                origin.startswith("http://") for origin in self.cors_origins
            ):
                problems.append(
                    "CORS_ORIGINS в production должен содержать только https-адреса"
                )
            if self.sandbox_allow_network:
                problems.append(
                    "SANDBOX_ALLOW_NETWORK=true недопустим в production: "
                    "задание получит доступ к внутренней сети"
                )
            if self.sandbox_backend == "subprocess":
                problems.append(
                    "SANDBOX_BACKEND=subprocess не изолирует сеть на уровне ядра; "
                    "для production требуется docker"
                )
            if self.email_verification_required and self.email_delivery == "console":
                problems.append(
                    "EMAIL_VERIFICATION_REQUIRED=true требует EMAIL_DELIVERY=smtp: "
                    "в console-режиме пользователи не получат ссылку"
                )
            if self.email_delivery == "smtp" and not self.smtp_host:
                problems.append("EMAIL_DELIVERY=smtp требует SMTP_HOST")
            if not self.public_base_url.startswith("https://"):
                problems.append(
                    "PUBLIC_BASE_URL должен быть https-адресом: по ссылке из письма "
                    "передаётся одноразовый токен"
                )
            if self.ssrf_allow_private:
                problems.append(
                    "SSRF_ALLOW_PRIVATE=true недопустим в production: "
                    "исходящие запросы должны блокироваться во внутренние сети"
                )
            if self.rate_limit_enabled and not self.redis_url:
                problems.append("RATE_LIMIT_ENABLED=true требует REDIS_URL")
        if problems:
            raise RuntimeError(
                "Небезопасная конфигурация, запуск остановлен: " + "; ".join(problems)
            )

    def extra_secret_key_problems(self) -> list[str]:
        """Проверка кольца ключей шифрования (вынесена для тестируемости)."""
        from app.core import secrets as secrets_module

        problems: list[str] = []
        try:
            ring = secrets_module.EnvKeyProvider(
                raw_keys=self.secret_encryption_keys or None,
                master=self.secret_key,
            ).key_ring()
        except secrets_module.SecretError as exc:
            return [f"SECRET_ENCRYPTION_KEYS: {exc}"]
        if self.is_production and not self.secret_encryption_keys:
            problems.append(
                "SECRET_ENCRYPTION_KEYS обязателен в production: ключи не должны "
                "выводиться из SECRET_KEY (ротация шифрования не должна требовать "
                "смены ключа подписи сессий)"
            )
        if ring.active_version < 1:
            problems.append("активная версия ключа шифрования должна быть ≥ 1")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
