"""Live-Postgres alembic migration smoke test.

Context
-------
``docs/reviews/2026-06-10-hardening-review.md`` lists, under "Open (separate
effort)": *"a real-Postgres migration smoke test in CI (would have caught
the 0010 uuid bug)"*. Migration ``0010_backfill_null_tenant`` originally
bound a plain string parameter to a Postgres ``uuid`` column
(``DatatypeMismatch``) -- unit tests never caught it because nothing in the
suite ever calls ``alembic upgrade head`` against a real database.

Confirmed gap: every "live" integration test that needs Postgres schema
(e.g. ``tests/integration/test_ap3_real_reconciler_live.py``) provisions it
via ``Base.metadata.create_all`` -- the SQLAlchemy ORM's own DDL, generated
directly from the model definitions. That path can never fail the way a
hand-written alembic migration can (wrong bind type, wrong cast, missing
``IF NOT EXISTS``, a migration that only works against a database that
already has prior migrations' side effects). The alembic scripts under
``packages/core/alembic/versions/`` are therefore never executed by any
automated test.

This module closes that gap: it runs the real alembic migration chain
(``alembic upgrade head``) against a real, throwaway Postgres database and
asserts the result is usable.

Gate
----
Requires ``DATABASE_URL`` to point at a reachable Postgres server with
privileges to ``CREATE DATABASE`` / ``DROP DATABASE`` (true for the
``omniscience`` role provisioned by ``docker-compose.yml`` and by the
``postgres:16-alpine`` service container in ``.github/workflows/ci.yml``).
Skips gracefully when unset, matching the existing live-test convention in
this package (see ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS`` etc.).

Isolation
---------
Each test creates its own uniquely-named throwaway database via an
autocommit admin connection to the ``postgres`` maintenance database, runs
migrations against *that* database only, and drops it afterwards. This
never touches the schema/data any other test creates via
``Base.metadata.create_all`` in the same Postgres instance.

Driver note
-----------
Only ``asyncpg`` is a project dependency (no ``psycopg2``), so every
database operation here goes through
``sqlalchemy.ext.asyncio.create_async_engine`` -- including schema
inspection, via ``AsyncConnection.run_sync``. Alembic's own ``command``
API is synchronous and internally drives its async engine with
``asyncio.run()`` (see ``packages/core/alembic/env.py``), so it must be
invoked from a plain synchronous test function, never from inside an
already-running event loop.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

_PG_URL = os.environ.get("DATABASE_URL", "")

_LIVE_PG = pytest.mark.skipif(
    not _PG_URL,
    reason="set DATABASE_URL to a reachable Postgres to run live alembic migration tests",
)

_ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "packages" / "core" / "alembic"

# The head revision of the migration chain at the time this test was written
# (packages/core/alembic/versions/0014_entity_content_hash.py). If this
# assertion ever fails because a new migration landed, that is expected --
# update the constant alongside the new migration.
_EXPECTED_HEAD_REVISION = "0014"

# Every table declared as a SQLAlchemy ORM model in
# omniscience_core.db.models.Base.metadata. The migration chain must create
# all of them -- a model added without a matching migration (or vice versa)
# is exactly the kind of drift this test exists to catch.
_EXPECTED_ORM_TABLES = frozenset(
    {
        "workspaces",
        "sources",
        "documents",
        "ingestion_runs",
        "chunks",
        "api_tokens",
        "entities",
        "edges",
        "entity_emitter",
        "outbox_events",
    }
)

# Columns whose live Postgres type must be a true `uuid` column -- the exact
# class of bug that shipped in migration 0010 (a bare string bound to a uuid
# column raised asyncpg.DatatypeMismatch, uncaught because no test exercised
# a real Postgres connection).
_EXPECTED_UUID_COLUMNS = (
    ("sources", "tenant_id"),
    ("api_tokens", "workspace_id"),
    ("entities", "source_id"),
    ("workspaces", "id"),
)


def _normalize(url: str) -> URL:
    made = make_url(url)
    if made.drivername in ("postgresql", "postgres"):
        made = made.set(drivername="postgresql+asyncpg")
    return made


def _admin_url(pg_url: str) -> str:
    """Return *pg_url* pointed at the ``postgres`` maintenance database."""
    return _normalize(pg_url).set(database="postgres").render_as_string(hide_password=False)


def _throwaway_url(pg_url: str, dbname: str) -> str:
    return _normalize(pg_url).set(database=dbname).render_as_string(hide_password=False)


async def _create_database(pg_url: str, dbname: str) -> None:
    engine = create_async_engine(_admin_url(pg_url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await engine.dispose()


async def _drop_database(pg_url: str, dbname: str) -> None:
    engine = create_async_engine(_admin_url(pg_url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # Disconnect any lingering session before DROP DATABASE.
            await conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                ),
                {"dbname": dbname},
            )
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{dbname}"'))
    finally:
        await engine.dispose()


@pytest.fixture()
def _throwaway_database() -> Iterator[str]:
    """Create a uniquely-named database for one test, then drop it.

    Yields the ``postgresql+asyncpg://`` URL of the throwaway database
    (matching the driver ``omniscience_core.config.Settings`` and
    ``alembic/env.py`` both expect for ``DATABASE_URL``).
    """
    dbname = f"omniscience_migration_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_create_database(_PG_URL, dbname))
    try:
        yield _throwaway_url(_PG_URL, dbname)
    finally:
        asyncio.run(_drop_database(_PG_URL, dbname))


def _run_alembic_upgrade_head(database_url: str) -> None:
    """Run the real migration chain against *database_url*.

    ``alembic/env.py`` reads ``DATABASE_URL`` from the environment in
    preference to ``alembic.ini``, so the target database is selected by
    temporarily overriding that env var for the duration of the call.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _fetch_alembic_version(database_url: str) -> str | None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            row = result.first()
            return row[0] if row else None
    finally:
        await engine.dispose()


