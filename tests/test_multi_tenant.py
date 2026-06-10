"""Tests for multi-tenant workspace isolation — Issue #57.

Coverage:
- Workspace model and Pydantic schemas (WorkspaceCreate, WorkspaceRead)
- get_workspace_id helper — including fail-closed behaviour for legacy tokens
- workspace_filter helper applied to SELECT statements — strict equality only
- ApiToken workspace_id field propagation
- create_api_token with workspace_id argument
- Security: NULL-tenant rows are NOT visible to any workspace token
- Security: legacy token without workspace_id raises PermissionError
- Backfill logic: workspace_filter no longer includes OR-NULL clause
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.auth.tokens import (
    create_api_token,
)
from omniscience_core.auth.workspace import (
    DEFAULT_WORKSPACE_ID,
    get_workspace_id,
    workspace_filter,
)
from omniscience_core.db.models import ApiToken, Workspace
from omniscience_core.db.schemas import (
    ApiTokenCreate,
    ApiTokenRead,
    WorkspaceCreate,
    WorkspaceRead,
)
from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.dialects.postgresql import UUID

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_WS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ALPHA_WS_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_BETA_WS_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")

# SQLAlchemy renders UUID literals without dashes when using literal_binds.
_ALPHA_WS_HEX = _ALPHA_WS_ID.hex  # 'aaaaaaaa000000000000000000000001'
_BETA_WS_HEX = _BETA_WS_ID.hex
_DEFAULT_WS_HEX = _DEFAULT_WS_ID.hex  # '00000000000000000000000000000001'

# Pattern that matches SQL "OR" as a keyword (word boundary), NOT as a
# substring inside column names like "wORkspace_id".
_RE_SQL_OR = re.compile(r"\bOR\b", re.IGNORECASE)

# Pattern that matches IS NULL anywhere in the SQL string.
_RE_IS_NULL = re.compile(r"\bIS\s+NULL\b", re.IGNORECASE)


def _make_token(
    workspace_id: uuid.UUID | None = None,
    scopes: list[str] | None = None,
) -> ApiToken:
    """Build a minimal ApiToken mock."""
    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = uuid.uuid4()
    tok.name = "test-token"
    tok.token_prefix = "sk_dev_x"
    tok.scopes = scopes or ["search"]
    tok.workspace_id = workspace_id
    tok.created_at = datetime.now(tz=UTC)
    tok.expires_at = None
    tok.last_used_at = None
    tok.is_active = True
    tok.hashed_token = "placeholder"
    return tok


def _make_workspace(
    ws_id: uuid.UUID | None = None,
    name: str = "default",
    display_name: str = "Default Workspace",
) -> Workspace:
    """Build a minimal Workspace mock."""
    ws: Workspace = MagicMock(spec=Workspace)
    ws.id = ws_id or _DEFAULT_WS_ID
    ws.name = name
    ws.display_name = display_name
    ws.settings = {}
    ws.created_at = datetime.now(tz=UTC)
    ws.updated_at = datetime.now(tz=UTC)
    return ws


def _make_table_with_cols(*col_names: str) -> Table:
    """Build a bare SQLAlchemy Table with the specified column names."""
    meta = MetaData()
    cols = [Column("id", UUID(as_uuid=True), primary_key=True)]
    for name in col_names:
        cols.append(Column(name, UUID(as_uuid=True), nullable=True))
    return Table("_test_table", meta, *cols)


# ---------------------------------------------------------------------------
# DEFAULT_WORKSPACE_ID constant
# ---------------------------------------------------------------------------


def test_default_workspace_id_matches_seeded_value() -> None:
    """DEFAULT_WORKSPACE_ID must equal the UUID seeded by migration 0003."""
    assert uuid.UUID("00000000-0000-0000-0000-000000000001") == DEFAULT_WORKSPACE_ID


# ---------------------------------------------------------------------------
# WorkspaceCreate schema
# ---------------------------------------------------------------------------


def test_workspace_create_required_fields() -> None:
    """WorkspaceCreate requires name and display_name."""
    schema = WorkspaceCreate(name="alpha", display_name="Alpha Team")
    assert schema.name == "alpha"
    assert schema.display_name == "Alpha Team"


def test_workspace_create_default_settings() -> None:
    """WorkspaceCreate defaults settings to an empty dict."""
    schema = WorkspaceCreate(name="beta", display_name="Beta")
    assert schema.settings == {}


def test_workspace_create_custom_settings() -> None:
    """WorkspaceCreate accepts arbitrary settings dict."""
    schema = WorkspaceCreate(
        name="gamma",
        display_name="Gamma",
        settings={"max_sources": 10, "feature_flags": ["rag_v2"]},
    )
    assert schema.settings["max_sources"] == 10
    assert "rag_v2" in schema.settings["feature_flags"]


def test_workspace_create_name_is_str() -> None:
    """WorkspaceCreate name field is a plain string."""
    schema = WorkspaceCreate(name="my-workspace", display_name="My Workspace")
    assert isinstance(schema.name, str)


# ---------------------------------------------------------------------------
# WorkspaceRead schema
# ---------------------------------------------------------------------------


def test_workspace_read_from_orm() -> None:
    """WorkspaceRead.model_validate correctly maps an ORM-like object."""
    ws = _make_workspace(ws_id=_DEFAULT_WS_ID, name="default", display_name="Default Workspace")
    read = WorkspaceRead.model_validate(ws)
    assert read.id == _DEFAULT_WS_ID
    assert read.name == "default"
    assert read.display_name == "Default Workspace"
    assert isinstance(read.settings, dict)
    assert isinstance(read.created_at, datetime)
    assert isinstance(read.updated_at, datetime)


def test_workspace_read_settings_preserved() -> None:
    """WorkspaceRead preserves the settings dict from the ORM object."""
    ws = _make_workspace()
    ws.settings = {"key": "value", "limit": 50}
    read = WorkspaceRead.model_validate(ws)
    assert read.settings["key"] == "value"
    assert read.settings["limit"] == 50


def test_workspace_read_different_ids() -> None:
    """WorkspaceRead preserves distinct UUIDs for different workspace rows."""
    ws_a = _make_workspace(ws_id=_ALPHA_WS_ID, name="alpha", display_name="Alpha")
    ws_b = _make_workspace(ws_id=_BETA_WS_ID, name="beta", display_name="Beta")
    read_a = WorkspaceRead.model_validate(ws_a)
    read_b = WorkspaceRead.model_validate(ws_b)
    assert read_a.id != read_b.id
    assert read_a.name != read_b.name


# ---------------------------------------------------------------------------
# get_workspace_id — happy paths
# ---------------------------------------------------------------------------


def test_get_workspace_id_returns_uuid_when_set() -> None:
    """get_workspace_id returns the token's workspace_id UUID."""
    token = _make_token(workspace_id=_ALPHA_WS_ID)
    result = get_workspace_id(token)
    assert result == _ALPHA_WS_ID


