"""Coverage tests for omniscience_server.mcp.server (issue coverage).

Covers the previously-untested paths in server.py:
- set_fastapi_app / _get_app RuntimeError
- _extract_bearer edge cases
- _resolve_token: None request_context, no factory, empty env var
- _require_scope passes for valid scope
- _parse_as_of: Z suffix, non-UTC offset, naive datetime, empty string, unparseable
- _record_tool_invocation happy path and anonymous token
- Tool handlers (search, get_document, get_related_entities, get_entity,
  list_sources, source_stats, resolve_incident, incident_timeline,
  blast_radius, replay_context_tool, suggest_runbook,
  find_similar_incidents, generate_postmortem_tool) — happy path +
  auth/scope rejections + workspace-scoped-token check + ValueError
  re-raise paths.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from omniscience_core.db.models import ApiToken

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS_ID = uuid.UUID("aaaaaaaa-1111-2222-3333-444400000001")
_NOW = datetime(2026, 4, 17, 10, 0, 0, tzinfo=UTC)


def _make_token(
    scopes: list[str] | None = None,
    workspace_id: uuid.UUID | None = _WS_ID,
) -> MagicMock:
    """Build an ApiToken mock."""
    token: MagicMock = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.scopes = scopes if scopes is not None else ["search"]
    token.workspace_id = workspace_id
    token.token_prefix = "sk_t"
    token.is_active = True
    token.profile_id = "omniscience-mcp-read-v1"
    issued_at = datetime.now(UTC)
    token.created_at = issued_at
    token.expires_at = issued_at + timedelta(days=30)
    return token


def _make_ctx(bearer: str | None = "sk_test") -> Any:
    """Build a minimal fake FastMCP Context."""
    if bearer is not None:
        headers = {"authorization": f"Bearer {bearer}"}
        request = SimpleNamespace(headers=headers)
    else:
        request = None
    request_context = SimpleNamespace(request=request)
    return SimpleNamespace(_request_context=request_context)


def _make_ctx_no_request_context() -> Any:
    """FastMCP Context where _request_context is None (stdio)."""
    return SimpleNamespace(_request_context=None)


def _make_app(factory: Any = None, retrieval_service: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = factory
    app.state.retrieval_service = retrieval_service
    return app


def _mock_factory() -> MagicMock:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _assert_v1_additive_response(
    result: dict[str, Any],
    legacy: dict[str, Any],
) -> None:
    """Assert legacy top-level fields survive and the v1 envelope is additive."""
    for key, value in legacy.items():
        assert result[key] == value
    assert result["meta"]["contract"]["version"] == "1.0.0"
    assert result["meta"]["workspace_id"] == str(_WS_ID)


# ---------------------------------------------------------------------------
# set_fastapi_app / _get_app
# ---------------------------------------------------------------------------


def test_set_fastapi_app_and_get_app() -> None:
    """set_fastapi_app stores the reference; _get_app returns it."""
    import omniscience_server.mcp.server as server_mod

    app = FastAPI()
    server_mod.set_fastapi_app(app)
    assert server_mod._get_app() is app


def test_get_app_raises_when_not_set() -> None:
    """_get_app raises RuntimeError when no app has been configured."""
    import omniscience_server.mcp.server as server_mod

    original = server_mod._fastapi_app
    try:
        server_mod._fastapi_app = None
        with pytest.raises(RuntimeError, match="call set_fastapi_app"):
            server_mod._get_app()
    finally:
        server_mod._fastapi_app = original


# ---------------------------------------------------------------------------
# _extract_bearer
# ---------------------------------------------------------------------------


def test_extract_bearer_none_request() -> None:
    from omniscience_server.mcp.server import _extract_bearer

    assert _extract_bearer(None) is None


def test_extract_bearer_no_headers_attr() -> None:
    from omniscience_server.mcp.server import _extract_bearer

    assert _extract_bearer(SimpleNamespace()) is None


def test_extract_bearer_missing_header() -> None:
    from omniscience_server.mcp.server import _extract_bearer

    req = SimpleNamespace(headers={})
    assert _extract_bearer(req) is None


def test_extract_bearer_non_bearer_scheme() -> None:
    from omniscience_server.mcp.server import _extract_bearer

    req = SimpleNamespace(headers={"authorization": "Basic dXNlcjpwYXNz"})
    assert _extract_bearer(req) is None


def test_extract_bearer_valid() -> None:
    from omniscience_server.mcp.server import _extract_bearer

    req = SimpleNamespace(headers={"authorization": "Bearer sk_abc123"})
    assert _extract_bearer(req) == "sk_abc123"


# ---------------------------------------------------------------------------
# _resolve_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_token_no_request_context() -> None:
    """_resolve_token with None _request_context falls back to env."""
    from omniscience_server.mcp.server import _resolve_token

    token = _make_token()
    ctx = _make_ctx_no_request_context()
    app = _make_app(factory=_mock_factory())

    import omniscience_server.mcp.server as server_mod

    server_mod._fastapi_app = app

    with (
        patch.dict(os.environ, {"OMNISCIENCE_TOKEN": "sk_env_token"}),
        patch(
            "omniscience_server.mcp.server._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
    ):
        result = await _resolve_token(ctx)

    assert result is token


@pytest.mark.asyncio
async def test_resolve_token_no_factory_returns_none() -> None:
    """_resolve_token returns None when app has no db_session_factory."""
    from omniscience_server.mcp.server import _resolve_token

    ctx = _make_ctx("sk_test")
    app = _make_app(factory=None)

    import omniscience_server.mcp.server as server_mod

    server_mod._fastapi_app = app
    result = await _resolve_token(ctx)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_token_empty_env_var_returns_none() -> None:
    """_resolve_token returns None when OMNISCIENCE_TOKEN is empty string."""
    from omniscience_server.mcp.server import _resolve_token

    ctx = _make_ctx_no_request_context()

    import omniscience_server.mcp.server as server_mod

    app = _make_app(factory=_mock_factory())
    server_mod._fastapi_app = app

    with patch.dict(os.environ, {"OMNISCIENCE_TOKEN": ""}):
        result = await _resolve_token(ctx)

    assert result is None


# ---------------------------------------------------------------------------
# _parse_as_of
# ---------------------------------------------------------------------------


def test_parse_as_of_none() -> None:
    from omniscience_server.mcp.server import _parse_as_of

    assert _parse_as_of(None) is None


def test_parse_as_of_empty_string() -> None:
    from omniscience_server.mcp.server import _parse_as_of

    assert _parse_as_of("") is None


def test_parse_as_of_z_suffix() -> None:
    from omniscience_server.mcp.server import _parse_as_of

    result = _parse_as_of("2026-04-12T10:00:00Z")
    assert result is not None
    assert result.tzinfo is not None
    assert result == datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)


def test_parse_as_of_plus_offset() -> None:
    from omniscience_server.mcp.server import _parse_as_of

    result = _parse_as_of("2026-04-12T12:00:00+02:00")
    assert result is not None
    assert result.utcoffset() == timedelta(hours=2)


def test_parse_as_of_naive_raises() -> None:
    from omniscience_server.as_of import INVALID_TIMEZONE_CODE
    from omniscience_server.mcp.server import _parse_as_of

    with pytest.raises(ValueError, match=INVALID_TIMEZONE_CODE):
        _parse_as_of("2026-04-12T10:00:00")


def test_parse_as_of_unparseable_raises() -> None:
    from omniscience_server.as_of import INVALID_TIMEZONE_CODE
    from omniscience_server.mcp.server import _parse_as_of

    with pytest.raises(ValueError, match=INVALID_TIMEZONE_CODE):
        _parse_as_of("not-a-date")


# ---------------------------------------------------------------------------
# _record_tool_invocation
# ---------------------------------------------------------------------------


def test_record_tool_invocation_with_token() -> None:
    """_record_tool_invocation records the invocation against a known token_id."""
    from omniscience_server.mcp.server import _record_tool_invocation

    token = _make_token()
    registry = MagicMock()
    registry.record_tool_invocation = MagicMock()

    with patch("omniscience_server.mcp.server.get_client_registry", return_value=registry):
        _record_tool_invocation(tool_name="search", token=token)

    registry.record_tool_invocation.assert_called_once_with(
        tool_name="search",
        token_id=str(token.id),
    )


def test_record_tool_invocation_anonymous() -> None:
    """_record_tool_invocation uses ANONYMOUS_TOKEN_ID when token is None."""
    from omniscience_core.telemetry.clients import ANONYMOUS_TOKEN_ID
    from omniscience_server.mcp.server import _record_tool_invocation

    registry = MagicMock()
    registry.record_tool_invocation = MagicMock()

    with patch("omniscience_server.mcp.server.get_client_registry", return_value=registry):
        _record_tool_invocation(tool_name="search", token=None)

    registry.record_tool_invocation.assert_called_once_with(
        tool_name="search",
        token_id=ANONYMOUS_TOKEN_ID,
    )


# ---------------------------------------------------------------------------
# Tool: search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tool_unauthorized() -> None:
    """search tool raises ValueError('unauthorized') when token is None."""
    from omniscience_server.mcp.server import search

    tool_fn = getattr(search, "fn", search)
    app = _make_app(factory=_mock_factory())

    with (
        patch("omniscience_server.mcp.server._get_app", return_value=app),
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(ValueError, match="unauthorized"),
    ):
        await tool_fn(query="test", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_search_tool_forbidden_scope() -> None:
    """search tool raises ValueError('forbidden') when token lacks 'search' scope."""
    from omniscience_server.mcp.server import search

    tool_fn = getattr(search, "fn", search)
    token = _make_token(scopes=["sources:read"])

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(query="test", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_search_tool_happy_path() -> None:
    """search tool returns result dict from mcp_search."""
    from omniscience_server.mcp.server import search

    tool_fn = getattr(search, "fn", search)
    token = _make_token()
    expected = {"hits": [], "query_stats": {}}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_search",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(query="hello", ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_search_tool_with_as_of() -> None:
    """search tool passes parsed as_of to mcp_search."""
    from omniscience_server.mcp.server import search

    tool_fn = getattr(search, "fn", search)
    token = _make_token()
    expected = {"hits": [], "query_stats": {}}
    as_of_str = "2026-04-12T10:00:00Z"

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_search",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_search,
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(query="hello", ctx=_make_ctx(), as_of=as_of_str)

    call_kwargs = mock_search.call_args[1]
    assert call_kwargs["as_of"] == datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tool: get_document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_document_tool_happy_path() -> None:
    """get_document tool returns result from mcp_get_document."""
    from omniscience_server.mcp.server import get_document

    tool_fn = getattr(get_document, "fn", get_document)
    token = _make_token()
    expected = {"document": {"id": str(uuid.uuid4())}, "chunks": []}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_document",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(document_id=str(uuid.uuid4()), ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_get_document_tool_not_found_rewrap() -> None:
    """get_document tool rewraps 'document_not_found' as 'source_not_found'."""
    from omniscience_server.mcp.server import get_document

    tool_fn = getattr(get_document, "fn", get_document)
    token = _make_token()
    doc_id = str(uuid.uuid4())

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_document",
            new_callable=AsyncMock,
            side_effect=ValueError(f"document_not_found:{doc_id}"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="source_not_found"),
    ):
        await tool_fn(document_id=doc_id, ctx=_make_ctx())


@pytest.mark.asyncio
async def test_get_document_tool_other_valueerror_reraises() -> None:
    """get_document tool re-raises non-document_not_found ValueErrors."""
    from omniscience_server.mcp.server import get_document

    tool_fn = getattr(get_document, "fn", get_document)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_document",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected_error:something broke"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="unexpected_error"),
    ):
        await tool_fn(document_id=str(uuid.uuid4()), ctx=_make_ctx())


# ---------------------------------------------------------------------------
# Tool: get_related_entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_related_entities_no_workspace_raises() -> None:
    """get_related_entities raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import get_related_entities

    tool_fn = getattr(get_related_entities, "fn", get_related_entities)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(entity_name="svc.api", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_get_related_entities_happy_path() -> None:
    """get_related_entities happy path delegates to mcp_get_related_entities."""
    from omniscience_server.mcp.server import get_related_entities

    tool_fn = getattr(get_related_entities, "fn", get_related_entities)
    token = _make_token()
    expected: dict[str, Any] = {"seed": {}, "related": [], "edges": []}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_related_entities",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(entity_name="svc.api", ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_get_related_entities_entity_not_found_reraises() -> None:
    """get_related_entities re-raises 'entity_not_found' errors."""
    from omniscience_server.mcp.server import get_related_entities

    tool_fn = getattr(get_related_entities, "fn", get_related_entities)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_related_entities",
            new_callable=AsyncMock,
            side_effect=ValueError("entity_not_found:svc.api"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="entity_not_found"),
    ):
        await tool_fn(entity_name="svc.api", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_get_related_entities_other_error_reraises() -> None:
    """get_related_entities re-raises non-entity_not_found ValueError."""
    from omniscience_server.mcp.server import get_related_entities

    tool_fn = getattr(get_related_entities, "fn", get_related_entities)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_related_entities",
            new_callable=AsyncMock,
            side_effect=ValueError("graph_error:timeout"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="graph_error"),
    ):
        await tool_fn(entity_name="svc.api", ctx=_make_ctx())


