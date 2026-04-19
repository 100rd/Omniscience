"""Tests for multi-tenant workspace isolation — Issue #57.

Coverage:
- Workspace model and Pydantic schemas (WorkspaceCreate, WorkspaceRead)
- get_workspace_id helper
- workspace_filter helper applied to SELECT statements
- ApiToken workspace_id field propagation
- create_api_token with workspace_id argument
- Backward-compat: tokens/queries without workspace_id
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.auth.tokens import (
    create_api_token,
)
from omniscience_core.auth.workspace import get_workspace_id, workspace_filter
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
# get_workspace_id
# ---------------------------------------------------------------------------


def test_get_workspace_id_returns_uuid_when_set() -> None:
    """get_workspace_id returns the token's workspace_id UUID."""
    token = _make_token(workspace_id=_ALPHA_WS_ID)
    result = get_workspace_id(token)
    assert result == _ALPHA_WS_ID


def test_get_workspace_id_returns_none_for_legacy_token() -> None:
    """get_workspace_id returns None when workspace_id is not set."""
    token = _make_token(workspace_id=None)
    result = get_workspace_id(token)
    assert result is None


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
# workspace_filter — pass-through cases
# ---------------------------------------------------------------------------


def _make_table_with_cols(*col_names: str) -> Table:
    """Build a bare SQLAlchemy Table with the specified column names."""
    meta = MetaData()
    cols = [Column("id", UUID(as_uuid=True), primary_key=True)]
    for name in col_names:
        cols.append(Column(name, UUID(as_uuid=True), nullable=True))
    return Table("_test_table", meta, *cols)


def test_workspace_filter_passthrough_when_no_workspace_id() -> None:
    """workspace_filter returns the query unchanged when workspace_id is None."""
    tbl = _make_table_with_cols("workspace_id")
    base_query = select(tbl)
    result = workspace_filter(base_query, None)
    assert result is base_query


def test_workspace_filter_passthrough_for_unscoped_table() -> None:
    """workspace_filter returns unchanged query for tables without scoping columns."""
    tbl = _make_table_with_cols("some_other_col")
    base_query = select(tbl)
    result = workspace_filter(base_query, _ALPHA_WS_ID)
    assert result is base_query


# ---------------------------------------------------------------------------
# workspace_filter — workspace_id column
# ---------------------------------------------------------------------------


def test_workspace_filter_adds_clause_for_workspace_id_col() -> None:
    """workspace_filter produces a WHERE clause when the table has workspace_id."""
    tbl = _make_table_with_cols("workspace_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled = str(filtered.compile(compile_kwargs={"literal_binds": False}))
    assert "workspace_id" in compiled
    assert filtered is not base_query


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
# workspace_filter — tenant_id column (legacy sources table)
# ---------------------------------------------------------------------------


def test_workspace_filter_uses_tenant_id_col_as_fallback() -> None:
    """workspace_filter falls back to tenant_id when workspace_id is absent."""
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    filtered = workspace_filter(base_query, _ALPHA_WS_ID)
    compiled = str(filtered.compile(compile_kwargs={"literal_binds": False}))
    assert "tenant_id" in compiled
    assert filtered is not base_query


def test_workspace_filter_tenant_id_passthrough_when_workspace_id_none() -> None:
    """workspace_filter returns query unchanged for tenant_id tables when workspace is None."""
    tbl = _make_table_with_cols("tenant_id")
    base_query = select(tbl)
    result = workspace_filter(base_query, None)
    assert result is base_query


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
    """ApiTokenRead returns None workspace_id for legacy tokens."""
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
    """ApiToken.workspace_id column is nullable for backward compatibility."""
    col = ApiToken.__table__.c["workspace_id"]
    assert col.nullable is True
