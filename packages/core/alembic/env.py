"""Alembic environment configuration — async SQLAlchemy with autogenerate support."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# ---------------------------------------------------------------------------
# Alembic config object
# ---------------------------------------------------------------------------

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Import models so autogenerate can detect them
# ---------------------------------------------------------------------------

# Side-effect import — registers all mapped classes onto Base.metadata
from omniscience_core.db.models import Base  # noqa: E402

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Database URL resolution
# ---------------------------------------------------------------------------


def _get_database_url() -> str:
    """Resolve the database URL from env var or alembic.ini."""
    url = os.getenv("DATABASE_URL")
    if url:
        # Normalise sync drivers to asyncpg for Alembic
        url = url.replace("postgresql://", "postgresql+asyncpg://")
        url = url.replace("postgres://", "postgresql+asyncpg://")
        return url
    return config.get_main_option("sqlalchemy.url", "")


# ---------------------------------------------------------------------------
# Offline mode (generate SQL without connecting)
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without a live DB connection."""
    url = _get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode (connect and apply migrations)
# ---------------------------------------------------------------------------


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against a live async engine."""
    connectable = create_async_engine(
        _get_database_url(),
        poolclass=pool.NullPool,  # No pooling during migrations
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migration mode."""
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