# ---------------------------------------------------------------------------
# Tool: get_entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_no_workspace_raises() -> None:
    """get_entity raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import get_entity

    tool_fn = getattr(get_entity, "fn", get_entity)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(entity_name="svc.api", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_get_entity_happy_path() -> None:
    """get_entity happy path delegates to mcp_get_entity."""
    from omniscience_server.mcp.server import get_entity

    tool_fn = getattr(get_entity, "fn", get_entity)
    token = _make_token()
    expected: dict[str, Any] = {
        "entity": {"kind": "service"},
        "effective_as_of": _NOW.isoformat(),
    }

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_entity",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(entity_name="svc.api", ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_get_entity_with_as_of() -> None:
    """get_entity with as_of passes parsed datetime to mcp_get_entity."""
    from omniscience_server.mcp.server import get_entity

    tool_fn = getattr(get_entity, "fn", get_entity)
    token = _make_token()
    expected: dict[str, Any] = {
        "entity": None,
        "effective_as_of": "2026-01-01T00:00:00+00:00",
    }

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_get_entity",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_fn,
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(entity_name="svc.api", ctx=_make_ctx(), as_of="2026-01-01T00:00:00Z")

    call_kwargs = mock_fn.call_args[1]
    assert call_kwargs["as_of"] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tool: list_sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sources_unauthorized() -> None:
    """list_sources raises 'unauthorized' when token is None."""
    from omniscience_server.mcp.server import list_sources

    tool_fn = getattr(list_sources, "fn", list_sources)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        pytest.raises(ValueError, match="unauthorized"),
    ):
        await tool_fn(ctx=_make_ctx())


@pytest.mark.asyncio
async def test_list_sources_happy_path() -> None:
    """list_sources happy path delegates to mcp_list_sources."""
    from omniscience_server.mcp.server import list_sources

    tool_fn = getattr(list_sources, "fn", list_sources)
    token = _make_token(scopes=["search"])
    expected: dict[str, Any] = {"sources": []}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_list_sources",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


# ---------------------------------------------------------------------------
# Tool: source_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_stats_happy_path() -> None:
    """source_stats happy path returns from mcp_source_stats."""
    from omniscience_server.mcp.server import source_stats

    tool_fn = getattr(source_stats, "fn", source_stats)
    token = _make_token(scopes=["search"])
    src_id = str(uuid.uuid4())
    expected: dict[str, Any] = {"source_id": src_id, "doc_count": 42}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_source_stats",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(source_id=src_id, ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_source_stats_source_not_found_reraises() -> None:
    """source_stats re-raises 'source_not_found' errors."""
    from omniscience_server.mcp.server import source_stats

    tool_fn = getattr(source_stats, "fn", source_stats)
    token = _make_token(scopes=["search"])
    src_id = str(uuid.uuid4())

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_source_stats",
            new_callable=AsyncMock,
            side_effect=ValueError(f"source_not_found:{src_id}"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="source_not_found"),
    ):
        await tool_fn(source_id=src_id, ctx=_make_ctx())


@pytest.mark.asyncio
async def test_source_stats_other_valueerror_reraises() -> None:
    """source_stats re-raises unexpected ValueErrors."""
    from omniscience_server.mcp.server import source_stats

    tool_fn = getattr(source_stats, "fn", source_stats)
    token = _make_token(scopes=["search"])

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_source_stats",
            new_callable=AsyncMock,
            side_effect=ValueError("db_error:connection refused"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="db_error"),
    ):
        await tool_fn(source_id=str(uuid.uuid4()), ctx=_make_ctx())


# ---------------------------------------------------------------------------
# Tool: resolve_incident
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_incident_no_workspace_raises() -> None:
    """resolve_incident raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import resolve_incident

    tool_fn = getattr(resolve_incident, "fn", resolve_incident)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(alert_id="alert://pagerduty/INC-1", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_resolve_incident_invalid_alert_id_reraises() -> None:
    """resolve_incident re-raises 'invalid_alert_id' errors."""
    from omniscience_server.incidents import INVALID_ALERT_ID_CODE
    from omniscience_server.mcp.server import resolve_incident

    tool_fn = getattr(resolve_incident, "fn", resolve_incident)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_resolve_incident",
            new_callable=AsyncMock,
            side_effect=ValueError(f"{INVALID_ALERT_ID_CODE}:bad id"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=INVALID_ALERT_ID_CODE),
    ):
        await tool_fn(alert_id="not-an-alert-uri", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_resolve_incident_alert_not_found_reraises() -> None:
    """resolve_incident re-raises 'alert_not_found' errors."""
    from omniscience_server.incidents import ALERT_NOT_FOUND_CODE
    from omniscience_server.mcp.server import resolve_incident

    tool_fn = getattr(resolve_incident, "fn", resolve_incident)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_resolve_incident",
            new_callable=AsyncMock,
            side_effect=ValueError(f"{ALERT_NOT_FOUND_CODE}:alert://pd/INC-1"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=ALERT_NOT_FOUND_CODE),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_resolve_incident_other_error_reraises() -> None:
    """resolve_incident re-raises unexpected ValueErrors not matching known codes."""
    from omniscience_server.mcp.server import resolve_incident

    tool_fn = getattr(resolve_incident, "fn", resolve_incident)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_resolve_incident",
            new_callable=AsyncMock,
            side_effect=ValueError("unexpected:something"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="unexpected"),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_resolve_incident_happy_path() -> None:
    """resolve_incident happy path returns wrapped response."""
    from omniscience_server.mcp.server import resolve_incident

    tool_fn = getattr(resolve_incident, "fn", resolve_incident)
    token = _make_token()
    legacy = {"alert_id": "alert://pd/INC-1", "resolution_confidence": 0.9}
    wrapped = {**legacy, "_meta": {}}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_resolve_incident",
            new_callable=AsyncMock,
            return_value=legacy,
        ),
        patch(
            "omniscience_server.mcp.server.wrap_resolve_incident_response",
            return_value=wrapped,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())

    _assert_v1_additive_response(result, wrapped)


