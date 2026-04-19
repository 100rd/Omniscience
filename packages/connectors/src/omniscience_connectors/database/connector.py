"""Database source connector.

Discovers table and view schemas from any SQLAlchemy-compatible relational
database and returns them as formatted Markdown documents.

The connection string is supplied via ``secrets["connection_string"]`` at
runtime so it is never persisted in configuration.

Design notes
------------
* Uses SQLAlchemy Core (sync) executed inside ``asyncio.to_thread`` to remain
  non-blocking in async callers while avoiding the complexity of async DB
  drivers as a hard dependency.
* All schema introspection is done via ``information_schema`` views — portable
  across PostgreSQL, MySQL, CockroachDB, and SQLite (with limitations).
* Each table/view becomes a single :class:`~omniscience_connectors.base.DocumentRef`.
  Its ``external_id`` is ``<schema>.<table>`` (stable across syncs).
* ``fetch`` returns the schema as Markdown via
  :func:`~omniscience_connectors.database.formatter.format_table_schema`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument, WebhookHandler
from omniscience_connectors.database.formatter import format_table_schema

__all__ = ["DatabaseConfig", "DatabaseConnector"]

logger = logging.getLogger(__name__)

# information_schema column query (works on PG, MySQL, SQLite via dialect shims)
_COLUMNS_QUERY = """
SELECT
    table_schema,
    table_name,
    column_name,
    data_type,
    udt_name,
    is_nullable,
    column_default,
    ordinal_position,
    character_maximum_length,
    numeric_precision,
    numeric_scale
FROM information_schema.columns
WHERE table_schema = ANY(:schemas)
  AND table_catalog = current_database()
ORDER BY table_schema, table_name, ordinal_position
"""

# information_schema table/view list query
_TABLES_QUERY = """
SELECT
    table_schema,
    table_name,
    table_type
FROM information_schema.tables
WHERE table_schema = ANY(:schemas)
  AND table_catalog = current_database()
ORDER BY table_schema, table_name
"""

# information_schema constraint query (PG-specific kcu + rc join for FKs)
_CONSTRAINTS_QUERY = """
SELECT
    tc.constraint_name,
    tc.constraint_type,
    tc.table_schema,
    tc.table_name,
    kcu.column_name,
    ccu.table_name  AS foreign_table_name,
    ccu.column_name AS foreign_column_name,
    cc.check_clause
FROM information_schema.table_constraints tc
LEFT JOIN information_schema.key_column_usage kcu
       ON tc.constraint_name = kcu.constraint_name
      AND tc.table_schema    = kcu.table_schema
LEFT JOIN information_schema.constraint_column_usage ccu
       ON tc.constraint_name = ccu.constraint_name
      AND tc.constraint_type = 'FOREIGN KEY'
LEFT JOIN information_schema.check_constraints cc
       ON tc.constraint_name = cc.constraint_name
      AND tc.constraint_type = 'CHECK'
WHERE tc.table_schema = ANY(:schemas)
  AND tc.table_catalog = current_database()
ORDER BY tc.table_schema, tc.table_name, tc.constraint_name
"""

# Simplified fallback queries for non-PostgreSQL databases
_TABLES_QUERY_SIMPLE = """
SELECT
    table_schema,
    table_name,
    table_type
FROM information_schema.tables
WHERE table_schema = :schema
ORDER BY table_schema, table_name
"""

_COLUMNS_QUERY_SIMPLE = """
SELECT
    table_schema,
    table_name,
    column_name,
    data_type,
    column_name AS udt_name,
    is_nullable,
    column_default,
    ordinal_position,
    character_maximum_length,
    numeric_precision,
    numeric_scale
FROM information_schema.columns
WHERE table_schema = :schema
  AND table_name   = :table
