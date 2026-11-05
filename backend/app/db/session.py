"""Асинхронная работа с PostgreSQL.

Движок и фабрика сессий создаются один раз на процесс. Приложение
горизонтально масштабируется: состояние живёт в PostgreSQL и Redis,
а не в памяти процесса (кроме короткоживущих кешей).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(url: str | None = None) -> AsyncEngine:
    """Создаёт движок. В тестах используется NullPool.

    NullPool в тестах нужен потому, что pytest создаёт отдельный event loop
    на каждый тест, а пул asyncpg привязан к циклу, в котором соединение
    было открыто.
    """
    if settings.environment == "test":
        return create_async_engine(
            url or settings.database_url, echo=settings.database_echo, poolclass=NullPool
        )
    return create_async_engine(
        url or settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        globals()["_session_factory"] = _session_factory
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        get_engine()
        assert _session_factory is not None
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: одна сессия на запрос, коммит на выходе."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия для фоновых задач и воркеров."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