@pytest.mark.asyncio
async def test_resolve_incident_depth_clamped_to_max() -> None:
    """resolve_incident clamps max_depth to MAX_MAX_DEPTH."""
    from omniscience_server.incidents import MAX_MAX_DEPTH
    from omniscience_server.mcp.server import resolve_incident

    tool_fn = getattr(resolve_incident, "fn", resolve_incident)
    token = _make_token()
    legacy: dict[str, Any] = {}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_resolve_incident",
            new_callable=AsyncMock,
            return_value=legacy,
        ) as mock_fn,
        patch("omniscience_server.mcp.server.wrap_resolve_incident_response", return_value=legacy),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx(), max_depth=9999)

    call_kwargs = mock_fn.call_args[1]
    assert call_kwargs["max_depth"] == MAX_MAX_DEPTH


@pytest.mark.asyncio
async def test_resolve_incident_depth_clamped_to_min() -> None:
    """resolve_incident clamps max_depth to MIN_MAX_DEPTH."""
    from omniscience_server.incidents import MIN_MAX_DEPTH
    from omniscience_server.mcp.server import resolve_incident

    tool_fn = getattr(resolve_incident, "fn", resolve_incident)
    token = _make_token()
    legacy: dict[str, Any] = {}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_resolve_incident",
            new_callable=AsyncMock,
            return_value=legacy,
        ) as mock_fn,
        patch("omniscience_server.mcp.server.wrap_resolve_incident_response", return_value=legacy),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx(), max_depth=-1)

    call_kwargs = mock_fn.call_args[1]
    assert call_kwargs["max_depth"] == MIN_MAX_DEPTH