def test_get_workspace_id_default_workspace() -> None:
    """get_workspace_id returns the default workspace UUID when set."""
    token = _make_token(workspace_id=_DEFAULT_WS_ID)
    result = get_workspace_id(token)
    assert result == _DEFAULT_WS_ID


def test_get_workspace_id_different_workspaces() -> None:
    """get_workspace_id returns distinct UUIDs for tokens in different workspaces."""
    tok_a = _make_token(workspace_id=_ALPHA_WS_ID)
    tok_b = _make_token(workspace_id=_BETA_WS_ID)
    assert get_workspace_id(tok_a) != get_workspace_id(tok_b)


# ---------------------------------------------------------------------------
# get_workspace_id — fail-closed for legacy tokens (workspace_id is None)
# ---------------------------------------------------------------------------


def test_get_workspace_id_raises_for_legacy_token() -> None:
    """get_workspace_id raises PermissionError when workspace_id is None.

    Security invariant: a token without a workspace cannot see any data.
    This replaces the previous behaviour of returning None (which callers
    would treat as 'no restriction').
    """
    token = _make_token(workspace_id=None)
    with pytest.raises(PermissionError, match="forbidden:workspace_required"):
        get_workspace_id(token)


def test_get_workspace_id_legacy_token_never_returns_none() -> None:
    """get_workspace_id never returns None — it always raises instead."""
    token = _make_token(workspace_id=None)
    raised = False
    try:
        result = get_workspace_id(token)
        # If we get here, result must not be None (belt-and-suspenders).
        assert result is not None
    except PermissionError:
        raised = True
    assert raised, "Expected PermissionError for legacy token"


