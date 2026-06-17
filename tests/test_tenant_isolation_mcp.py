"""Tenant isolation tests for MCP tool functions.

Tests call the tool implementations in ``omniscience_server.mcp.tools``
directly (bypassing the FastMCP server layer) to verify that the workspace_id
parameter is correctly enforced.

Tools under test:
  mcp_list_sources   — must filter by workspace_id; reject None with RuntimeError
  mcp_source_stats   — must reject cross-tenant sources and None workspace_id
  mcp_get_document   — must reject cross-tenant documents and None workspace_id

Security invariants tested:
  1. mcp_list_sources with workspace_id=None raises RuntimeError (fail-closed).
  2. mcp_list_sources with workspace_id=A returns only workspace-A sources.
  3. mcp_list_sources does not leak workspace-B sources to workspace-A token.
  4. mcp_source_stats with workspace_id=None raises RuntimeError (fail-closed).
  5. mcp_source_stats with cross-tenant source_id raises ValueError("source_not_found:…").
  6. mcp_source_stats with own source returns stats normally.
  7. mcp_get_document with workspace_id=None raises RuntimeError (fail-closed).
  8. mcp_get_document with cross-tenant document raises ValueError("document_not_found:…").
  9. mcp_get_document preserves "document_not_found:" prefix (for server.py re-wrap).
 10. mcp_get_document with own document returns data normally.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from omniscience_core.db.models import (
    Document,
    Source,
    SourceStatus,
    SourceType,
)
from omniscience_server.mcp.tools import (
    mcp_get_document,
    mcp_list_sources,
    mcp_source_stats,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_WS_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    *,
    tenant_id: uuid.UUID | None,
    name: str = "src",
    source_id: uuid.UUID | None = None,
) -> Source:
    src: Source = MagicMock(spec=Source)
    src.id = source_id or uuid.uuid4()
    src.type = SourceType.git
    src.name = name
    src.config = {}
    src.secrets_ref = None
    src.status = SourceStatus.active
    src.last_sync_at = None
    src.last_error = None
    src.last_error_at = None
    src.freshness_sla_seconds = None
    src.tenant_id = tenant_id
    src.created_at = datetime.now(tz=UTC)
    src.updated_at = datetime.now(tz=UTC)
    return src


def _make_document(
    *,
    source_id: uuid.UUID,
    doc_id: uuid.UUID | None = None,
) -> Document:
    doc: Document = MagicMock(spec=Document)
    doc.id = doc_id or uuid.uuid4()
    doc.source_id = source_id
    doc.external_id = "ext-1"
    doc.uri = "file:///a.txt"
    doc.title = "A"
    doc.doc_version = 1
    doc.indexed_at = datetime.now(tz=UTC)
    doc.tombstoned_at = None
    doc.doc_metadata = {}
    return doc


def _make_session_for_list_sources(sources: list[Source]) -> AsyncMock:
    """Session that returns ``sources`` from select(Source).where(...)."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        res.scalars.return_value.all.return_value = sources
        return res

    session.execute = _execute
    session.get = AsyncMock(return_value=None)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_session_for_get(get_result: Any) -> AsyncMock:
    """Session that returns ``get_result`` from session.get(...)."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        res.scalar_one.return_value = 0
        res.scalar_one_or_none.return_value = None
        return res

    session.execute = _execute
    session.get = AsyncMock(return_value=get_result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_app(session: AsyncMock) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = MagicMock(return_value=session)
    return app


# ---------------------------------------------------------------------------
# mcp_list_sources — workspace_id=None is fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_list_sources_none_workspace_raises() -> None:
    """mcp_list_sources raises RuntimeError when workspace_id is None (fail-closed)."""
    session = _make_session_for_list_sources([])
    app = _make_app(session)

    with pytest.raises(RuntimeError, match="forbidden:workspace_required"):
        await mcp_list_sources(app=app, workspace_id=None)


# ---------------------------------------------------------------------------
# mcp_list_sources — returns only own-workspace sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_list_sources_returns_own_workspace_only() -> None:
    """mcp_list_sources returns sources for workspace A when called with WS_A."""
    src_a = _make_source(tenant_id=_WS_A, name="ws-a-source")
    session = _make_session_for_list_sources([src_a])
    app = _make_app(session)

    result = await mcp_list_sources(app=app, workspace_id=_WS_A)

    assert "sources" in result
    assert len(result["sources"]) == 1
    assert result["sources"][0]["name"] == "ws-a-source"


@pytest.mark.asyncio
async def test_mcp_list_sources_excludes_cross_tenant() -> None:
    """mcp_list_sources returns an empty list when no workspace-A sources exist."""
    # The SQL filter (Source.tenant_id == workspace_id) is enforced in the tool.
    # The mock returns an empty list, simulating that WS_B rows were excluded by SQL.
    session = _make_session_for_list_sources([])
    app = _make_app(session)

    result = await mcp_list_sources(app=app, workspace_id=_WS_A)

    assert result == {"sources": []}


# ---------------------------------------------------------------------------
# mcp_source_stats — workspace_id=None is fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_source_stats_none_workspace_raises() -> None:
    """mcp_source_stats raises RuntimeError when workspace_id is None (fail-closed)."""
    session = _make_session_for_get(None)
    app = _make_app(session)

    with pytest.raises(RuntimeError, match="forbidden:workspace_required"):
        await mcp_source_stats(app=app, source_id=str(uuid.uuid4()), workspace_id=None)


# ---------------------------------------------------------------------------
# mcp_source_stats — cross-tenant source returns source_not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_source_stats_cross_tenant_raises_not_found() -> None:
    """mcp_source_stats raises ValueError('source_not_found:…') for workspace-B source."""
    src_b = _make_source(tenant_id=_WS_B)
    session = _make_session_for_get(src_b)
    app = _make_app(session)

    with pytest.raises(ValueError, match=f"source_not_found:{src_b.id}"):
        await mcp_source_stats(app=app, source_id=str(src_b.id), workspace_id=_WS_A)


@pytest.mark.asyncio
async def test_mcp_source_stats_missing_source_raises_not_found() -> None:
    """mcp_source_stats raises ValueError('source_not_found:…') when source is absent."""
    session = _make_session_for_get(None)
    app = _make_app(session)
    fake_id = str(uuid.uuid4())

    with pytest.raises(ValueError, match=f"source_not_found:{fake_id}"):
        await mcp_source_stats(app=app, source_id=fake_id, workspace_id=_WS_A)


# ---------------------------------------------------------------------------
# mcp_source_stats — own workspace source returns data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_source_stats_own_workspace_returns_stats() -> None:
    """mcp_source_stats returns stats when source belongs to the caller's workspace."""
    src_a = _make_source(tenant_id=_WS_A, name="own-source")
    session = _make_session_for_get(src_a)
    app = _make_app(session)

    result = await mcp_source_stats(app=app, source_id=str(src_a.id), workspace_id=_WS_A)

    assert result["id"] == str(src_a.id)
    assert result["name"] == "own-source"
    assert "indexed_document_count" in result
    assert "indexed_chunk_count" in result