# ---------------------------------------------------------------------------
# Tool: blast_radius
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blast_radius_no_workspace_raises() -> None:
    """blast_radius raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(entity_id="svc/api", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_blast_radius_invalid_action_type() -> None:
    """blast_radius raises 'invalid_action_type' for unknown action_type."""
    from omniscience_server.blast_radius import INVALID_ACTION_TYPE_CODE
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=INVALID_ACTION_TYPE_CODE),
    ):
        await tool_fn(entity_id="svc/api", ctx=_make_ctx(), action_type="fly")


@pytest.mark.asyncio
async def test_blast_radius_happy_path() -> None:
    """blast_radius happy path delegates to mcp_blast_radius."""
    from omniscience_server.blast_radius import ACTION_TYPES
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token()
    expected: dict[str, Any] = {"blast_radius": [], "seed_entity_id": "svc/api"}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_blast_radius",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(
            entity_id="svc/api",
            ctx=_make_ctx(),
            action_type=ACTION_TYPES[0],
        )

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_blast_radius_entity_not_found_reraises() -> None:
    """blast_radius re-raises 'entity_not_found' from mcp_blast_radius."""
    from omniscience_server.blast_radius import ACTION_TYPES, ENTITY_NOT_FOUND_CODE
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_blast_radius",
            new_callable=AsyncMock,
            side_effect=ValueError(f"{ENTITY_NOT_FOUND_CODE}:svc/api"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=ENTITY_NOT_FOUND_CODE),
    ):
        await tool_fn(entity_id="svc/api", ctx=_make_ctx(), action_type=ACTION_TYPES[0])


@pytest.mark.asyncio
async def test_blast_radius_invalid_entity_id_reraises() -> None:
    """blast_radius re-raises 'invalid_entity_id' errors from mcp_blast_radius."""
    from omniscience_server.blast_radius import ACTION_TYPES, INVALID_ENTITY_ID_CODE
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_blast_radius",
            new_callable=AsyncMock,
            side_effect=ValueError(f"{INVALID_ENTITY_ID_CODE}:bad-id"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=INVALID_ENTITY_ID_CODE),
    ):
        await tool_fn(entity_id="bad-id", ctx=_make_ctx(), action_type=ACTION_TYPES[0])


@pytest.mark.asyncio
async def test_blast_radius_invalid_action_type_from_underlying_reraises() -> None:
    """blast_radius re-raises 'invalid_action_type' errors from mcp_blast_radius."""
    from omniscience_server.blast_radius import ACTION_TYPES, INVALID_ACTION_TYPE_CODE
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_blast_radius",
            new_callable=AsyncMock,
            side_effect=ValueError(f"{INVALID_ACTION_TYPE_CODE}:bad"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=INVALID_ACTION_TYPE_CODE),
    ):
        await tool_fn(entity_id="svc/api", ctx=_make_ctx(), action_type=ACTION_TYPES[0])


@pytest.mark.asyncio
async def test_blast_radius_other_error_reraises() -> None:
    """blast_radius re-raises unexpected ValueErrors not matching known codes."""
    from omniscience_server.blast_radius import ACTION_TYPES
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_blast_radius",
            new_callable=AsyncMock,
            side_effect=ValueError("graph_timeout:db"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="graph_timeout"),
    ):
        await tool_fn(entity_id="svc/api", ctx=_make_ctx(), action_type=ACTION_TYPES[0])


@pytest.mark.asyncio
async def test_blast_radius_depth_clamped() -> None:
    """blast_radius clamps max_depth to the allowed range."""
    from omniscience_server.blast_radius import (
        ACTION_TYPES,
    )
    from omniscience_server.blast_radius import (
        MAX_MAX_DEPTH as BLAST_MAX_MAX_DEPTH,
    )
    from omniscience_server.mcp.server import blast_radius

    tool_fn = getattr(blast_radius, "fn", blast_radius)
    token = _make_token()
    expected: dict[str, Any] = {}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_blast_radius",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_fn,
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(
            entity_id="svc/api",
            ctx=_make_ctx(),
            action_type=ACTION_TYPES[0],
            max_depth=9999,
        )

    assert mock_fn.call_args[1]["max_depth"] <= BLAST_MAX_MAX_DEPTH


# ---------------------------------------------------------------------------
# Tool: suggest_runbook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_runbook_no_workspace_raises() -> None:
    """suggest_runbook raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import suggest_runbook

    tool_fn = getattr(suggest_runbook, "fn", suggest_runbook)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_suggest_runbook_happy_path() -> None:
    """suggest_runbook happy path delegates to mcp_suggest_runbook."""
    from omniscience_server.mcp.server import suggest_runbook

    tool_fn = getattr(suggest_runbook, "fn", suggest_runbook)
    token = _make_token()
    expected: dict[str, Any] = {"suggestions": []}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_suggest_runbook",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_suggest_runbook_invalid_alert_id_reraises() -> None:
    """suggest_runbook re-raises 'invalid_alert_id' errors."""
    from omniscience_server.mcp.server import suggest_runbook
    from omniscience_server.runbook import INVALID_ALERT_ID_CODE

    tool_fn = getattr(suggest_runbook, "fn", suggest_runbook)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_suggest_runbook",
            new_callable=AsyncMock,
            side_effect=ValueError(f"{INVALID_ALERT_ID_CODE}:bad"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=INVALID_ALERT_ID_CODE),
    ):
        await tool_fn(alert_id="not-valid", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_suggest_runbook_alert_not_found_reraises() -> None:
    """suggest_runbook re-raises 'alert_not_found' errors."""
    from omniscience_server.mcp.server import suggest_runbook
    from omniscience_server.runbook import ALERT_NOT_FOUND_CODE

    tool_fn = getattr(suggest_runbook, "fn", suggest_runbook)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_suggest_runbook",
            new_callable=AsyncMock,
            side_effect=ValueError(f"{ALERT_NOT_FOUND_CODE}:alert://pd/INC-1"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match=ALERT_NOT_FOUND_CODE),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_suggest_runbook_other_error_reraises() -> None:
    """suggest_runbook re-raises unexpected ValueErrors not matching known codes."""
    from omniscience_server.mcp.server import suggest_runbook

    tool_fn = getattr(suggest_runbook, "fn", suggest_runbook)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_suggest_runbook",
            new_callable=AsyncMock,
            side_effect=ValueError("db_error:timeout"),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="db_error"),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_suggest_runbook_top_k_clamped() -> None:
    """suggest_runbook clamps top_k to the valid range."""
    from omniscience_server.mcp.server import suggest_runbook
    from omniscience_server.runbook import MAX_TOP_K

    tool_fn = getattr(suggest_runbook, "fn", suggest_runbook)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_suggest_runbook",
            new_callable=AsyncMock,
            return_value={},
        ) as mock_fn,
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx(), top_k=9999)

    assert mock_fn.call_args[1]["top_k"] <= MAX_TOP_K


