"""Tests for the ``list_entities`` MCP tool (finishes Gap B).

Mirrors the ``get_entity`` MCP coverage tests (``test_mcp_server_coverage.py``)
and the ``as_of`` contract tests (``test_mcp_as_of.py``):

1. Decorated tool auth wrapper (``omniscience_server.mcp.server.list_entities``):
   - scope 'search' required (unscoped token -> 'unauthorized'),
   - workspace-scoped token required (no-workspace token -> 'forbidden'),
   - happy-path delegation to ``mcp_list_entities`` with the contract kwargs,
   - ``as_of`` string parsed to a UTC datetime and forwarded,
   - cluster/name forwarded verbatim.
2. The ``mcp_list_entities`` handler against an in-memory ``GraphStore`` fake:
   - per-entity shape matches ``get_entity`` (``_entity_view_to_dict``),
   - ``effective_as_of`` echo-back of the supplied ``as_of``,
   - cross-workspace isolation (ACL invariant — the store call carries only
     the caller's workspace; B's entities never leak into A's result),
   - empty pre-history result carries ``meta.degraded_response``.
3. FastMCP registry exposes ``list_entities`` with the contract input schema
   (``kind``, ``cluster``, ``name``, ``as_of``).

The store layer (``GraphStore.list_entities``) and its Neo4j adapter have
their own store-free unit tests (``tests/test_neo4j_store_list_entities.py``,
committed in f8240b7); these tests cover the MCP surface on top of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from omniscience_core.db.models import ApiToken
from omniscience_core.storage import EntityNodeView
from omniscience_server.as_of import DEGRADED_PRE_HISTORY
from omniscience_server.mcp.server import list_entities, mcp_server
from omniscience_server.mcp.tools import mcp_list_entities

# ---------------------------------------------------------------------------
# Constants — two workspaces, a small storage-class timeline
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-1111-2222-3333-4444aaaa0001")
_WS_B = uuid.UUID("bbbbbbbb-1111-2222-3333-4444bbbb0001")

_T1 = datetime(2026, 5, 1, 8, 0, 0, tzinfo=UTC)
_PRE_HISTORY = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory GraphStore fake — honours (workspace_id, kind, cluster, name)
# ---------------------------------------------------------------------------


class _ListEntitiesStore:
    """Minimal ``GraphStore`` fake implementing ``list_entities``.

    Holds ``(workspace_id, kind)`` -> list[EntityNodeView] with a ``cluster``
    captured per node via its ``source`` (encoded as ``cluster:<name>`` for
    test recognisability).  Honours the optional ``cluster``/``name`` filters
    exactly as the protocol contract requires, and records every call so
    ACL-isolation tests can assert the workspace predicate never widens.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[uuid.UUID, str], list[tuple[str, EntityNodeView]]] = {}
        self.calls: list[dict[str, Any]] = []

    def plant(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str,
        cluster: str,
        name: str,
    ) -> None:
        node = EntityNodeView(
            id=uuid.uuid4(),
            name=name,
            kind=kind,
            source=f"{cluster}:{name}",
            chunk_text=f"{kind} {name} in {cluster}",
            valid_from=_T1,
            valid_to=None,
            recorded_at=_T1,
        )
        self._rows.setdefault((workspace_id, kind), []).append((cluster, node))

    async def list_entities(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str,
        cluster: str | None = None,
        name: str | None = None,
        as_of: datetime | None = None,
    ) -> list[EntityNodeView]:
        self.calls.append(
            {
                "op": "list_entities",
                "workspace_id": workspace_id,
                "kind": kind,
                "cluster": cluster,
                "name": name,
                "as_of": as_of,
            }
        )
        rows = self._rows.get((workspace_id, kind), [])
        out: list[EntityNodeView] = []
        for row_cluster, node in rows:
            if cluster is not None and row_cluster != cluster:
                continue
            if name is not None and node.name != name:
                continue
            out.append(node)
        return out


def _planted_store() -> _ListEntitiesStore:
    """Two clusters in workspace A; a same-named class in workspace B."""
    store = _ListEntitiesStore()
    store.plant(workspace_id=_WS_A, kind="StorageClass", cluster="prod-eu", name="gold")
    store.plant(workspace_id=_WS_A, kind="StorageClass", cluster="prod-eu", name="silver")
    store.plant(workspace_id=_WS_A, kind="StorageClass", cluster="prod-us", name="bronze")
    # Workspace B carries a StorageClass with the SAME name in the SAME-named
    # cluster — used for the ACL-isolation test.
    store.plant(workspace_id=_WS_B, kind="StorageClass", cluster="prod-eu", name="gold")
    return store


