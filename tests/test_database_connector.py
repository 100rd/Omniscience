"""Tests for the database connector (Issue #26).

All database interactions are mocked via unittest.mock so no live database
connection is required.  Tests cover:

- DatabaseConfig defaults and custom values
- format_table_schema output (column table, constraints, relationships)
- _format_type helper edge cases
- DatabaseConnector.validate (success, missing secret, inaccessible schema)
- DatabaseConnector.discover (basic, include/exclude filters, view filtering)
- DatabaseConnector.fetch (column rendering, constraint rendering, FK enrichment)
- DatabaseConnector.webhook_handler returns None
- Registry auto-registration
- _should_include logic
- _require_connection_string guard
- _get_column_comments (PG vs non-PG)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from omniscience_connectors import DatabaseConnector, get_connector
from omniscience_connectors.base import DocumentRef
from omniscience_connectors.database.connector import (
    DatabaseConfig,
    _require_connection_string,
    _should_include,
)
from omniscience_connectors.database.formatter import (
    _format_type,
    format_table_schema,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_CONN_STR = "postgresql+psycopg2://user:pass@localhost/testdb"
_SECRETS = {"connection_string": _CONN_STR}


def _make_ref(schema: str = "public", table: str = "users") -> DocumentRef:
    return DocumentRef(
        external_id="abc123",
        uri=f"db://{schema}/{table}",
        metadata={"schema": schema, "table": table, "table_type": "BASE TABLE"},
    )


def _col(
    name: str,
    dtype: str = "integer",
    nullable: str = "NO",
    default: str | None = None,
    char_max: int | None = None,
    num_prec: int | None = None,
    num_scale: int | None = None,
) -> dict[str, object]:
    return {
        "column_name": name,
        "data_type": dtype,
        "udt_name": dtype,
        "is_nullable": nullable,
        "column_default": default,
        "ordinal_position": 1,
        "character_maximum_length": char_max,
        "numeric_precision": num_prec,
        "numeric_scale": num_scale,
    }


# ---------------------------------------------------------------------------
# DatabaseConfig
# ---------------------------------------------------------------------------


class TestDatabaseConfig:
    def test_defaults(self) -> None:
        cfg = DatabaseConfig()
        assert cfg.schemas == ["public"]
        assert cfg.include_tables == []
        assert cfg.exclude_tables == []
        assert cfg.include_views is True

    def test_custom_schemas(self) -> None:
        cfg = DatabaseConfig(schemas=["app", "audit"])
        assert cfg.schemas == ["app", "audit"]

    def test_include_exclude_tables(self) -> None:
        cfg = DatabaseConfig(include_tables=["users"], exclude_tables=["logs"])
        assert "users" in cfg.include_tables
        assert "logs" in cfg.exclude_tables

    def test_no_views(self) -> None:
        cfg = DatabaseConfig(include_views=False)
        assert cfg.include_views is False


# ---------------------------------------------------------------------------
# format_table_schema
# ---------------------------------------------------------------------------


class TestFormatTableSchema:
    def test_heading_contains_table_name(self) -> None:
        md = format_table_schema("public.users", [_col("id")])
        assert "# Table: `public.users`" in md

    def test_columns_header_present(self) -> None:
        md = format_table_schema("public.users", [_col("id")])
        assert "## Columns" in md

    def test_column_appears_in_table(self) -> None:
        md = format_table_schema("public.users", [_col("email", "varchar")])
        assert "`email`" in md
        assert "`varchar`" in md

    def test_nullable_yes(self) -> None:
        md = format_table_schema("t", [_col("bio", nullable="YES")])
        assert "YES" in md

    def test_nullable_no(self) -> None:
        md = format_table_schema("t", [_col("id", nullable="NO")])
        assert "NO" in md

    def test_default_value_shown(self) -> None:
        md = format_table_schema("t", [_col("created_at", default="now()")])
        assert "now()" in md

    def test_long_default_truncated(self) -> None:
        long_default = "x" * 80
        md = format_table_schema("t", [_col("col", default=long_default)])
        assert "..." in md

    def test_primary_key_constraint(self) -> None:
        constraints = [
            {
                "constraint_name": "users_pkey",
                "constraint_type": "PRIMARY KEY",
                "column_name": "id",
                "foreign_table_name": "",
                "foreign_column_name": "",
                "check_clause": "",
            }
        ]
        md = format_table_schema("public.users", [_col("id")], constraints)
        assert "Primary Key" in md
        assert "`id`" in md

    def test_unique_constraint(self) -> None:
        constraints = [
            {
                "constraint_name": "users_email_key",
                "constraint_type": "UNIQUE",
                "column_name": "email",
                "foreign_table_name": "",
                "foreign_column_name": "",
                "check_clause": "",
            }
        ]
        md = format_table_schema("public.users", [_col("email")], constraints)
        assert "Unique" in md

    def test_foreign_key_constraint(self) -> None:
        constraints = [
            {
                "constraint_name": "orders_user_id_fkey",
                "constraint_type": "FOREIGN KEY",
                "column_name": "user_id",
                "foreign_table_name": "users",
                "foreign_column_name": "id",
                "check_clause": "",
            }
        ]
        md = format_table_schema("public.orders", [_col("user_id")], constraints)
        assert "Foreign Key" in md
        assert "users" in md
        assert "Relationships" in md

    def test_check_constraint(self) -> None:
        constraints = [
            {
                "constraint_name": "price_positive",
                "constraint_type": "CHECK",
                "column_name": "",
                "foreign_table_name": "",
                "foreign_column_name": "",
                "check_clause": "price > 0",
            }
        ]
        md = format_table_schema("public.products", [_col("price")], constraints)
        assert "Check" in md
        assert "price > 0" in md

    def test_column_comment_appears(self) -> None:
        comments = {"id": "Primary key, auto-increment"}
        md = format_table_schema("t", [_col("id")], comments=comments)
        assert "Primary key" in md

    def test_no_constraints_section_when_empty(self) -> None:
        md = format_table_schema("t", [_col("id")], constraints=[])
        assert "## Constraints" not in md

    def test_no_relationships_when_no_fks(self) -> None:
        constraints = [
            {
                "constraint_name": "pk",
                "constraint_type": "PRIMARY KEY",
                "column_name": "id",
                "foreign_table_name": "",
                "foreign_column_name": "",
                "check_clause": "",
            }
        ]
        md = format_table_schema("t", [_col("id")], constraints)
        assert "Relationships" not in md

    def test_multiple_columns(self) -> None:
        cols = [_col("id"), _col("name", "varchar"), _col("age", "integer")]
        md = format_table_schema("public.users", cols)
        assert "`id`" in md
        assert "`name`" in md
        assert "`age`" in md


# ---------------------------------------------------------------------------
# _format_type helper
# ---------------------------------------------------------------------------


class TestFormatType:
    def test_simple_type(self) -> None:
        assert _format_type({"data_type": "integer", "udt_name": "integer"}) == "integer"

    def test_varchar_with_length(self) -> None:
        result = _format_type(
            {
                "data_type": "character varying",
                "udt_name": "varchar",
                "character_maximum_length": 255,
                "numeric_precision": None,
                "numeric_scale": None,
            }
        )
        assert "255" in result

    def test_numeric_with_scale(self) -> None:
        result = _format_type(
            {
                "data_type": "numeric",
                "udt_name": "numeric",
                "character_maximum_length": None,
                "numeric_precision": 10,
                "numeric_scale": 2,
            }
        )
        assert "10" in result
        assert "2" in result

    def test_pg_udt_preferred_over_data_type(self) -> None:
        result = _format_type(
            {
                "data_type": "USER-DEFINED",
                "udt_name": "citext",
                "character_maximum_length": None,
                "numeric_precision": None,
                "numeric_scale": None,
            }
        )
        assert "citext" in result


# ---------------------------------------------------------------------------
# _should_include
# ---------------------------------------------------------------------------


class TestShouldInclude:
    def test_no_filters_includes_all(self) -> None:
        assert _should_include("users", [], []) is True

    def test_include_list_allows_match(self) -> None:
        assert _should_include("users", ["users", "orders"], []) is True

    def test_include_list_blocks_non_match(self) -> None:
        assert _should_include("logs", ["users", "orders"], []) is False

    def test_exclude_list_blocks_match(self) -> None:
        assert _should_include("logs", [], ["logs"]) is False

    def test_exclude_does_not_block_other(self) -> None:
        assert _should_include("users", [], ["logs"]) is True

    def test_include_and_exclude_combined(self) -> None:
        # In allowlist but also in denylist → excluded
        assert _should_include("users", ["users"], ["users"]) is False


# ---------------------------------------------------------------------------
# _require_connection_string
# ---------------------------------------------------------------------------


class TestRequireConnectionString:
    def test_returns_value_when_present(self) -> None:
        cs = _require_connection_string({"connection_string": "sqlite://"})
        assert cs == "sqlite://"

    def test_raises_when_missing(self) -> None:
        with pytest.raises(ValueError, match="connection_string"):
            _require_connection_string({})

    def test_raises_when_empty(self) -> None:
        with pytest.raises(ValueError, match="connection_string"):
            _require_connection_string({"connection_string": ""})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_database() -> None:
    connector = get_connector("database")
    assert isinstance(connector, DatabaseConnector)


def test_connector_type_attribute() -> None:
    assert DatabaseConnector.connector_type == "database"


def test_config_schema_attribute() -> None:
    assert DatabaseConnector.config_schema is DatabaseConfig


# ---------------------------------------------------------------------------
# webhook_handler
# ---------------------------------------------------------------------------


def test_webhook_handler_returns_none() -> None:
    connector = DatabaseConnector()
    assert connector.webhook_handler() is None


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestDatabaseConnectorValidate:
    @pytest.mark.asyncio
    async def test_validate_missing_connection_string(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig()
        with pytest.raises(ValueError, match="connection_string"):
            await connector.validate(cfg, {})

    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        """Validate succeeds when the engine connects and the schema is found."""
        connector = DatabaseConnector()
        cfg = DatabaseConfig(schemas=["public"])

        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(return_value="public")

        mock_result = MagicMock()
        mock_result.fetchone.return_value = mock_row

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with (
            patch(
                "omniscience_connectors.database.connector.asyncio.to_thread",
                side_effect=lambda fn, *a, **k: asyncio.get_event_loop().run_in_executor(
                    None, fn, *a, **k
                ),
            ),
            patch("sqlalchemy.create_engine", return_value=mock_engine),
        ):
            await connector.validate(cfg, _SECRETS)

        mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_inaccessible_schema_raises(self) -> None:
        """Validate raises RuntimeError when no configured schema is found."""
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None  # schema not found

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):

            def _run_sync() -> None:
                from omniscience_connectors.database.connector import _verify_schema_access

                with mock_conn:
                    _verify_schema_access(mock_conn, ["missing_schema"], "postgresql")

            with pytest.raises(RuntimeError, match="accessible"):
                await asyncio.get_event_loop().run_in_executor(None, _run_sync)


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


class TestDatabaseConnectorDiscover:
    @pytest.mark.asyncio
    async def test_discover_yields_table_refs(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig(schemas=["public"])

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.__iter__ = MagicMock(
                return_value=iter([("public", "users", "BASE TABLE")])
            )
            return mock_result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            refs = [ref async for ref in connector.discover(cfg, _SECRETS)]

        assert len(refs) == 1
        assert refs[0].uri == "db://public/users"
        assert refs[0].metadata["table"] == "users"
        assert refs[0].metadata["schema"] == "public"

    @pytest.mark.asyncio
    async def test_discover_exclude_table(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig(schemas=["public"], exclude_tables=["migrations"])

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.__iter__ = MagicMock(
                return_value=iter(
                    [
                        ("public", "users", "BASE TABLE"),
                        ("public", "migrations", "BASE TABLE"),
                    ]
                )
            )
            return mock_result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            refs = [ref async for ref in connector.discover(cfg, _SECRETS)]

        tables = [r.metadata["table"] for r in refs]
        assert "users" in tables
        assert "migrations" not in tables

    @pytest.mark.asyncio
    async def test_discover_include_filter(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig(schemas=["public"], include_tables=["orders"])

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.__iter__ = MagicMock(
                return_value=iter(
                    [
                        ("public", "users", "BASE TABLE"),
                        ("public", "orders", "BASE TABLE"),
                    ]
                )
            )
            return mock_result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            refs = [ref async for ref in connector.discover(cfg, _SECRETS)]

        assert len(refs) == 1
        assert refs[0].metadata["table"] == "orders"

    @pytest.mark.asyncio
    async def test_discover_excludes_views_when_disabled(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig(schemas=["public"], include_views=False)

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.__iter__ = MagicMock(
                return_value=iter(
                    [
                        ("public", "users", "BASE TABLE"),
                        ("public", "active_users", "VIEW"),
                    ]
                )
            )
            return mock_result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            refs = [ref async for ref in connector.discover(cfg, _SECRETS)]

        tables = [r.metadata["table"] for r in refs]
        assert "users" in tables
        assert "active_users" not in tables

    @pytest.mark.asyncio
    async def test_discover_includes_views_by_default(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig(schemas=["public"])  # include_views=True by default

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.__iter__ = MagicMock(
                return_value=iter(
                    [
                        ("public", "users", "BASE TABLE"),
                        ("public", "v_active", "VIEW"),
                    ]
                )
            )
            return mock_result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            refs = [ref async for ref in connector.discover(cfg, _SECRETS)]

        tables = [r.metadata["table"] for r in refs]
        assert "v_active" in tables

    @pytest.mark.asyncio
    async def test_discover_missing_connection_string(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig()
        with pytest.raises(ValueError, match="connection_string"):
            async for _ in connector.discover(cfg, {}):
                pass

    @pytest.mark.asyncio
    async def test_discover_external_id_is_stable_sha1(self) -> None:
        """The external_id must be the SHA-1 of schema.table — stable across syncs."""
        import hashlib

        connector = DatabaseConnector()
        cfg = DatabaseConfig(schemas=["public"])

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            mock_result = MagicMock()
            mock_result.__iter__ = MagicMock(
                return_value=iter([("public", "orders", "BASE TABLE")])
            )
            return mock_result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            refs = [ref async for ref in connector.discover(cfg, _SECRETS)]

        expected = hashlib.sha1(b"public.orders").hexdigest()  # noqa: S324
        assert refs[0].external_id == expected


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


class TestDatabaseConnectorFetch:
    def _make_column_rows(self) -> list[tuple[Any, ...]]:
        """Return fake information_schema.columns rows."""
        return [
            ("id", "integer", "NO", None, 1, None, 32, 0),
            ("email", "character varying", "NO", None, 2, 255, None, None),
            ("created_at", "timestamp with time zone", "YES", "now()", 3, None, None, None),
        ]

    @pytest.mark.asyncio
    async def test_fetch_returns_markdown(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig()
        ref = _make_ref("public", "users")

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            sql = str(query)
            result = MagicMock()
            if "information_schema.columns" in sql:
                result.__iter__ = MagicMock(return_value=iter(self._make_column_rows()))
            elif "table_constraints" in sql or "pg_attribute" in sql:
                result.__iter__ = MagicMock(return_value=iter([]))
            else:
                result.__iter__ = MagicMock(return_value=iter([]))
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            doc = await connector.fetch(cfg, _SECRETS, ref)

        assert doc.content_type == "text/markdown"
        content = doc.content_bytes.decode()
        assert "public.users" in content
        assert "id" in content
        assert "email" in content

    @pytest.mark.asyncio
    async def test_fetch_content_type_is_markdown(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig()
        ref = _make_ref()

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            result = MagicMock()
            result.__iter__ = MagicMock(return_value=iter([]))
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "sqlite"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            doc = await connector.fetch(cfg, _SECRETS, ref)

        assert doc.content_type == "text/markdown"

    @pytest.mark.asyncio
    async def test_fetch_missing_connection_string(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig()
        ref = _make_ref()
        with pytest.raises(ValueError, match="connection_string"):
            await connector.fetch(cfg, {}, ref)

    @pytest.mark.asyncio
    async def test_fetch_ref_is_preserved(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig()
        ref = _make_ref("app", "products")

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            result = MagicMock()
            result.__iter__ = MagicMock(return_value=iter([]))
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            doc = await connector.fetch(cfg, _SECRETS, ref)

        assert doc.ref is ref

    @pytest.mark.asyncio
    async def test_fetch_includes_constraint_in_output(self) -> None:
        connector = DatabaseConnector()
        cfg = DatabaseConfig()
        ref = _make_ref("public", "orders")

        def _fake_execute(query: Any, params: Any) -> MagicMock:
            sql = str(query)
            result = MagicMock()
            if "information_schema.columns" in sql:
                result.__iter__ = MagicMock(
                    return_value=iter([("id", "integer", "NO", None, 1, None, None, None)])
                )
            elif "table_constraints" in sql and "constraint_column_usage" not in sql:
                result.__iter__ = MagicMock(
                    return_value=iter([("orders_pkey", "PRIMARY KEY", "id")])
                )
            else:
                result.__iter__ = MagicMock(return_value=iter([]))
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dialect.name = "postgresql"
        mock_engine.dispose = MagicMock()

        with patch("sqlalchemy.create_engine", return_value=mock_engine):
            doc = await connector.fetch(cfg, _SECRETS, ref)

        content = doc.content_bytes.decode()
        assert "Primary Key" in content