ORDER BY ordinal_position
"""


class DatabaseConfig(BaseModel):
    """Public configuration for the database connector (no secrets).

    The actual connection string is provided at runtime via
    ``secrets["connection_string"]``.
    """

    schemas: list[str] = Field(default=["public"])
    """Database schemas to introspect.  Defaults to the ``public`` schema."""

    include_tables: list[str] = Field(default_factory=list)
    """Allowlist of table/view names.  Empty list = include everything."""

    exclude_tables: list[str] = Field(default_factory=list)
    """Denylist of table/view names.  Applied after ``include_tables``."""

    include_views: bool = True
    """Whether to include views in addition to base tables."""


class DatabaseConnector(Connector):
    """Source connector for relational databases.

    Discovers table and view schemas and returns them as Markdown documents
    suitable for downstream ingestion.  No live row data is fetched —
    only DDL-level metadata (columns, types, constraints).

    Polling only — ``webhook_handler`` returns ``None``.
    """

    connector_type: ClassVar[str] = "database"
    config_schema: ClassVar[type[BaseModel]] = DatabaseConfig

    # ------------------------------------------------------------------
    # Public Connector interface
    # ------------------------------------------------------------------

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Test connectivity and verify that at least one target schema exists.

        Raises:
            ValueError: If ``connection_string`` is absent from *secrets*.
            RuntimeError: If the connection fails or no configured schema is
                accessible.
        """
        cfg: DatabaseConfig = config  # type: ignore[assignment]
        connection_string = _require_connection_string(secrets)

        def _check() -> None:
            import sqlalchemy as sa

            engine = sa.create_engine(connection_string, pool_pre_ping=True)
            try:
                with engine.connect() as conn:
                    _verify_schema_access(conn, cfg.schemas, engine.dialect.name)
            finally:
                engine.dispose()

        await asyncio.to_thread(_check)

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield a :class:`DocumentRef` for every matching table/view.

        Each ref's ``external_id`` is ``<schema>.<table>`` — stable across
        syncs as long as the table name does not change.
        """
        cfg: DatabaseConfig = config  # type: ignore[assignment]
        connection_string = _require_connection_string(secrets)

        refs = await asyncio.to_thread(_discover_sync, cfg, connection_string)
        for ref in refs:
            yield ref

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Return the table schema as a Markdown document.

        Args:
            config:  Public connector configuration.
            secrets: Runtime secrets (must include ``connection_string``).
            ref:     A ref previously yielded by :meth:`discover`.

        Returns:
            :class:`FetchedDocument` with ``content_type="text/markdown"`` and
            the schema rendered as a Markdown table.
        """
        connection_string = _require_connection_string(secrets)
        schema_name = str(ref.metadata.get("schema", "public"))
        table_name = str(ref.metadata.get("table", ""))
        fq_name = f"{schema_name}.{table_name}"

        content = await asyncio.to_thread(
            _fetch_schema_sync, connection_string, schema_name, table_name, fq_name
        )
        return FetchedDocument(
            ref=ref,
            content_bytes=content.encode("utf-8"),
            content_type="text/markdown",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        """Databases are polled; no webhook support."""
        return None


# ---------------------------------------------------------------------------
# Synchronous helpers (run inside asyncio.to_thread)
# ---------------------------------------------------------------------------


def _require_connection_string(secrets: dict[str, str]) -> str:
    """Extract and validate the connection string from *secrets*.

    Raises:
        ValueError: If the key is missing or empty.
    """
    cs = secrets.get("connection_string", "")
    if not cs:
        raise ValueError(
            "DatabaseConnector requires 'connection_string' in secrets. "
            "Example: postgresql+psycopg2://user:pass@host/dbname"
        )
    return cs


def _verify_schema_access(
    conn: Any,
    schemas: list[str],
    dialect_name: str,
) -> None:
    """Raise RuntimeError if none of the configured schemas are accessible."""
    import sqlalchemy as sa

    found: list[str] = []
    for schema in schemas:
        try:
            if dialect_name == "postgresql":
                row = conn.execute(
                    sa.text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = :s"
                    ),
                    {"s": schema},
                ).fetchone()
            else:
                row = conn.execute(
                    sa.text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = :s"
                    ),
                    {"s": schema},
                ).fetchone()
            if row is not None:
                found.append(schema)
        except Exception:
            logger.debug("database.validate.schema_check_failed", extra={"schema": schema})

    if not found:
        raise RuntimeError(
            f"None of the configured schemas {schemas!r} are accessible in the database."
        )