def test_get_workspace_id_error_message_is_descriptive() -> None:
    """PermissionError message contains 'forbidden' and 'workspace_required'."""
    token = _make_token(workspace_id=None)
    with pytest.raises(PermissionError) as exc_info:
        get_workspace_id(token)
    assert "forbidden" in str(exc_info.value)
    assert "workspace_required" in str(exc_info.value)


# ---------------------------------------------------------------------------
# workspace_filter — pass-through for unscoped tables
# ---------------------------------------------------------------------------


def test_workspace_filter_passthrough_for_unscoped_table() -> None:
    """workspace_filter returns unchanged query for tables without scoping columns."""
    tbl = _make_table_with_cols("some_other_col")
    base_query = select(tbl)
    result = workspace_filter(base_query, _ALPHA_WS_ID)
    assert result is base_query


# ---------------------------------------------------------------------------
# workspace_filter — strict equality on workspace_id column
# ---------------------------------------------------------------------------


def test_workspace_filter_adds_clause_for_workspace_id_col() -> None:
    """workspace_filter produces a WHERE clause when the table has workspace_id."""
    tbl = _make_table_with_cols("workspace_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled = str(filtered.compile(compile_kwargs={"literal_binds": False}))
    assert "workspace_id" in compiled
    assert filtered is not base_query


def test_workspace_filter_does_not_include_null_branch_for_workspace_id_col() -> None:
    """Security: workspace_filter must NOT emit OR workspace_id IS NULL.

    Before migration 0010_backfill_null_tenant, the helper used
    ``or_(col == ws, col == null())`` which exposed NULL-tenant rows to every
    workspace token.  After backfill, only strict equality is emitted.

    Note: _RE_SQL_OR uses word boundary (\\bOR\\b) to avoid false positives
    from 'OR' appearing inside column names like 'wORkspace_id'.
    """
    tbl = _make_table_with_cols("workspace_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
    assert not _RE_IS_NULL.search(compiled), "IS NULL must not appear in compiled SQL"
    assert not _RE_SQL_OR.search(compiled), "OR keyword must not appear in compiled SQL"


def test_workspace_filter_different_workspace_ids_produce_different_queries() -> None:
    """Two different workspace IDs yield different compiled WHERE clauses."""
    tbl = _make_table_with_cols("workspace_id")
    base_query = select(tbl)
    q_alpha = workspace_filter(base_query, _ALPHA_WS_ID)
    q_beta = workspace_filter(base_query, _BETA_WS_ID)
    compiled_alpha = str(q_alpha.compile())
    compiled_beta = str(q_beta.compile())
    # Both have the same structure but bind different param values.
    assert compiled_alpha == compiled_beta  # structure is the same
    # Params differ
    alpha_params = q_alpha.compile().params
    beta_params = q_beta.compile().params
    # At least one param value differs between the two queries.
    assert any(
        alpha_params.get(k) != beta_params.get(k) for k in set(alpha_params) | set(beta_params)
    )


# ---------------------------------------------------------------------------
# workspace_filter — strict equality on tenant_id column (legacy sources table)
# ---------------------------------------------------------------------------


def test_workspace_filter_uses_tenant_id_col_as_fallback() -> None:
    """workspace_filter falls back to tenant_id when workspace_id is absent."""
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled = str(filtered.compile(compile_kwargs={"literal_binds": False}))
    assert "tenant_id" in compiled
    assert filtered is not base_query


def test_workspace_filter_does_not_include_null_branch_for_tenant_id_col() -> None:
    """Security: workspace_filter must NOT emit OR tenant_id IS NULL.

    This is the critical cross-tenant vector: prior to migration 0010 a row
    in ``sources`` with tenant_id IS NULL was returned to *every* workspace
    token because of the ``or_(col == ws, col == null())`` clause.  After
    backfill and this fix, only ``tenant_id = :workspace_id`` is emitted.
    """
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
    assert not _RE_IS_NULL.search(compiled), "IS NULL must not appear in compiled SQL"
    assert not _RE_SQL_OR.search(compiled), "OR keyword must not appear in compiled SQL"


def test_workspace_filter_null_tenant_row_not_visible_to_alpha() -> None:
    """Security: a NULL-tenant row does NOT satisfy the alpha workspace filter.

    After backfill, workspace_filter(q, _ALPHA_WS_ID) only matches
    tenant_id = _ALPHA_WS_ID.  A row with tenant_id IS NULL would not match.
    SQLAlchemy renders UUID literal without dashes; we check hex form.
    """
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled_literal = str(filtered.compile(compile_kwargs={"literal_binds": True}))
    # The literal bind must be the ALPHA UUID hex (no dashes in SA render).
    assert _ALPHA_WS_HEX in compiled_literal
    assert not _RE_IS_NULL.search(compiled_literal)


def test_workspace_filter_alpha_and_beta_produce_disjoint_filters_for_tenant_id() -> None:
    """Filters for alpha and beta workspaces are structurally equal but bind-param different."""
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    q_alpha = workspace_filter(base_query, _ALPHA_WS_ID)
    q_beta = workspace_filter(base_query, _BETA_WS_ID)
    alpha_params = q_alpha.compile().params
    beta_params = q_beta.compile().params
    assert any(
        alpha_params.get(k) != beta_params.get(k) for k in set(alpha_params) | set(beta_params)
    )


# ---------------------------------------------------------------------------
# Backfill migration logic: default workspace constant aligns with migration
# ---------------------------------------------------------------------------


def test_backfill_default_workspace_id_constant() -> None:
    """The DEFAULT_WORKSPACE_ID exported from workspace.py matches migration 0003.

    Migration 0003_workspaces seeds ``00000000-0000-0000-0000-000000000001``
    as the default workspace.  Migration 0010_backfill_null_tenant assigns
    this same UUID to all NULL-tenant rows.  This test ensures the constant
    used at runtime matches that seeded value.
    """
    assert uuid.UUID("00000000-0000-0000-0000-000000000001") == DEFAULT_WORKSPACE_ID


def test_backfill_workspace_filter_after_backfill_returns_default_rows() -> None:
    """After backfill, default-workspace token can read ex-NULL rows.

    Before backfill: NULL rows were readable by everyone via OR-NULL clause.
    After backfill: NULL rows become tenant_id=DEFAULT_WS rows; they are
    now readable only by the default workspace token (strict equality match).
    SQLAlchemy renders UUID literals without dashes; we compare using .hex.
    """
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _DEFAULT_WS_ID)
    compiled = str(filtered.compile(compile_kwargs={"literal_binds": True}))
    # Should contain the default workspace UUID hex literal.
    assert _DEFAULT_WS_HEX in compiled
    assert not _RE_IS_NULL.search(compiled)


def test_backfill_workspace_filter_alpha_does_not_match_default_rows() -> None:
    """After backfill, alpha workspace cannot read default-workspace rows.

    The strict equality filter ``tenant_id = :alpha_uuid`` will never match
    rows that now carry ``tenant_id = :default_uuid``.
    SQLAlchemy renders UUID literals without dashes; we compare using .hex.
    """
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    filtered_alpha = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled = str(filtered_alpha.compile(compile_kwargs={"literal_binds": True}))
    # Alpha UUID hex is bound, not the default UUID hex.
    assert _ALPHA_WS_HEX in compiled
    assert _DEFAULT_WS_HEX not in compiled


# ---------------------------------------------------------------------------
# ApiTokenCreate schema — workspace_id field
# ---------------------------------------------------------------------------


def test_api_token_create_accepts_workspace_id() -> None:
    """ApiTokenCreate accepts an optional workspace_id."""
    schema = ApiTokenCreate(
        name="tok",
        hashed_token="h",
        token_prefix="sk",
        scopes=["search"],
        workspace_id=_ALPHA_WS_ID,
    )
    assert schema.workspace_id == _ALPHA_WS_ID


def test_api_token_create_workspace_id_defaults_to_none() -> None:
    """ApiTokenCreate workspace_id defaults to None."""
    schema = ApiTokenCreate(
        name="tok",
        hashed_token="h",
        token_prefix="sk",
        scopes=["search"],
    )
    assert schema.workspace_id is None


# ---------------------------------------------------------------------------
# ApiTokenRead schema — workspace_id field
# ---------------------------------------------------------------------------


def test_api_token_read_includes_workspace_id() -> None:
    """ApiTokenRead exposes workspace_id from ORM objects."""
    token = _make_token(workspace_id=_BETA_WS_ID)
    read = ApiTokenRead.model_validate(token)
    assert read.workspace_id == _BETA_WS_ID


def test_api_token_read_workspace_id_none_for_legacy() -> None:
    """ApiTokenRead returns None workspace_id for legacy tokens (schema preserves raw value)."""
    token = _make_token(workspace_id=None)
    read = ApiTokenRead.model_validate(token)
    assert read.workspace_id is None


# ---------------------------------------------------------------------------
# create_api_token — workspace_id propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_api_token_with_workspace_id() -> None:
    """create_api_token persists workspace_id on the token object."""
    captured: dict[str, Any] = {}

    async def _refresh(obj: Any) -> None:
        obj.id = uuid.uuid4()
        obj.name = "ws-token"
        obj.token_prefix = obj.token_prefix  # already set
        obj.scopes = ["search"]
        obj.workspace_id = obj.workspace_id  # preserve what was set
        obj.created_at = datetime.now(tz=UTC)
        obj.expires_at = None
        obj.last_used_at = None
        obj.is_active = True
        captured["workspace_id"] = obj.workspace_id

    session = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: None)
    session.flush = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh)

    _read, plaintext = await create_api_token(
        session, "ws-token", ["search"], workspace_id=_ALPHA_WS_ID
    )

    assert captured["workspace_id"] == _ALPHA_WS_ID
    assert plaintext.startswith("sk_")
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_create_api_token_without_workspace_id() -> None:
    """create_api_token sets workspace_id to None when not provided."""
    captured: dict[str, Any] = {}

    async def _refresh(obj: Any) -> None:
        obj.id = uuid.uuid4()
        obj.name = "no-ws-token"
        obj.token_prefix = obj.token_prefix
        obj.scopes = ["search"]
        obj.workspace_id = None
        obj.created_at = datetime.now(tz=UTC)
        obj.expires_at = None
        obj.last_used_at = None
        obj.is_active = True
        captured["workspace_id"] = obj.workspace_id

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock(side_effect=_refresh)

    await create_api_token(session, "no-ws-token", ["search"])

    assert captured["workspace_id"] is None


# ---------------------------------------------------------------------------
# Workspace ORM model attribute checks
# ---------------------------------------------------------------------------


def test_workspace_model_has_required_columns() -> None:
    """Workspace ORM model exposes the expected mapped attributes."""
    columns = {col.key for col in Workspace.__table__.columns}
    assert "id" in columns
    assert "name" in columns
    assert "display_name" in columns
    assert "settings" in columns
    assert "created_at" in columns
    assert "updated_at" in columns


def test_workspace_tablename() -> None:
    """Workspace model maps to the 'workspaces' table."""
    assert Workspace.__tablename__ == "workspaces"


def test_api_token_has_workspace_id_column() -> None:
    """ApiToken ORM model has a workspace_id column."""
    columns = {col.key for col in ApiToken.__table__.columns}
    assert "workspace_id" in columns


def test_api_token_workspace_id_is_nullable() -> None:
    """ApiToken.workspace_id column is nullable (for DB-level backward compat).

    Note: the Python layer enforces non-null via get_workspace_id() raising
    PermissionError.  The DB column remains nullable so that migration 0010
    can run as a plain UPDATE without schema changes.
    """
    col = ApiToken.__table__.c["workspace_id"]
    assert col.nullable is True