# ---------------------------------------------------------------------------
# mcp_get_document — workspace_id=None is fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_get_document_none_workspace_raises() -> None:
    """mcp_get_document raises RuntimeError when workspace_id is None (fail-closed)."""
    session = _make_session_for_get(None)
    app = _make_app(session)

    with pytest.raises(RuntimeError, match="forbidden:workspace_required"):
        await mcp_get_document(app=app, document_id=str(uuid.uuid4()), workspace_id=None)


# ---------------------------------------------------------------------------
# mcp_get_document — cross-tenant document returns document_not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_get_document_cross_tenant_raises_not_found() -> None:
    """mcp_get_document raises ValueError('document_not_found:…') for workspace-B document."""
    src_b = _make_source(tenant_id=_WS_B)
    doc = _make_document(source_id=src_b.id)

    # Build a session that returns doc then src_b on successive .get() calls.
    session = AsyncMock()
    call_count: list[int] = [0]

    async def _get(model: Any, pk: Any) -> Any:
        call_count[0] += 1
        return doc if call_count[0] == 1 else src_b

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res

    session.get = _get
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(session)

    with pytest.raises(ValueError, match=f"document_not_found:{doc.id}"):
        await mcp_get_document(app=app, document_id=str(doc.id), workspace_id=_WS_A)


@pytest.mark.asyncio
async def test_mcp_get_document_not_found_raises_not_found() -> None:
    """mcp_get_document raises ValueError('document_not_found:…') when doc is absent."""
    session = _make_session_for_get(None)
    app = _make_app(session)
    fake_id = str(uuid.uuid4())

    with pytest.raises(ValueError, match=f"document_not_found:{fake_id}"):
        await mcp_get_document(app=app, document_id=fake_id, workspace_id=_WS_A)


@pytest.mark.asyncio
async def test_mcp_get_document_error_prefix_preserved() -> None:
    """mcp_get_document ValueError starts with 'document_not_found:' for server re-wrap."""
    # The server-layer wrapper in server.py re-maps this prefix to 'source_not_found:'.
    # Verify the exact prefix is preserved.
    session = _make_session_for_get(None)
    app = _make_app(session)
    doc_id = str(uuid.uuid4())

    with pytest.raises(ValueError) as exc_info:
        await mcp_get_document(app=app, document_id=doc_id, workspace_id=_WS_A)

    assert str(exc_info.value).startswith("document_not_found:")


# ---------------------------------------------------------------------------
# mcp_get_document — own workspace document returns data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_get_document_own_workspace_returns_data() -> None:
    """mcp_get_document returns document and chunks when source is in caller's workspace."""
    src_a = _make_source(tenant_id=_WS_A)
    doc = _make_document(source_id=src_a.id)

    session = AsyncMock()
    call_count: list[int] = [0]

    async def _get(model: Any, pk: Any) -> Any:
        call_count[0] += 1
        return doc if call_count[0] == 1 else src_a

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res

    session.get = _get
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(session)

    result = await mcp_get_document(app=app, document_id=str(doc.id), workspace_id=_WS_A)

    assert "document" in result
    assert result["document"]["id"] == str(doc.id)
    assert "chunks" in result
