"""Этап 10, J: `.env.example` соответствует конфигурации.

Тест ловит самый частый дрейф: добавили настройку в `Settings`, забыли
документировать — и новый участник не знает, что переменная существует.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _documented() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return {name.lower() for name in re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)}


def test_env_example_exists():
    assert ENV_EXAMPLE.exists(), "backend/.env.example обязателен (production checklist §1.10)"


def test_every_setting_is_documented():
    missing = sorted(set(Settings.model_fields) - _documented())
    assert missing == [], f"не задокументированы в .env.example: {missing}"


def test_no_unknown_variables():
    unknown = sorted(_documented() - set(Settings.model_fields))
    assert unknown == [], f"в .env.example есть переменные, которых нет в Settings: {unknown}"


def test_production_critical_variables_have_examples():
    """Критичные переменные не оставлены «пустыми без пояснения»."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for name in (
        "SECRET_ENCRYPTION_KEYS",
        "INTERNAL_SERVICE_TOKEN",
        "TRUSTED_PROXIES",
        "BACKUP_ENCRYPTION_KEY",
        "PUBLIC_BASE_URL",
    ):
        assert re.search(rf"^# .*\n(?:#.*\n)*{name}=", text, re.M), (
            f"{name} должен быть с пояснением в комментарии выше"
        )