def _discover_sync(cfg: DatabaseConfig, connection_string: str) -> list[DocumentRef]:
    """Blocking implementation of discover — called via asyncio.to_thread."""
    import sqlalchemy as sa

    engine = sa.create_engine(connection_string, pool_pre_ping=True)
    refs: list[DocumentRef] = []

    try:
        with engine.connect() as conn:
            dialect = engine.dialect.name
            rows = _list_tables(conn, cfg.schemas, cfg.include_views, dialect)
            for schema, table, table_type in rows:
                if not _should_include(table, cfg.include_tables, cfg.exclude_tables):
                    continue

                fq_name = f"{schema}.{table}"
                # Use a stable SHA-1 fingerprint of the fully-qualified name
                # (sha1 is used only as a compact identifier, not for security)
                external_id = hashlib.sha1(fq_name.encode()).hexdigest()  # noqa: S324

                refs.append(
                    DocumentRef(
                        external_id=external_id,
                        uri=f"db://{schema}/{table}",
                        metadata={
                            "schema": schema,
                            "table": table,
                            "table_type": table_type,
                        },
                    )
                )
    finally:
        engine.dispose()

    return refs


def _list_tables(
    conn: Any,
    schemas: list[str],
    include_views: bool,
    dialect: str,
) -> list[tuple[str, str, str]]:
    """Return ``(schema, table, table_type)`` rows from information_schema."""
    import sqlalchemy as sa

    rows: list[tuple[str, str, str]] = []

    for schema in schemas:
        try:
            result = conn.execute(
                sa.text(
                    "SELECT table_schema, table_name, table_type "
                    "FROM information_schema.tables "
                    "WHERE table_schema = :schema "
                    "ORDER BY table_schema, table_name"
                ),
                {"schema": schema},
            )
            for row in result:
                ttype = str(row[2])
                if ttype == "VIEW" and not include_views:
                    continue
                rows.append((str(row[0]), str(row[1]), ttype))
        except Exception as exc:
            logger.warning(
                "database.discover.list_tables_failed",
                extra={"schema": schema, "error": str(exc)},
            )

    return rows


def _should_include(
    table: str,
    include_tables: list[str],
    exclude_tables: list[str],
) -> bool:
    """Return True if *table* passes the include/exclude filter."""
    if include_tables and table not in include_tables:
        return False
    return not (exclude_tables and table in exclude_tables)


def _fetch_schema_sync(
    connection_string: str,
    schema_name: str,
    table_name: str,
    fq_name: str,
) -> str:
    """Blocking implementation of fetch — called via asyncio.to_thread."""
    import sqlalchemy as sa

    engine = sa.create_engine(connection_string)
    try:
        with engine.connect() as conn:
            columns = _get_columns(conn, schema_name, table_name)
            constraints = _get_constraints(conn, schema_name, table_name, engine.dialect.name)
            comments = _get_column_comments(conn, schema_name, table_name, engine.dialect.name)
    finally:
        engine.dispose()

    return format_table_schema(fq_name, columns, constraints, comments)


def _get_columns(conn: Any, schema: str, table: str) -> list[dict[str, object]]:
    """Fetch column metadata from information_schema.columns."""
    import sqlalchemy as sa

    result = conn.execute(
        sa.text(
            "SELECT "
            "    column_name, "
            "    data_type, "
            "    is_nullable, "
            "    column_default, "
            "    ordinal_position, "
            "    character_maximum_length, "
            "    numeric_precision, "
            "    numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema "
            "  AND table_name   = :table "
            "ORDER BY ordinal_position"
        ),
        {"schema": schema, "table": table},
    )
    columns: list[dict[str, object]] = []
    for row in result:
        columns.append(
            {
                "column_name": row[0],
                "data_type": row[1],
                "is_nullable": row[2],
                "column_default": row[3],
                "ordinal_position": row[4],
                "character_maximum_length": row[5],
                "numeric_precision": row[6],
                "numeric_scale": row[7],
                "udt_name": row[1],  # fallback: use data_type as udt_name
            }
        )
    return columns