def _wire_store(app: FastAPI, store: _ListEntitiesStore) -> None:
    app.state.graph_store = store


# ---------------------------------------------------------------------------
# Auth helpers — mirror test_mcp_server_coverage.py
# ---------------------------------------------------------------------------

_WS_ID = _WS_A


def _make_token(
    scopes: list[str] | None = None,
    workspace_id: uuid.UUID | None = _WS_ID,
) -> MagicMock:
    token: MagicMock = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.scopes = scopes if scopes is not None else ["search"]
    token.workspace_id = workspace_id
    token.token_prefix = "sk_t"
    token.is_active = True
    token.profile_id = "omniscience-mcp-read-v1"
    token.created_at = datetime.now(UTC)
    token.expires_at = token.created_at + timedelta(days=30)
    return token


def _make_ctx(bearer: str | None = "sk_test") -> Any:
    if bearer is not None:
        request = SimpleNamespace(headers={"authorization": f"Bearer {bearer}"})
    else:
        request = None
    return SimpleNamespace(_request_context=SimpleNamespace(request=request))


def _make_app() -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = None
    return app


# ---------------------------------------------------------------------------
# Decorated tool — auth wrapper (omniscience_server.mcp.server.list_entities)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_entities_unauthorized_when_no_token() -> None:
    """list_entities raises 'unauthorized' when the token is missing."""
    tool_fn = getattr(list_entities, "fn", list_entities)

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="unauthorized"),
    ):
        await tool_fn(kind="StorageClass", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_list_entities_forbidden_without_search_scope() -> None:
    """A token lacking 'search' scope is forbidden from list_entities."""
    tool_fn = getattr(list_entities, "fn", list_entities)
    token = _make_token(scopes=["sources:read"])

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation"),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool_fn(kind="StorageClass", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_list_entities_no_workspace_raises_forbidden() -> None:
    """list_entities raises 'forbidden' for a token with no workspace."""
    tool_fn = getattr(list_entities, "fn", list_entities)
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
        await tool_fn(kind="StorageClass", ctx=_make_ctx())


@pytest.mark.asyncio
async def test_list_entities_happy_path_delegates() -> None:
    """list_entities forwards the contract kwargs to mcp_list_entities."""
    tool_fn = getattr(list_entities, "fn", list_entities)
    token = _make_token()
    expected: dict[str, Any] = {"entities": [], "effective_as_of": _T1.isoformat(), "meta": None}

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_list_entities",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_fn,
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        result = await tool_fn(
            kind="StorageClass", ctx=_make_ctx(), cluster="prod-eu", name="gold"
        )

    assert result["entities"] == expected["entities"]
    assert result["effective_as_of"] == expected["effective_as_of"]
    assert result["meta"]["contract"]["version"] == "1.0.0"
    call_kwargs = mock_fn.call_args[1]
    assert call_kwargs["kind"] == "StorageClass"
    assert call_kwargs["cluster"] == "prod-eu"
    assert call_kwargs["name"] == "gold"
    assert call_kwargs["workspace_id"] == _WS_A
    assert call_kwargs["as_of"] is None


@pytest.mark.asyncio
async def test_list_entities_parses_as_of() -> None:
    """list_entities parses the as_of string to a UTC datetime."""
    tool_fn = getattr(list_entities, "fn", list_entities)
    token = _make_token()

    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._get_app", return_value=_make_app()),
        patch(
            "omniscience_server.mcp.server.mcp_list_entities",
            new_callable=AsyncMock,
            return_value={"entities": [], "effective_as_of": "x", "meta": None},
        ) as mock_fn,
        patch("omniscience_server.mcp.server._record_tool_invocation"),
    ):
        await tool_fn(kind="StorageClass", ctx=_make_ctx(), as_of="2026-05-01T08:00:00Z")

    assert mock_fn.call_args[1]["as_of"] == datetime(2026, 5, 1, 8, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Handler — mcp_list_entities against the in-memory store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_get_entity_shaped_entities() -> None:
    """Each entity carries the same per-entity shape as get_entity returns."""
    app = FastAPI()
    _wire_store(app, _planted_store())

    result = await mcp_list_entities(
        app=app, workspace_id=_WS_A, kind="StorageClass", cluster="prod-eu"
    )

    assert {e["name"] for e in result["entities"]} == {"gold", "silver"}
    first = result["entities"][0]
    # Same keys as _entity_view_to_dict / get_entity's "entity" payload.
    assert set(first.keys()) == {
        "name",
        "kind",
        "source",
        "chunk_text",
        "valid_from",
        "valid_to",
        "recorded_at",
    }
    assert first["kind"] == "StorageClass"
    assert result["meta"] is None


@pytest.mark.asyncio
async def test_handler_name_filter_existence_question() -> None:
    """The name filter answers the 'does class X exist in cluster Y' question."""
    app = FastAPI()
    _wire_store(app, _planted_store())

    hit = await mcp_list_entities(
        app=app, workspace_id=_WS_A, kind="StorageClass", cluster="prod-eu", name="gold"
    )
    assert [e["name"] for e in hit["entities"]] == ["gold"]

    miss = await mcp_list_entities(
        app=app, workspace_id=_WS_A, kind="StorageClass", cluster="prod-us", name="gold"
    )
    assert miss["entities"] == []


@pytest.mark.asyncio
async def test_handler_echoes_as_of() -> None:
    """effective_as_of echoes the supplied as_of."""
    app = FastAPI()
    _wire_store(app, _planted_store())

    result = await mcp_list_entities(
        app=app,
        workspace_id=_WS_A,
        kind="StorageClass",
        cluster="prod-eu",
        as_of=_T1 + timedelta(hours=1),
    )
    assert result["effective_as_of"] == (_T1 + timedelta(hours=1)).isoformat()


@pytest.mark.asyncio
async def test_handler_acl_isolation_never_widens_workspace() -> None:
    """The store call carries only the caller's workspace; B never leaks into A.

    Both A and B planted a StorageClass 'gold' in 'prod-eu'.  A query scoped
    to A must return exactly one 'gold' (A's), and every store call must
    carry workspace A — no silent widening (ACL invariant).
    """
    store = _planted_store()
    app = FastAPI()
    _wire_store(app, store)

    result = await mcp_list_entities(
        app=app, workspace_id=_WS_A, kind="StorageClass", cluster="prod-eu", name="gold"
    )
    assert [e["name"] for e in result["entities"]] == ["gold"]
    assert all(call["workspace_id"] == _WS_A for call in store.calls)


@pytest.mark.asyncio
async def test_handler_pre_history_empty_carries_degraded_meta() -> None:
    """An empty result at a pre-history as_of carries the degraded hint."""
    app = FastAPI()
    # Empty store -> no rows -> empty list, with a non-None as_of.
    _wire_store(app, _ListEntitiesStore())

    result = await mcp_list_entities(
        app=app,
        workspace_id=_WS_A,
        kind="StorageClass",
        as_of=_PRE_HISTORY,
    )
    assert result["entities"] == []
    assert result["meta"] == {"degraded_response": DEGRADED_PRE_HISTORY}


@pytest.mark.asyncio
async def test_handler_empty_current_has_no_degraded_meta() -> None:
    """An empty current-state result (as_of=None) is NOT degraded."""
    app = FastAPI()
    _wire_store(app, _ListEntitiesStore())

    result = await mcp_list_entities(app=app, workspace_id=_WS_A, kind="StorageClass")
    assert result["entities"] == []
    assert result["meta"] is None


@pytest.mark.asyncio
async def test_handler_raises_when_graph_store_missing() -> None:
    """A missing graph_store on app.state is a RuntimeError (fail-closed)."""
    app = FastAPI()
    with pytest.raises(RuntimeError, match="graph_store not available"):
        await mcp_list_entities(app=app, workspace_id=_WS_A, kind="StorageClass")


# ---------------------------------------------------------------------------
# FastMCP registry — input-schema contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_exposes_list_entities_with_contract_schema() -> None:
    """The tool is registered as 'list_entities' with the contract inputs."""
    tools = await mcp_server.list_tools()
    names = {t.name: t for t in tools}
    assert "list_entities" in names, "missing MCP tool: list_entities"
    props = names["list_entities"].inputSchema.get("properties", {})
    for field in ("kind", "cluster", "name", "as_of"):
        assert field in props, f"list_entities input schema must declare '{field}'"