# ---------------------------------------------------------------------------
# Tool: find_similar_incidents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_similar_incidents_no_workspace_raises() -> None:
    """find_similar_incidents raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import find_similar_incidents

    tool_fn = getattr(find_similar_incidents, "fn", find_similar_incidents)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(incident_id="alert://pd/INC-1", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_find_similar_incidents_happy_path() -> None:
    """find_similar_incidents happy path delegates to mcp_find_similar_incidents."""
    from omniscience_server.mcp.server import find_similar_incidents

    tool_fn = getattr(find_similar_incidents, "fn", find_similar_incidents)
    token = _make_token()
    expected: dict[str, Any] = {"matches": []}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_find_similar_incidents",
            new_callable=AsyncMock,
            return_value=expected,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(incident_id="alert://pd/INC-1", ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)


@pytest.mark.asyncio
async def test_find_similar_incidents_clamps_params() -> None:
    """find_similar_incidents clamps limit and since_days to valid range."""
    from omniscience_server.mcp.server import find_similar_incidents
    from omniscience_server.similar_incidents import MAX_LIMIT, MAX_SINCE_DAYS

    tool_fn = getattr(find_similar_incidents, "fn", find_similar_incidents)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_find_similar_incidents",
            new_callable=AsyncMock,
            return_value={},
        ) as mock_fn,
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(
            incident_id="alert://pd/INC-1",
            ctx=_make_ctx(),
            limit=99999,
            since_days=99999,
        )

    call_kwargs = mock_fn.call_args[1]
    assert call_kwargs["limit"] <= MAX_LIMIT
    assert call_kwargs["since_days"] <= MAX_SINCE_DAYS


# ---------------------------------------------------------------------------
# Tool: replay_context_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_context_no_workspace_raises() -> None:
    """replay_context_tool raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import replay_context_tool

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(ctx=_make_ctx(), audit_log_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_replay_context_both_supplied_raises() -> None:
    """replay_context_tool raises 'bad_request' when both audit_log_id and inline supplied."""
    from omniscience_server.mcp.server import replay_context_tool

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "omniscience_server.mcp.server._get_app",
            return_value=_make_app(factory=_mock_factory()),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="bad_request"),
    ):
        await tool_fn(
            ctx=_make_ctx(),
            audit_log_id=str(uuid.uuid4()),
            at_time="2026-01-01T00:00:00Z",
        )


