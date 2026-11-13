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
    ws_ticket_ttl_seconds: int = 60
    password_min_length: int = 10
    node_request_max_skew_seconds: int = 60
    # Доверенные обратные прокси (точные IP или CIDR). Пока пусто —
    # заголовок X-Forwarded-For НЕ доверяется (anti-spoofing rate limit / аудит).
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Fail-closed anti-replay, если Redis недоступен (в production включён по умолчанию)
    anti_replay_fail_closed: bool | None = None

    # ── Datastores ────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_civilization"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"

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

    @field_validator("cors_origins", "trusted_proxies", mode="before")
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

    def anti_replay_closed(self) -> bool:
        """Fail-closed anti-replay: по умолчанию в production/staging, иначе open."""
        if self.anti_replay_fail_closed is not None:
            return self.anti_replay_fail_closed
        return self.environment in {"staging", "production"}

    def validate_for_runtime(self) -> None:
        """Проверки, которые должны падать при старте в продакшене."""
        if self.is_production:
            if "change-me" in self.secret_key.lower() or len(self.secret_key) < 32:
                raise RuntimeError("SECRET_KEY must be set to a strong value in production")
            if self.debug:
                raise RuntimeError("DEBUG must be false in production")
            if self.sandbox_backend != "docker":
                raise RuntimeError("SANDBOX_BACKEND must be 'docker' in production")
            if not self.internal_service_token:
                logger_note = "INTERNAL_SERVICE_TOKEN не задан: внутренний API доставки отключён"
                raise RuntimeError(logger_note)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
