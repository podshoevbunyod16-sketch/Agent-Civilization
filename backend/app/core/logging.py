"""Структурированное JSON-логирование (совместимо с Loki/ELK)."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

from app.core.config import settings

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """JSON-лог с обязательной очисткой секретов.

    Очистка выполняется здесь, а не в местах логирования: правило «секреты
    не попадают в лог» должно держаться независимо от аккуратности
    разработчика. Даже если токен передан в `extra` или вставлен в текст
    сообщения, он будет замаскирован (`scrub`).
    """

    def format(self, record: logging.LogRecord) -> str:
        from app.core.secrets import is_secret_name, scrub

        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
            # Correlation ID: передаётся через фильтр, поэтому попадает в
            # каждую запись внутри запроса, включая фоновые задачи.
            "correlation_id": getattr(record, "request_id", None),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                # Маскируем и по имени поля: `auth_token="..."` не должен
                # попасть в лог, даже если это простое значение без «секретных»
                # подстрок в самом тексте.
                payload[key] = "***" if is_secret_name(key) else scrub(value)
        if record.exc_info:
            payload["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


_context: ContextVar[dict[str, str | None] | None] = ContextVar("log_context", default=None)


def set_log_context(**fields: str | None) -> None:
    _context.set({key: value for key, value in fields.items() if value})


def clear_log_context() -> None:
    _context.set({})


class ContextFilter(logging.Filter):
    """Добавляет correlation id (и user/route) в каждую запись.

    Correlation id живёт в `ContextVar`, поэтому автоматически попадает и в
    записи, сделанные в глубине вызова внутри того же запроса.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = _context.get() or {}
        if not getattr(record, "request_id", None):
            record.request_id = context.get("request_id")
        for key in ("user_id", "route", "node_key"):
            if context.get(key) and not hasattr(record, key):
                setattr(record, key, context[key])
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
