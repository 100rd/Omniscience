"""Tests for the MCP server module.

Covers:
- search tool returns correct response structure
- get_document tool returns document + chunks
- list_sources tool returns sources with freshness fields
- source_stats tool returns stats + last_ingestion_run
- Auth token extraction from Authorization header (HTTP transport)
- Auth token extraction from OMNISCIENCE_TOKEN env var (stdio transport)
- Scope enforcement: search requires 'search' scope
- Scope enforcement: list_sources/source_stats require 'sources:read' scope
- Error responses for missing/wrong token (unauthorized)
- Error responses for insufficient scope (forbidden)
- Error for unknown document id
- Error for unknown source id
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from omniscience_core.db.models import (
    ApiToken,
    IngestionRun,
    IngestionRunStatus,
    Source,
    SourceStatus,
    SourceType,
)

# Fixture: import tool functions directly for unit testing
from omniscience_server.mcp.tools import (
    mcp_get_document,
    mcp_list_sources,
    mcp_search,
    mcp_source_stats,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 10, 0, 0, tzinfo=UTC)
_SRC_ID = uuid.uuid4()
_DOC_ID = uuid.uuid4()
_CHUNK_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()


def _make_source(
    src_id: uuid.UUID = _SRC_ID,
    name: str = "main-git",
    source_type: SourceType = SourceType.git,
    status: SourceStatus = SourceStatus.active,
    last_sync_at: datetime | None = None,
    freshness_sla_seconds: int | None = 300,
    last_error: str | None = None,
    last_error_at: datetime | None = None,
) -> MagicMock:
    s = MagicMock(spec=Source)
    s.id = src_id
    s.name = name
    s.type = source_type
    s.status = status
    s.last_sync_at = last_sync_at
    s.freshness_sla_seconds = freshness_sla_seconds
    s.last_error = last_error
    s.last_error_at = last_error_at
    return s


def _make_doc(
    doc_id: uuid.UUID = _DOC_ID,
    source_id: uuid.UUID = _SRC_ID,
    uri: str = "https://example.com/file.py",
    title: str | None = "file.py",
    doc_version: int = 1,
    indexed_at: datetime = _NOW,
    tombstoned_at: datetime | None = None,
    doc_metadata: dict[str, Any] | None = None,
) -> MagicMock:
    d = MagicMock()
    d.id = doc_id
    d.source_id = source_id
    d.external_id = "ext-001"
    d.uri = uri
    d.title = title
    d.doc_version = doc_version
    d.indexed_at = indexed_at
    d.tombstoned_at = tombstoned_at
    d.doc_metadata = doc_metadata or {}
    return d


def _make_chunk(
    chunk_id: uuid.UUID = _CHUNK_ID,
    doc_id: uuid.UUID = _DOC_ID,
    text: str = "def foo(): pass",
    chunk_ord: int = 0,
) -> MagicMock:
    c = MagicMock()
    c.id = chunk_id
    c.document_id = doc_id
    c.ord = chunk_ord
    c.text = text
    c.symbol = "foo"
    c.embedding_model = "text-embedding-004"
    c.embedding_provider = "google-ai"
    c.parser_version = "treesitter-python-0.21"
    c.chunker_strategy = "code_symbol"
    c.chunk_metadata = {"language": "python"}
    c.ingestion_run_id = None
    return c


def _make_ingestion_run(
    run_id: uuid.UUID = _RUN_ID,
    source_id: uuid.UUID = _SRC_ID,
) -> MagicMock:
    r = MagicMock(spec=IngestionRun)
    r.id = run_id
    r.source_id = source_id
    r.started_at = _NOW
    r.finished_at = _NOW
    r.status = IngestionRunStatus.ok
    r.docs_new = 10
    r.docs_updated = 2
    r.docs_removed = 1
    r.run_errors = {}
    return r


def _make_app(factory: Any = None, retrieval_service: Any = None) -> FastAPI:
    """Build a minimal FastAPI app stub with mocked state."""
    app = FastAPI()
    app.state.db_session_factory = factory
    app.state.retrieval_service = retrieval_service
    return app


def _make_session_factory(
    get_result: Any = None,
    scalars_results: list[Any] | None = None,
    scalar_one_results: list[Any] | None = None,
    scalar_one_or_none_result: Any = None,
) -> MagicMock:
    """Build an async session factory mock."""
    session = AsyncMock()

    # session.get(Model, id)
    session.get = AsyncMock(return_value=get_result)

    # session.execute() — returns a result object
    execute_results: list[MagicMock] = []
    for row_list in scalars_results or []:
        res = MagicMock()
        res.scalars.return_value.all.return_value = row_list
        execute_results.append(res)
    for scalar_val in scalar_one_results or []:
        res = MagicMock()
        res.scalar_one.return_value = scalar_val
        execute_results.append(res)
    if scalar_one_or_none_result is not None:
        res = MagicMock()
        res.scalar_one_or_none.return_value = scalar_one_or_none_result
        execute_results.append(res)

    session.execute = AsyncMock(side_effect=execute_results if execute_results else None)

    # Context manager
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# ---------------------------------------------------------------------------
# mcp_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_search_returns_hits_structure() -> None:
    """mcp_search returns a dict with 'hits' and 'query_stats' keys."""
    from omniscience_retrieval.models import (
        ChunkLineage,
        Citation,
        QueryStats,
        SearchHit,
        SearchResult,
        SourceInfo,
    )

    hit = SearchHit(
        chunk_id=_CHUNK_ID,
        document_id=_DOC_ID,
        score=0.9,
        text="def foo(): pass",
        source=SourceInfo(id=_SRC_ID, name="main-git", type="git"),
        citation=Citation(uri="https://x.com", title="f.py", indexed_at=_NOW, doc_version=1),
        lineage=ChunkLineage(
            ingestion_run_id=None,
            embedding_model="m",
            embedding_provider="p",
            parser_version="v",
            chunker_strategy="s",
        ),
        metadata={"language": "python"},
    )
    result = SearchResult(
        hits=[hit],
        query_stats=QueryStats(
            total_matches_before_filters=5,
            vector_matches=3,
            text_matches=4,
            duration_ms=12.0,
        ),
    )
    service = AsyncMock()
    service.search = AsyncMock(return_value=result)
    app = _make_app(retrieval_service=service)

    response = await mcp_search(app=app, query="foo function")

    assert "hits" in response
    assert "query_stats" in response
    assert len(response["hits"]) == 1
    assert response["hits"][0]["score"] == 0.9


@pytest.mark.asyncio
async def test_mcp_search_passes_all_params() -> None:
    """mcp_search passes all parameters correctly to RetrievalService.search."""
    from omniscience_retrieval.models import QueryStats, SearchResult

    result = SearchResult(
        hits=[],
        query_stats=QueryStats(
            total_matches_before_filters=0,
            vector_matches=0,
            text_matches=0,
            duration_ms=1.0,
        ),
    )
    service = AsyncMock()
    service.search = AsyncMock(return_value=result)
    app = _make_app(retrieval_service=service)

    await mcp_search(
        app=app,
        query="search term",
        top_k=5,
        sources=["git-main"],
        types=["git"],
        max_age_seconds=3600,
        filters={"language": "python"},
        include_tombstoned=True,
        retrieval_strategy="keyword",
    )

    call_args = service.search.call_args[0][0]
    assert call_args.query == "search term"
    assert call_args.top_k == 5
    assert call_args.sources == ["git-main"]
    assert call_args.types == ["git"]
    assert call_args.max_age_seconds == 3600
    assert call_args.filters == {"language": "python"}
    assert call_args.include_tombstoned is True
    assert call_args.retrieval_strategy == "keyword"


@pytest.mark.asyncio
async def test_mcp_search_raises_when_no_service() -> None:
    """mcp_search raises RuntimeError when retrieval_service is not on app.state."""
    app = _make_app(retrieval_service=None)
    with pytest.raises(RuntimeError, match="retrieval_service not available"):
        await mcp_search(app=app, query="test")


# ---------------------------------------------------------------------------
# mcp_get_document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_get_document_returns_document_and_chunks() -> None:
    """mcp_get_document returns dict with 'document' and 'chunks' keys."""
    doc = _make_doc()
    src = _make_source()
    chunk = _make_chunk()

    session = AsyncMock()
    # First call: get(Document) → doc; second call: get(Source) → src
    session.get = AsyncMock(side_effect=[doc, src])
    # execute → chunks
    chunks_result = MagicMock()
    chunks_result.scalars.return_value.all.return_value = [chunk]
    session.execute = AsyncMock(return_value=chunks_result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    result = await mcp_get_document(app=app, document_id=str(_DOC_ID))

    assert "document" in result
    assert "chunks" in result
    assert result["document"]["id"] == str(_DOC_ID)
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["text"] == "def foo(): pass"


@pytest.mark.asyncio
async def test_mcp_get_document_raises_on_missing_doc() -> None:
    """mcp_get_document raises ValueError with document_not_found when id unknown."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    with pytest.raises(ValueError, match="document_not_found"):
        await mcp_get_document(app=app, document_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_mcp_get_document_invalid_uuid() -> None:
    """mcp_get_document raises ValueError for malformed document_id."""
    app = _make_app(factory=MagicMock())
    with pytest.raises(ValueError):
        await mcp_get_document(app=app, document_id="not-a-uuid")


# ---------------------------------------------------------------------------
# mcp_list_sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_list_sources_returns_sources_key() -> None:
    """mcp_list_sources returns a dict with a 'sources' list."""
    src = _make_source(last_sync_at=_NOW)

    session = AsyncMock()
    sources_result = MagicMock()
    sources_result.scalars.return_value.all.return_value = [src]
    counts_result = MagicMock()
    counts_result.__iter__ = MagicMock(return_value=iter([MagicMock(source_id=_SRC_ID, cnt=42)]))
    session.execute = AsyncMock(side_effect=[sources_result, counts_result])

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    result = await mcp_list_sources(app=app)

    assert "sources" in result
    assert len(result["sources"]) == 1
    s = result["sources"][0]
    assert s["id"] == str(_SRC_ID)
    assert s["name"] == "main-git"
    assert s["type"] == "git"
    assert "is_stale" in s
    assert "indexed_document_count" in s


@pytest.mark.asyncio
async def test_mcp_list_sources_is_stale_when_no_sync() -> None:
    """is_stale is True when freshness_sla_seconds set but last_sync_at is None."""
    src = _make_source(last_sync_at=None, freshness_sla_seconds=300)

    session = AsyncMock()
    sources_result = MagicMock()
    sources_result.scalars.return_value.all.return_value = [src]
    counts_result = MagicMock()
    counts_result.__iter__ = MagicMock(return_value=iter([]))
    session.execute = AsyncMock(side_effect=[sources_result, counts_result])

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    result = await mcp_list_sources(app=app)

    assert result["sources"][0]["is_stale"] is True


@pytest.mark.asyncio
async def test_mcp_list_sources_not_stale_when_recent() -> None:
    """is_stale is False when last_sync_at is within freshness_sla_seconds."""
    from datetime import timedelta

    recent = datetime.now(tz=UTC) - timedelta(seconds=10)
    src = _make_source(last_sync_at=recent, freshness_sla_seconds=300)

    session = AsyncMock()
    sources_result = MagicMock()
    sources_result.scalars.return_value.all.return_value = [src]
    counts_result = MagicMock()
    counts_result.__iter__ = MagicMock(return_value=iter([]))
    session.execute = AsyncMock(side_effect=[sources_result, counts_result])

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    result = await mcp_list_sources(app=app)

    assert result["sources"][0]["is_stale"] is False


# ---------------------------------------------------------------------------
# mcp_source_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_source_stats_returns_full_stats() -> None:
    """mcp_source_stats returns counts, freshness and last_ingestion_run."""
    src = _make_source(last_sync_at=_NOW)
    run = _make_ingestion_run()

    session = AsyncMock()
    session.get = AsyncMock(return_value=src)
    doc_count_result = MagicMock()
    doc_count_result.scalar_one.return_value = 100
    chunk_count_result = MagicMock()
    chunk_count_result.scalar_one.return_value = 1500
    last_run_result = MagicMock()
    last_run_result.scalar_one_or_none.return_value = run
    session.execute = AsyncMock(
        side_effect=[doc_count_result, chunk_count_result, last_run_result]
    )

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    result = await mcp_source_stats(app=app, source_id=str(_SRC_ID))

    assert result["indexed_document_count"] == 100
    assert result["indexed_chunk_count"] == 1500
    assert result["last_ingestion_run"] is not None
    assert result["last_ingestion_run"]["docs_new"] == 10
    assert result["last_ingestion_run"]["status"] == str(IngestionRunStatus.ok)


@pytest.mark.asyncio
async def test_mcp_source_stats_no_run() -> None:
    """mcp_source_stats returns last_ingestion_run=None when no runs exist."""
    src = _make_source()

    session = AsyncMock()
    session.get = AsyncMock(return_value=src)
    doc_count_result = MagicMock()
    doc_count_result.scalar_one.return_value = 0
    chunk_count_result = MagicMock()
    chunk_count_result.scalar_one.return_value = 0
    last_run_result = MagicMock()
    last_run_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(
        side_effect=[doc_count_result, chunk_count_result, last_run_result]
    )

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    result = await mcp_source_stats(app=app, source_id=str(_SRC_ID))

    assert result["last_ingestion_run"] is None


@pytest.mark.asyncio
async def test_mcp_source_stats_raises_on_missing_source() -> None:
    """mcp_source_stats raises ValueError with source_not_found for unknown id."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    app = _make_app(factory=factory)
    with pytest.raises(ValueError, match="source_not_found"):
        await mcp_source_stats(app=app, source_id=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Auth token extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_token_extracts_bearer_from_http_header() -> None:
    """_resolve_token reads bearer token from Authorization header."""
    from omniscience_server.mcp.server import _resolve_token

    token_obj = MagicMock(spec=ApiToken)
    token_obj.scopes = ["search"]

    mock_request = MagicMock()
    mock_request.headers = {"authorization": "Bearer sk_test_abc123"}

    ctx = MagicMock()
    ctx._request_context = MagicMock()
    ctx._request_context.request = mock_request

    with patch(
        "omniscience_server.mcp.server._lookup_token",
        new_callable=AsyncMock,
        return_value=token_obj,
    ):
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        app = _make_app(factory=factory)

        import omniscience_server.mcp.server as server_mod

        server_mod._fastapi_app = app

        result = await _resolve_token(ctx)

    assert result is token_obj


@pytest.mark.asyncio
async def test_resolve_token_uses_env_var_for_non_http() -> None:
    """_resolve_token falls back to OMNISCIENCE_TOKEN env var for non-HTTP requests."""
    import os

    from omniscience_server.mcp.server import _resolve_token

    token_obj = MagicMock(spec=ApiToken)
    token_obj.scopes = ["search"]

    ctx = MagicMock()
    ctx._request_context = MagicMock()
    ctx._request_context.request = None  # not a Starlette Request

    with (
        patch.dict(os.environ, {"OMNISCIENCE_TOKEN": "sk_test_envtoken"}),
        patch(
            "omniscience_server.mcp.server._lookup_token",
            new_callable=AsyncMock,
            return_value=token_obj,
        ),
    ):
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        app = _make_app(factory=factory)

        import omniscience_server.mcp.server as server_mod

        server_mod._fastapi_app = app

        result = await _resolve_token(ctx)

    assert result is token_obj


@pytest.mark.asyncio
async def test_resolve_token_returns_none_when_no_token() -> None:
    """_resolve_token returns None when no token is present."""
    import os

    from omniscience_server.mcp.server import _resolve_token

    ctx = MagicMock()
    ctx._request_context = MagicMock()
    ctx._request_context.request = None

    with patch.dict(os.environ, {}, clear=True):
        # Ensure OMNISCIENCE_TOKEN is not set
        os.environ.pop("OMNISCIENCE_TOKEN", None)

        import omniscience_server.mcp.server as server_mod

        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        app = _make_app(factory=factory)
        server_mod._fastapi_app = app

        result = await _resolve_token(ctx)

    assert result is None


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


def test_require_scope_raises_unauthorized_when_no_token() -> None:
    """_require_scope raises ValueError with 'unauthorized' when token is None."""
    from omniscience_core.auth.scopes import Scope
    from omniscience_server.mcp.server import _require_scope

    with pytest.raises(ValueError, match="unauthorized"):
        _require_scope(None, Scope.search)


def test_require_scope_raises_forbidden_when_wrong_scope() -> None:
    """_require_scope raises ValueError with 'forbidden' for insufficient scope."""
    from omniscience_core.auth.scopes import Scope
    from omniscience_server.mcp.server import _require_scope

    token = MagicMock(spec=ApiToken)
    token.scopes = ["sources:read"]  # has sources:read, not search

    with pytest.raises(ValueError, match="forbidden"):
        _require_scope(token, Scope.search)


def test_require_scope_passes_for_correct_scope() -> None:
    """_require_scope does not raise when token has the required scope."""
    from omniscience_core.auth.scopes import Scope
    from omniscience_server.mcp.server import _require_scope

    token = MagicMock(spec=ApiToken)
    token.scopes = ["search"]

    _require_scope(token, Scope.search)  # should not raise


def test_require_scope_admin_implies_all_scopes() -> None:
    """_require_scope passes for any scope when token has 'admin' scope."""
    from omniscience_core.auth.scopes import Scope
    from omniscience_server.mcp.server import _require_scope

    token = MagicMock(spec=ApiToken)
    token.scopes = ["admin"]

    _require_scope(token, Scope.search)
    _require_scope(token, Scope.sources_read)
    _require_scope(token, Scope.sources_write)


def test_require_scope_sources_read_needed_for_list_sources() -> None:
    """Token with only 'search' scope is forbidden from list_sources."""
    from omniscience_core.auth.scopes import Scope
    from omniscience_server.mcp.server import _require_scope

    token = MagicMock(spec=ApiToken)
    token.scopes = ["search"]

    with pytest.raises(ValueError, match="forbidden"):
        _require_scope(token, Scope.sources_read)