async def _fetch_table_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: set(sa.inspect(sync_conn).get_table_names())
            )
    finally:
        await engine.dispose()


async def _fetch_columns(database_url: str, table: str) -> dict[str, str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: sa.inspect(sync_conn).get_columns(table)
            )
            return {c["name"]: str(c["type"]).upper() for c in columns}
    finally:
        await engine.dispose()


@_LIVE_PG
def test_alembic_upgrade_head_succeeds_on_fresh_database(_throwaway_database: str) -> None:
    """The full migration chain must apply cleanly to a blank database.

    This is the exact regression class from the 0010 hardening bug: a
    migration that passes on mocks/ORM-generated-DDL backends but raises on
    real Postgres bind-parameter type enforcement
    (``asyncpg.DatatypeMismatch``).
    """
    _run_alembic_upgrade_head(_throwaway_database)


@_LIVE_PG
def test_alembic_head_matches_expected_revision(_throwaway_database: str) -> None:
    """The migration chain resolves to a single, known head revision.

    Guards against an accidental branch (two migrations both claiming the
    same ``down_revision``) going unnoticed -- alembic raises "Multiple
    heads" for ``upgrade head`` in that case, but only if a test actually
    invokes it, which nothing did before this module.
    """
    _run_alembic_upgrade_head(_throwaway_database)
    version = asyncio.run(_fetch_alembic_version(_throwaway_database))
    assert version == _EXPECTED_HEAD_REVISION


@_LIVE_PG
def test_migrated_schema_contains_every_orm_table(_throwaway_database: str) -> None:
    """Every table declared in ``omniscience_core.db.models.Base.metadata``
    must actually exist after ``alembic upgrade head``.

    Catches drift where a model gains a new table but the corresponding
    migration is forgotten (or vice versa) -- currently nothing checks this
    because the rest of the suite provisions schema via
    ``Base.metadata.create_all`` directly from the models, which can never
    disagree with itself.
    """
    _run_alembic_upgrade_head(_throwaway_database)
    live_tables = asyncio.run(_fetch_table_names(_throwaway_database))

    missing = _EXPECTED_ORM_TABLES - live_tables
    assert not missing, f"ORM model tables missing from the migrated schema: {sorted(missing)}"


@_LIVE_PG
def test_migrated_tenant_columns_are_native_uuid_type(_throwaway_database: str) -> None:
    """Tenant-scoping columns must be a real Postgres ``uuid`` column, not
    ``varchar``/``text``.

    This is the structural assertion that would have caught the class of
    bug in migration 0010: a bind parameter typed as a plain string against
    a ``uuid`` column. A ``varchar`` tenant column would silently accept
    non-UUID strings and defeat the equality-based tenant isolation in
    ``omniscience_core.auth.workspace.workspace_filter``.
    """
    _run_alembic_upgrade_head(_throwaway_database)

    for table, column in _EXPECTED_UUID_COLUMNS:
        columns = asyncio.run(_fetch_columns(_throwaway_database, table))
        assert column in columns, f"{table}.{column} does not exist"
        assert "UUID" in columns[column], (
            f"{table}.{column} has type {columns[column]!r}, expected a native UUID column"
        )
