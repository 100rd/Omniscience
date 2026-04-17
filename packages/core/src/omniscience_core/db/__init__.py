"""Database engine and session factory for Omniscience.

Usage::

    from omniscience_core.db import create_async_engine, create_session_factory

    engine = create_async_engine(settings)
    async_session = create_session_factory(engine)

    async with async_session() as session:
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_async_engine,
)

if TYPE_CHECKING:
    from omniscience_core.config import Settings


def create_async_engine(settings: Settings) -> AsyncEngine:
    """Create an :class:`AsyncEngine` from *settings*.

    Connection-pool parameters are sized for a typical backend service:
    - ``pool_size=10`` — ten persistent connections per process
    - ``max_overflow=20`` — up to 20 additional burst connections
    - ``pool_pre_ping=True`` — recycle stale connections transparently
    """
    return _sa_create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=settings.environment == "development",
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a bound :class:`async_sessionmaker` for *engine*.

    ``expire_on_commit=False`` prevents lazy-load errors after ``commit()``
    in async contexts where the connection is already closed.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


__all__ = [
    "AsyncEngine",
    "AsyncSession",
    "async_sessionmaker",
    "create_async_engine",
    "create_session_factory",
]