@pytest.mark.asyncio
async def test_replay_context_neither_supplied_raises() -> None:
    """replay_context_tool raises 'bad_request' when neither mode supplied."""
    from omniscience_server.mcp.server import replay_context_tool

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "omniscience_server.mcp.server._get_app",
            return_value=_make_app(factory=_mock_factory()),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="bad_request"),
    ):
        await tool_fn(ctx=_make_ctx())


@pytest.mark.asyncio
async def test_replay_context_bad_uuid_raises() -> None:
    """replay_context_tool raises 'bad_request' for non-UUID audit_log_id."""
    from omniscience_server.mcp.server import replay_context_tool

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "omniscience_server.mcp.server._get_app",
            return_value=_make_app(factory=_mock_factory()),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="bad_request"),
    ):
        await tool_fn(ctx=_make_ctx(), audit_log_id="not-a-uuid")


@pytest.mark.asyncio
async def test_replay_context_no_factory_raises_runtime_error() -> None:
    """replay_context_tool raises RuntimeError when db_session_factory is missing."""
    from omniscience_server.mcp.server import replay_context_tool

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token()
    app = _make_app(factory=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=app),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(RuntimeError, match="db_session_factory not available"),
    ):
        await tool_fn(ctx=_make_ctx(), at_time="2026-01-01T00:00:00Z", tool_name="search")