def _get_constraints(
    conn: Any,
    schema: str,
    table: str,
    dialect: str,
) -> list[dict[str, object]]:
    """Fetch constraint metadata.

    Uses a simplified query that avoids dialect-specific joins.
    """
    import sqlalchemy as sa

    try:
        result = conn.execute(
            sa.text(
                "SELECT "
                "    tc.constraint_name, "
                "    tc.constraint_type, "
                "    kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "LEFT JOIN information_schema.key_column_usage kcu "
                "       ON tc.constraint_name = kcu.constraint_name "
                "      AND tc.table_schema    = kcu.table_schema "
                "      AND tc.table_name      = kcu.table_name "
                "WHERE tc.table_schema = :schema "
                "  AND tc.table_name   = :table "
                "ORDER BY tc.constraint_name, kcu.ordinal_position"
            ),
            {"schema": schema, "table": table},
        )
        constraints: list[dict[str, object]] = []
        for row in result:
            constraints.append(
                {
                    "constraint_name": row[0],
                    "constraint_type": row[1],
                    "column_name": row[2] or "",
                    "foreign_table_name": "",
                    "foreign_column_name": "",
                    "check_clause": "",
                }
            )

        # Enrich FK constraints with referential details (PG-specific)
        if dialect == "postgresql":
            _enrich_fk_constraints(conn, schema, table, constraints)

        return constraints
    except Exception as exc:
        logger.warning(
            "database.fetch.constraints_failed",
            extra={"schema": schema, "table": table, "error": str(exc)},
        )
        return []


def _enrich_fk_constraints(
    conn: Any,
    schema: str,
    table: str,
    constraints: list[dict[str, object]],
) -> None:
    """Fill in foreign_table_name / foreign_column_name for FK constraints (PG only)."""
    import sqlalchemy as sa

    fk_names = [
        str(c["constraint_name"]) for c in constraints if c.get("constraint_type") == "FOREIGN KEY"
    ]
    if not fk_names:
        return

    try:
        result = conn.execute(
            sa.text(
                "SELECT "
                "    tc.constraint_name, "
                "    ccu.table_name  AS foreign_table_name, "
                "    ccu.column_name AS foreign_column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                " AND tc.table_schema    = ccu.table_schema "
                "WHERE tc.table_schema = :schema "
                "  AND tc.table_name   = :table "
                "  AND tc.constraint_type = 'FOREIGN KEY'"
            ),
            {"schema": schema, "table": table},
        )
        fk_details: dict[str, tuple[str, str]] = {
            str(row[0]): (str(row[1]), str(row[2])) for row in result
        }
        for c in constraints:
            name = str(c.get("constraint_name", ""))
            if c.get("constraint_type") == "FOREIGN KEY" and name in fk_details:
                c["foreign_table_name"] = fk_details[name][0]
                c["foreign_column_name"] = fk_details[name][1]
    except Exception as exc:
        logger.debug(
            "database.fetch.fk_enrich_failed",
            extra={"error": str(exc)},
        )


def _get_column_comments(
    conn: Any,
    schema: str,
    table: str,
    dialect: str,
) -> dict[str, str]:
    """Return a mapping of column_name -> comment text (PostgreSQL only).

    Returns an empty dict for other databases.
    """
    if dialect != "postgresql":
        return {}

    import sqlalchemy as sa

    try:
        result = conn.execute(
            sa.text(
                "SELECT a.attname, pg_catalog.col_description(a.attrelid, a.attnum) "
                "FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema "
                "  AND c.relname = :table "
                "  AND a.attnum > 0 "
                "  AND NOT a.attisdropped "
                "ORDER BY a.attnum"
            ),
            {"schema": schema, "table": table},
        )
        return {str(row[0]): str(row[1] or "") for row in result}
    except Exception as exc:
        logger.debug(
            "database.fetch.column_comments_failed",
            extra={"error": str(exc)},
        )
        return {}