@pytest.mark.asyncio
async def test_replay_context_audit_id_happy_path() -> None:
    """replay_context_tool with audit_log_id calls replay_by_audit_id and returns dict."""
    from omniscience_server.mcp.server import replay_context_tool
    from omniscience_server.replay import ReplayResult

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token()
    audit_id = uuid.uuid4()
    result_obj = ReplayResult(
        response={"hits": []},
        state_fingerprint="abc123",
        fingerprint_algorithm="sha256",
        original_state_fingerprint=None,
        fingerprint_match=None,
        at_time=_NOW,
        tool_name="search",
        audit_log_id=audit_id,
    )

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "omniscience_server.mcp.server._get_app",
            return_value=_make_app(factory=_mock_factory()),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        patch(
            "omniscience_server.mcp.server.replay_by_audit_id",
            new_callable=AsyncMock,
            return_value=result_obj,
        ),
        patch(
            "omniscience_server.mcp.server.envelope_to_dict",
            return_value={"state_fingerprint": "abc123"},
        ),
    ):
        result = await tool_fn(ctx=_make_ctx(), audit_log_id=str(audit_id))

    assert "state_fingerprint" in result


@pytest.mark.asyncio
async def test_replay_context_inline_happy_path() -> None:
    """replay_context_tool with inline at_time+tool_name calls replay_context."""
    from omniscience_server.mcp.server import replay_context_tool
    from omniscience_server.replay import ReplayResult

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token()
    result_obj = ReplayResult(
        response={"hits": []},
        state_fingerprint="def456",
        fingerprint_algorithm="sha256",
        original_state_fingerprint=None,
        fingerprint_match=None,
        at_time=_NOW,
        tool_name="get_entity",
        audit_log_id=None,
    )

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "omniscience_server.mcp.server._get_app",
            return_value=_make_app(factory=_mock_factory()),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        patch(
            "omniscience_server.mcp.server.replay_context",
            new_callable=AsyncMock,
            return_value=result_obj,
        ),
        patch(
            "omniscience_server.mcp.server.envelope_to_dict",
            return_value={"state_fingerprint": "def456"},
        ),
    ):
        result = await tool_fn(
            ctx=_make_ctx(),
            at_time="2026-01-01T00:00:00Z",
            tool_name="get_entity",
            arguments={"entity_name": "svc.api"},
        )

    assert "state_fingerprint" in result


@pytest.mark.asyncio
async def test_replay_context_audit_log_not_found() -> None:
    """replay_context_tool wraps AuditLogNotFoundError as 'audit_log_not_found'."""
    from omniscience_server.mcp.server import replay_context_tool
    from omniscience_server.replay import AuditLogNotFoundError

    tool_fn = getattr(replay_context_tool, "fn", replay_context_tool)
    token = _make_token()
    audit_id = uuid.uuid4()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "omniscience_server.mcp.server._get_app",
            return_value=_make_app(factory=_mock_factory()),
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        patch(
            "omniscience_server.mcp.server.replay_by_audit_id",
            new_callable=AsyncMock,
            side_effect=AuditLogNotFoundError(str(audit_id)),
        ),
        pytest.raises(ValueError, match="audit_log_not_found"),
    ):
        await tool_fn(ctx=_make_ctx(), audit_log_id=str(audit_id))


# ---------------------------------------------------------------------------
# Tool: generate_postmortem_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_postmortem_no_workspace_raises() -> None:
    """generate_postmortem_tool raises 'forbidden' for unscoped token."""
    from omniscience_server.mcp.server import generate_postmortem_tool

    tool_fn = getattr(generate_postmortem_tool, "fn", generate_postmortem_tool)
    token = _make_token(workspace_id=None)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(
            incident_id="INC-1",
            alert_id="alert://pd/INC-1",
            ctx=_make_ctx(),
        )


@pytest.mark.asyncio
async def test_generate_postmortem_happy_path() -> None:
    """generate_postmortem_tool happy path returns rendered report dict."""
    from omniscience_server.mcp.server import generate_postmortem_tool
    from omniscience_server.postmortem import PostmortemReport

    tool_fn = getattr(generate_postmortem_tool, "fn", generate_postmortem_tool)
    token = _make_token()

    mock_report = MagicMock(spec=PostmortemReport)
    mock_report.incident_id = "INC-1"
    mock_report.alert_id = "alert://pd/INC-1"
    mock_report.template_id = "blameless"
    mock_report.effective_as_of = _NOW
    mock_report.model_dump.return_value = {"incident_id": "INC-1"}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        patch(
            "omniscience_server.mcp.server.generate_postmortem",
            new_callable=AsyncMock,
            return_value=mock_report,
        ),
        patch(
            "omniscience_server.mcp.server.render",
            return_value="# Post-mortem",
        ),
    ):
        result = await tool_fn(
            incident_id="INC-1",
            alert_id="alert://pd/INC-1",
            ctx=_make_ctx(),
        )

    assert result["incident_id"] == "INC-1"
    assert result["rendered"] == "# Post-mortem"
    assert result["template"] == "blameless"


@pytest.mark.asyncio
async def test_generate_postmortem_postmortem_error_rewraps() -> None:
    """generate_postmortem_tool wraps PostmortemError as ValueError."""
    from omniscience_server.mcp.server import generate_postmortem_tool
    from omniscience_server.postmortem import PostmortemError

    tool_fn = getattr(generate_postmortem_tool, "fn", generate_postmortem_tool)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        patch(
            "omniscience_server.mcp.server.generate_postmortem",
            new_callable=AsyncMock,
            side_effect=PostmortemError("alert_not_found", "alert://pd/INC-1"),
        ),
        pytest.raises(ValueError, match="alert_not_found"),
    ):
        await tool_fn(
            incident_id="INC-1",
            alert_id="alert://pd/INC-1",
            ctx=_make_ctx(),
        )


# ---------------------------------------------------------------------------
# Tool: incident_timeline (thin shim in server.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_shim_finalizes_v1_response() -> None:
    """The incident_timeline shim authorizes once and adds the v1 envelope."""
    from omniscience_server.mcp.server import incident_timeline

    tool_fn = getattr(incident_timeline, "fn", incident_timeline)
    token = _make_token()
    expected: dict[str, Any] = {"events": []}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "omniscience_server.mcp.server.mcp_incident_timeline",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_tool,
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(alert_id="alert://pd/INC-1", ctx=_make_ctx())

    _assert_v1_additive_response(result, expected)
    mock_tool.assert_awaited_once()
