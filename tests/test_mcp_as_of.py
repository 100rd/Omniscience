"""End-to-end ``as_of`` contract tests for MCP and REST surfaces (issue #133).

What this file covers
---------------------

The Neo4j adapter's bitemporal ``as_of`` plumbing landed in #170/#132
and has its own unit tests (``tests/test_neo4j_store_as_of.py``).  This
file pins the **public-surface** contract from issue #133:

1. ``search``, ``get_entity`` and ``get_related_entities`` (MCP + REST)
   accept ``as_of`` and forward it to the underlying ``GraphStore``.
2. The response envelope carries ``effective_as_of`` (echo-back of the
   supplied ``as_of``, or response-generation time when ``None``).
3. Naive datetimes are rejected with the structured
   ``invalid_timezone`` code at HTTP 400 (REST) and as an MCP error
   envelope (MCP).
4. Future ``as_of`` is accepted and behaves like latest.
5. Pre-history ``as_of`` returns an empty result with
   ``meta.degraded_response = "as_of_before_recorded_history"``.
6. Cross-workspace isolation under ``as_of`` — the token-derived
   ``workspace_id`` continues to gate the read at any ``as_of``.
7. Telemetry: the ``omniscience_request_duration_seconds`` histogram
   carries the ``as_of_kind`` label and observes one sample per
   request, including the ``pre_history`` upgrade.
8. Contract test: the FastMCP tool registry exposes ``as_of`` on every
   relevant tool's input schema.
9. End-to-end smoke test: MCP transport -> composer -> Neo4j stub ->
   response, verifying ``as_of`` flows through.

Workspace fixtures use the existing ``_auth_token`` /
``_lookup_token`` patch pattern from
``tests/test_graph_workspace_isolation.py``; the GraphStore is a
hand-rolled fake that honours ``(workspace_id, name, as_of)`` so the
test does not depend on a Neo4j container.

ADR refs
--------
- ADR-0008 §5 — open-closed ``[valid_from, valid_to)`` interval and
  ``valid_to=NULL`` sentinel.
- Issue #133 §H — ACL invariant: ``as_of`` is a query-time predicate,
  not an authorisation surface.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphResultView,
)
from omniscience_retrieval.graph_rag import GraphRAGComposer
from omniscience_retrieval.models import (
    QueryStats,
    SearchRequest,
    SearchResult,
)
from omniscience_server.app import create_app
from omniscience_server.as_of import (
    DEGRADED_PRE_HISTORY,
    REQUEST_DURATION_SECONDS,
    classify_as_of,
    enforce_utc,
    resolve_effective_as_of,
)
from omniscience_server.mcp.server import _parse_as_of, mcp_server
from omniscience_server.mcp.tools import (
    mcp_get_entity,
    mcp_get_related_entities,
    mcp_search,
)
from prometheus_client import REGISTRY

# ---------------------------------------------------------------------------
# Constants — three-version timeline, two workspaces
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-1111-2222-3333-4444aa00133a")
_WS_B = uuid.UUID("bbbbbbbb-1111-2222-3333-4444bb00133b")

_T1 = datetime(2026, 4, 12, 8, 0, 0, tzinfo=UTC)  # v1 valid_from
_T2 = datetime(2026, 4, 12, 12, 0, 0, tzinfo=UTC)  # v2 valid_from (v1 ends)
_T3 = datetime(2026, 4, 12, 19, 30, 0, tzinfo=UTC)  # v3 valid_from (v2 ends)

_PRE_HISTORY = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
_FUTURE = datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _BitemporalGraphStore:
    """In-memory ``GraphStore`` fake that honours ``(workspace_id, name, as_of)``.

    Carries a per-workspace timeline of named-entity versions
    keyed by ``valid_from``.  ``as_of=None`` returns the still-current
    version (``valid_to is None``); a non-None value returns the row
    that satisfies ADR-0008 §5's open-closed predicate
    ``valid_from <= as_of < valid_to`` (with ``valid_to is None``
    treated as ``+inf``).

    Cross-workspace queries are silent ``entity_not_found`` (matches
    the protocol contract from #117 / ADR-0005).
    """

    def __init__(self) -> None:
        # (workspace, name) -> list[(valid_from, valid_to, EntityNodeView)]
        self._versions: dict[
            tuple[uuid.UUID, str],
            list[tuple[datetime, datetime | None, EntityNodeView]],
        ] = {}
        self.calls: list[dict[str, Any]] = []

    def plant_versions(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        versions: list[tuple[datetime, datetime | None, str]],
    ) -> None:
        """Plant N versions of an entity along its timeline.

        ``versions`` is a list of ``(valid_from, valid_to, kind)``
        triples; the entity's ``source`` and ``chunk_text`` are
        synthesised from the version's index so each version is
        recognisable in assertions.
        """
        chain: list[tuple[datetime, datetime | None, EntityNodeView]] = []
        for idx, (valid_from, valid_to, kind) in enumerate(versions, start=1):
            chain.append(
                (
                    valid_from,
                    valid_to,
                    EntityNodeView(
                        name=name,
                        kind=kind,
                        source=f"src-{name}-v{idx}",
                        chunk_text=f"v{idx}: {kind} state",
                        valid_from=valid_from,
                        valid_to=valid_to,
                        recorded_at=valid_from,
                    ),
                )
            )
        self._versions[(workspace_id, name)] = chain

    def _resolve(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        as_of: datetime | None,
    ) -> EntityNodeView | None:
        chain = self._versions.get((workspace_id, name))
        if not chain:
            return None
        if as_of is None:
            for _valid_from, valid_to, view in chain:
                if valid_to is None:
                    return view
            return None
        for valid_from, valid_to, view in chain:
            upper = valid_to is None or as_of < valid_to
            if valid_from <= as_of and upper:
                return view
        return None

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        self.calls.append(
            {
                "op": "get_entity",
                "entity_name": entity_name,
                "workspace_id": workspace_id,
                "as_of": as_of,
            }
        )
        return self._resolve(workspace_id=workspace_id, name=entity_name, as_of=as_of)

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        self.calls.append(
            {
                "op": "find_related",
                "entity_name": entity_name,
                "workspace_id": workspace_id,
                "as_of": as_of,
                "max_depth": max_depth,
            }
        )
        seed = self._resolve(workspace_id=workspace_id, name=entity_name, as_of=as_of)
        if seed is None:
            raise ValueError(f"entity_not_found:{entity_name}")
        return GraphResultView(seed=seed, related=[], edges=[])

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return await self.find_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=as_of,
            max_depth=max_depth,
            edge_types=edge_types,
        )


def _planted_store() -> _BitemporalGraphStore:
    """Three-version timeline for ``svc.ratings`` in workspace A.

    v1 [_T1, _T2):  kind="deployed"
    v2 [_T2, _T3):  kind="degraded"
    v3 [_T3, NULL): kind="recovered"
    """
    store = _BitemporalGraphStore()
    store.plant_versions(
        workspace_id=_WS_A,
        name="svc.ratings",
        versions=[
            (_T1, _T2, "deployed"),
            (_T2, _T3, "degraded"),
            (_T3, None, "recovered"),
        ],
    )
    # Workspace B carries an entity with the SAME name but a different
    # timeline — used for cross-workspace isolation tests.
    store.plant_versions(
        workspace_id=_WS_B,
        name="svc.ratings",
        versions=[
            (_T1, None, "B-only-state"),
        ],
    )
    return store


# ---------------------------------------------------------------------------
# Auth helpers — same pattern as test_graph_workspace_isolation.py
# ---------------------------------------------------------------------------


def _auth_token(workspace_id: uuid.UUID | None) -> tuple[MagicMock, str]:
    plaintext, prefix = generate_token("as_of")
    token: MagicMock = MagicMock(spec=ApiToken)
    token.hashed_token = hash_token(plaintext)
    token.token_prefix = prefix
    token.scopes = ["search"]
    token.expires_at = None
    token.is_active = True
    token.workspace_id = workspace_id
    return token, plaintext


def _make_rest_app() -> FastAPI:
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    return create_app(settings=settings)


# ---------------------------------------------------------------------------
# Pydantic-level: SearchRequest.as_of validator
# ---------------------------------------------------------------------------


class TestSearchRequestAsOfValidation:
    """``SearchRequest.as_of`` enforces UTC at the model boundary."""

    def test_naive_datetime_rejected(self) -> None:
        """A naive datetime raises ``invalid_timezone`` at validation."""
        with pytest.raises(ValueError, match="invalid_timezone"):
            SearchRequest(query="q", as_of=datetime(2026, 4, 1, 0, 0, 0))

    def test_non_utc_normalised_to_utc(self) -> None:
        """An aware non-UTC datetime is converted to UTC, not rejected."""
        from datetime import timezone

        plus_two = timezone(timedelta(hours=2))
        req = SearchRequest(query="q", as_of=datetime(2026, 4, 1, 12, 0, 0, tzinfo=plus_two))
        assert req.as_of is not None
        assert req.as_of.tzinfo is UTC
        assert req.as_of == datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)

    def test_z_suffix_accepted(self) -> None:
        """Pydantic accepts the ``Z`` suffix as UTC (Python 3.11+)."""
        req = SearchRequest.model_validate({"query": "q", "as_of": "2026-04-12T19:25:00Z"})
        assert req.as_of == datetime(2026, 4, 12, 19, 25, 0, tzinfo=UTC)

    def test_none_passes_through(self) -> None:
        """``None`` is the default and serialises as absent."""
        req = SearchRequest(query="q")
        assert req.as_of is None


# ---------------------------------------------------------------------------
# Pydantic / shared validator behaviour
# ---------------------------------------------------------------------------


class TestEnforceUtc:
    """Direct unit tests for :func:`enforce_utc`."""

    def test_none(self) -> None:
        assert enforce_utc(None) is None

    def test_naive_raises_invalid_timezone(self) -> None:
        with pytest.raises(ValueError, match="invalid_timezone"):
            enforce_utc(datetime(2026, 4, 1, 0, 0, 0))

    def test_utc_returned_unchanged(self) -> None:
        value = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
        assert enforce_utc(value) == value

    def test_non_utc_normalised(self) -> None:
        from datetime import timezone

        value = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        result = enforce_utc(value)
        assert result is not None
        assert result.tzinfo is UTC


# ---------------------------------------------------------------------------
# MCP tool helpers — direct invocation
# ---------------------------------------------------------------------------


def _wire_session_factory(app: FastAPI) -> None:
    """Attach a no-op AsyncSession factory so the auth middleware passes."""
    if getattr(app.state, "db_session_factory", None) is not None:
        return
    session = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = factory


def _wire_graph_store(app: FastAPI, store: _BitemporalGraphStore) -> None:
    """Wire the bitemporal fake plus a no-op session factory.

    The auth middleware reads ``app.state.db_session_factory`` to look
    up the bearer token; when ``None`` it 401s before our handler
    runs.  We don't need a real DB — the ``_lookup_token`` mock returns
    the ApiToken directly — but the factory must be present and
    callable.  An ``AsyncMock`` returning a no-op session covers it.
    """
    app.state.graph_store = store
    _wire_session_factory(app)


@pytest.mark.asyncio
class TestMcpGetEntityAsOf:
    """``mcp_get_entity`` returns the right version for each ``as_of``."""

    async def test_as_of_t2_returns_v2(self) -> None:
        app = FastAPI()
        _wire_graph_store(app, _planted_store())
        result = await mcp_get_entity(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_A,
            as_of=_T2 + timedelta(seconds=1),
        )
        assert result["entity"]["kind"] == "degraded"
        assert result["effective_as_of"] == (_T2 + timedelta(seconds=1)).isoformat()
        assert result["meta"] is None

    async def test_as_of_none_returns_current(self) -> None:
        app = FastAPI()
        _wire_graph_store(app, _planted_store())
        result = await mcp_get_entity(app=app, entity_name="svc.ratings", workspace_id=_WS_A)
        assert result["entity"]["kind"] == "recovered"
        # effective_as_of is response-generation time — exists, in UTC.
        echo = datetime.fromisoformat(result["effective_as_of"])
        assert echo.tzinfo is UTC

    async def test_future_as_of_returns_latest(self) -> None:
        """A future ``as_of`` resolves to the still-current row.

        The bitemporal predicate ``valid_from <= as_of`` is satisfied
        and ``valid_to is NULL`` always passes the upper bound, so the
        v3 row is returned — equivalent to ``as_of=None``.
        """
        app = FastAPI()
        _wire_graph_store(app, _planted_store())
        result = await mcp_get_entity(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_A,
            as_of=_FUTURE,
        )
        assert result["entity"]["kind"] == "recovered"
        assert result["effective_as_of"] == _FUTURE.isoformat()

    async def test_pre_history_returns_degraded_response(self) -> None:
        """``as_of`` before any record -> empty + degraded hint.

        Issue #133 §B: an explicit ``as_of`` that finds no row carries
        the degraded-response hint instead of a 404.
        """
        app = FastAPI()
        _wire_graph_store(app, _planted_store())
        result = await mcp_get_entity(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_A,
            as_of=_PRE_HISTORY,
        )
        assert result["entity"] is None
        assert result["meta"] == {"degraded_response": DEGRADED_PRE_HISTORY}
        assert result["effective_as_of"] == _PRE_HISTORY.isoformat()

    async def test_naive_datetime_rejected(self) -> None:
        """A naive ``as_of`` raises ``invalid_timezone`` at the tool level."""
        app = FastAPI()
        _wire_graph_store(app, _planted_store())
        with pytest.raises(ValueError, match="invalid_timezone"):
            await mcp_get_entity(
                app=app,
                entity_name="svc.ratings",
                workspace_id=_WS_A,
                as_of=datetime(2026, 4, 12, 12, 0, 0),
            )


@pytest.mark.asyncio
class TestMcpGetRelatedEntitiesAsOf:
    """``mcp_get_related_entities`` plumbs ``as_of`` to ``find_related``."""

    async def test_as_of_t2_passes_through(self) -> None:
        store = _planted_store()
        app = FastAPI()
        _wire_graph_store(app, store)
        result = await mcp_get_related_entities(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_A,
            as_of=_T2 + timedelta(seconds=1),
        )
        assert result["seed"]["chunk_text"] == "v2: degraded state"
        assert result["effective_as_of"] == (_T2 + timedelta(seconds=1)).isoformat()
        # Verify the GraphStore call carried as_of.
        last = next(c for c in store.calls if c["op"] == "find_related")
        assert last["as_of"] == _T2 + timedelta(seconds=1)

    async def test_pre_history_returns_empty_envelope(self) -> None:
        store = _planted_store()
        app = FastAPI()
        _wire_graph_store(app, store)
        result = await mcp_get_related_entities(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_A,
            as_of=_PRE_HISTORY,
        )
        assert result["related"] == []
        assert result["meta"] == {"degraded_response": DEGRADED_PRE_HISTORY}
        assert result["stats"]["entities_found"] == 0


# ---------------------------------------------------------------------------
# Cross-workspace isolation under ``as_of`` — ACL invariant (issue #133 §H)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCrossWorkspaceIsolationUnderAsOf:
    """``as_of`` is a query-time predicate, NOT an authorisation surface."""

    async def test_workspace_a_cannot_see_workspace_b_at_t2(self) -> None:
        """A token scoped to A queries ``as_of=T2`` but B's data MUST stay invisible.

        Both workspaces planted ``svc.ratings``; only A is queried.  The
        result must reflect A's v2 row (``kind="degraded"``), never B's
        ``kind="B-only-state"`` even though B has a row covering T2.
        """
        store = _planted_store()
        app = FastAPI()
        _wire_graph_store(app, store)
        result = await mcp_get_entity(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_A,
            as_of=_T2 + timedelta(seconds=1),
        )
        assert result["entity"]["kind"] == "degraded"
        # Sanity: ensure the store call carried workspace A only — no
        # silent widening at any as_of.
        for call in store.calls:
            assert call["workspace_id"] == _WS_A

    async def test_workspace_b_only_sees_its_own_at_t1(self) -> None:
        store = _planted_store()
        app = FastAPI()
        _wire_graph_store(app, store)
        result = await mcp_get_entity(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_B,
            as_of=_T1 + timedelta(hours=1),
        )
        assert result["entity"]["kind"] == "B-only-state"
        for call in store.calls:
            assert call["workspace_id"] == _WS_B


# ---------------------------------------------------------------------------
# REST surface — query-string + body interplay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRestEntitiesGetAsOf:
    """``GET /api/v1/entities/{name}`` plumbs ``as_of``."""

    async def test_as_of_t2_returns_v2(self) -> None:
        app = _make_rest_app()
        _wire_graph_store(app, _planted_store())
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/entities/svc.ratings",
                    params={"as_of": (_T2 + timedelta(seconds=1)).isoformat()},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["entity"]["kind"] == "degraded"
        assert body["effective_as_of"] == (_T2 + timedelta(seconds=1)).isoformat()

    async def test_naive_as_of_rejected_with_400(self) -> None:
        """A naive ``as_of`` query param returns 400 INVALID_TIMEZONE."""
        app = _make_rest_app()
        _wire_graph_store(app, _planted_store())
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/entities/svc.ratings",
                    params={"as_of": "2026-04-12T12:00:00"},  # naive
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "invalid_timezone"

    async def test_pre_history_returns_200_with_degraded_meta(self) -> None:
        """Pre-history ``as_of`` returns 200 with the degraded-response hint."""
        app = _make_rest_app()
        _wire_graph_store(app, _planted_store())
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/entities/svc.ratings",
                    params={"as_of": _PRE_HISTORY.isoformat()},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["entity"] is None
        assert body["meta"] == {"degraded_response": DEGRADED_PRE_HISTORY}

    async def test_cross_workspace_isolation_under_as_of(self) -> None:
        """A token scoped to A under ``as_of`` MUST never see B's data."""
        app = _make_rest_app()
        store = _planted_store()
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/entities/svc.ratings",
                    params={"as_of": (_T2 + timedelta(seconds=1)).isoformat()},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # A's v2 (degraded), never B's "B-only-state".
        assert body["entity"]["kind"] == "degraded"
        assert "B-only-state" not in resp.text
        # And the store saw only workspace A.
        for call in store.calls:
            assert call["workspace_id"] == _WS_A


@pytest.mark.asyncio
class TestRestEntitiesRelatedAsOf:
    """``GET /api/v1/entities/{name}/related`` plumbs ``as_of``."""

    async def test_as_of_t2_returns_200_with_v2_seed(self) -> None:
        app = _make_rest_app()
        _wire_graph_store(app, _planted_store())
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/entities/svc.ratings/related",
                    params={"as_of": (_T2 + timedelta(seconds=1)).isoformat()},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["seed"]["chunk_text"] == "v2: degraded state"
        assert body["effective_as_of"] == (_T2 + timedelta(seconds=1)).isoformat()

    async def test_pre_history_yields_degraded_envelope_not_404(self) -> None:
        app = _make_rest_app()
        _wire_graph_store(app, _planted_store())
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/entities/svc.ratings/related",
                    params={"as_of": _PRE_HISTORY.isoformat()},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["meta"] == {"degraded_response": DEGRADED_PRE_HISTORY}
        assert body["stats"]["entities_found"] == 0


# ---------------------------------------------------------------------------
# REST /search — body validation + envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRestSearchAsOf:
    """``POST /api/v1/search`` accepts ``as_of`` in body or query string."""

    async def test_naive_body_rejected_400_pydantic(self) -> None:
        app = _make_rest_app()
        _wire_session_factory(app)
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/search",
                    json={"query": "ratings", "as_of": "2026-04-12T12:00:00"},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        # Pydantic surfaces a 422 for body validation; the body carries
        # the ``invalid_timezone`` token in the field error message.
        assert resp.status_code == 422, resp.text
        assert "invalid_timezone" in resp.text

    async def test_naive_query_rejected_400(self) -> None:
        app = _make_rest_app()
        _wire_session_factory(app)
        token, plaintext = _auth_token(workspace_id=_WS_A)
        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/search",
                    params={"as_of": "2026-04-12T12:00:00"},
                    json={"query": "ratings"},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        # FastAPI's Query datetime parsing accepts the naive value, so
        # the manual ``enforce_utc`` call kicks in and 400s.
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["error"]["code"] == "invalid_timezone"


# ---------------------------------------------------------------------------
# Composer-level: the search composer forwards as_of to GraphStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestComposerForwardsAsOf:
    """``GraphRAGComposer.search`` forwards ``as_of`` to graph traversal.

    The vector step in #133 is unfiltered on time per scope guardrail
    (Qdrant ``as_of`` is #134) — only the graph-anchor stage carries
    the predicate.  This is the contract test.
    """

    async def test_search_with_anchor_passes_as_of_to_traverse(self) -> None:
        from omniscience_retrieval.graph_rag import ANCHOR_FILTER_KEY

        store = _planted_store()

        class _StubVector:
            async def search(
                self,
                *,
                request: SearchRequest,
                workspace_id: uuid.UUID,
                as_of: datetime | None = None,
            ) -> SearchResult:
                return SearchResult(
                    hits=[],
                    query_stats=QueryStats(
                        total_matches_before_filters=0,
                        vector_matches=0,
                        text_matches=0,
                        duration_ms=0.0,
                    ),
                )

        class _StubLegacy:
            async def search(self, request: SearchRequest) -> SearchResult:
                raise AssertionError("legacy not used")

        composer = GraphRAGComposer(
            graph_store=store,  # type: ignore[arg-type]
            vector_store=_StubVector(),
            legacy_service=_StubLegacy(),
        )
        # Force the GraphRAG path on the in-memory fakes.
        import omniscience_retrieval.graph_rag as gr

        gr._should_use_graphrag = lambda g, v: True  # type: ignore[assignment]
        composer._graphrag_active = True

        req = SearchRequest(
            query="ratings",
            filters={ANCHOR_FILTER_KEY: "svc.ratings"},
        )
        result = await composer.search(req, workspace_id=_WS_A, as_of=_T2 + timedelta(seconds=1))

        # ``as_of`` reached the graph store.
        traverse_calls = [c for c in store.calls if c["op"] == "find_related"]
        assert len(traverse_calls) == 1
        assert traverse_calls[0]["as_of"] == _T2 + timedelta(seconds=1)
        assert traverse_calls[0]["workspace_id"] == _WS_A
        assert result.effective_as_of == _T2 + timedelta(seconds=1)

    async def test_search_without_as_of_keeps_current_state_path(self) -> None:
        store = _planted_store()

        class _StubVector:
            async def search(
                self,
                *,
                request: SearchRequest,
                workspace_id: uuid.UUID,
                as_of: datetime | None = None,
            ) -> SearchResult:
                return SearchResult(
                    hits=[],
                    query_stats=QueryStats(
                        total_matches_before_filters=0,
                        vector_matches=0,
                        text_matches=0,
                        duration_ms=0.0,
                    ),
                )

        class _StubLegacy:
            async def search(self, request: SearchRequest) -> SearchResult:
                return SearchResult(
                    hits=[],
                    query_stats=QueryStats(
                        total_matches_before_filters=0,
                        vector_matches=0,
                        text_matches=0,
                        duration_ms=0.0,
                    ),
                )

        composer = GraphRAGComposer(
            graph_store=store,  # type: ignore[arg-type]
            vector_store=_StubVector(),
            legacy_service=_StubLegacy(),
        )
        composer._graphrag_active = True

        req = SearchRequest(query="ratings")
        result = await composer.search(req, workspace_id=_WS_A)
        # No anchor and no as_of -> traverse is not called at all (the
        # anchor stage is skipped).  effective_as_of is response-time.
        assert result.effective_as_of is not None
        assert result.effective_as_of.tzinfo is UTC


# ---------------------------------------------------------------------------
# Telemetry — histogram label ``as_of_kind``
# ---------------------------------------------------------------------------


def _hist_sample_count(*, surface: str, tool: str, as_of_kind: str) -> float:
    """Read the ``_count`` sample for a specific label triple."""
    name = "omniscience_request_duration_seconds_count"
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name != name:
                continue
            labels = sample.labels
            if (
                labels.get("surface") == surface
                and labels.get("tool") == tool
                and labels.get("as_of_kind") == as_of_kind
            ):
                return sample.value
    return 0.0


class TestTelemetryAsOfKind:
    """The shared duration histogram carries ``as_of_kind`` as a label."""

    def test_histogram_has_as_of_kind_label(self) -> None:
        labels = REQUEST_DURATION_SECONDS._labelnames
        assert "as_of_kind" in labels
        assert "surface" in labels
        assert "tool" in labels

    def test_classify_buckets(self) -> None:
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        assert classify_as_of(None) == "none"
        assert classify_as_of(_T1, now=now) == "past"
        assert classify_as_of(_FUTURE, now=now) == "future"

    @pytest.mark.asyncio
    async def test_pre_history_promoted_in_telemetry(self) -> None:
        """A past ``as_of`` returning empty -> ``as_of_kind=pre_history``."""
        app = FastAPI()
        _wire_graph_store(app, _planted_store())
        baseline = _hist_sample_count(surface="mcp", tool="get_entity", as_of_kind="pre_history")
        await mcp_get_entity(
            app=app,
            entity_name="svc.ratings",
            workspace_id=_WS_A,
            as_of=_PRE_HISTORY,
        )
        after = _hist_sample_count(surface="mcp", tool="get_entity", as_of_kind="pre_history")
        assert after == baseline + 1

    @pytest.mark.asyncio
    async def test_none_kind_recorded_for_current_state_read(self) -> None:
        app = FastAPI()
        _wire_graph_store(app, _planted_store())
        baseline = _hist_sample_count(surface="mcp", tool="get_entity", as_of_kind="none")
        await mcp_get_entity(app=app, entity_name="svc.ratings", workspace_id=_WS_A)
        after = _hist_sample_count(surface="mcp", tool="get_entity", as_of_kind="none")
        assert after == baseline + 1


# ---------------------------------------------------------------------------
# Contract test — FastMCP tool input schema exposes ``as_of``
# ---------------------------------------------------------------------------


class TestMcpToolSchemaContract:
    """Every relevant MCP tool's input schema declares ``as_of``."""

    @pytest.mark.asyncio
    async def test_search_schema_has_as_of(self) -> None:
        tools = await mcp_server.list_tools()
        names = {t.name: t for t in tools}
        for tool_name in ("search", "get_entity", "get_related_entities"):
            assert tool_name in names, f"missing MCP tool: {tool_name}"
            schema = names[tool_name].inputSchema
            props = schema.get("properties", {})
            assert "as_of" in props, (
                f"MCP tool '{tool_name}' input schema must declare 'as_of' (issue #133 contract)"
            )

    @pytest.mark.asyncio
    async def test_resolve_incident_schema_has_as_of(self) -> None:
        """``resolve_incident`` (issue #153) is registered with ``as_of``.

        The forcing-function test from #173 has been replaced with this
        positive contract test now that #153 has landed.  The new tool
        accepts ``as_of`` and forwards it to the bitemporal-aware
        ``GraphStore`` reads (#170 / #173 / #174).
        """
        tools = await mcp_server.list_tools()
        names = {t.name: t for t in tools}
        assert "resolve_incident" in names, (
            "resolve_incident must be registered now that issue #153 has landed."
        )
        schema = names["resolve_incident"].inputSchema
        props = schema.get("properties", {})
        assert "as_of" in props, (
            "MCP tool 'resolve_incident' input schema must declare 'as_of' "
            "(issue #153 contract; supersedes the #173 forcing-function test)."
        )


# ---------------------------------------------------------------------------
# _parse_as_of (MCP wire-format parser)
# ---------------------------------------------------------------------------


class TestParseAsOfWire:
    """``_parse_as_of`` is the MCP transport's string -> datetime gate."""

    def test_none_passes(self) -> None:
        assert _parse_as_of(None) is None

    def test_empty_string_passes_as_none(self) -> None:
        assert _parse_as_of("") is None

    def test_z_suffix(self) -> None:
        result = _parse_as_of("2026-04-12T12:00:00Z")
        assert result is not None
        assert result.tzinfo is not None
        assert result == datetime(2026, 4, 12, 12, 0, 0, tzinfo=UTC)

    def test_offset(self) -> None:
        result = _parse_as_of("2026-04-12T12:00:00+00:00")
        assert result is not None
        assert result.utcoffset() == timedelta(0)

    def test_naive_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"invalid_timezone"):
            _parse_as_of("2026-04-12T12:00:00")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"invalid_timezone"):
            _parse_as_of("not-a-datetime")


# ---------------------------------------------------------------------------
# resolve_effective_as_of
# ---------------------------------------------------------------------------


class TestResolveEffectiveAsOf:
    """``resolve_effective_as_of`` echoes back or returns now()."""

    def test_explicit_as_of_echoed(self) -> None:
        v = datetime(2026, 4, 12, 12, 0, 0, tzinfo=UTC)
        assert resolve_effective_as_of(v) == v

    def test_none_returns_now_in_utc(self) -> None:
        result = resolve_effective_as_of(None)
        assert result.tzinfo is UTC
        # within a few seconds of "now"
        assert abs((datetime.now(UTC) - result).total_seconds()) < 5


# ---------------------------------------------------------------------------
# End-to-end smoke test — MCP transport -> composer -> Neo4j stub -> response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_as_of_flows_through_mcp_to_graph_store() -> None:
    """Smoke test: as_of survives MCP transport -> composer -> graph_store.

    This exercises the full path the issue's acceptance criterion
    requires: MCP wire-format parsing -> tool handler ->
    GraphRAGComposer -> GraphStore.traverse -> response with
    ``effective_as_of`` echoed back.
    """
    from omniscience_retrieval.graph_rag import ANCHOR_FILTER_KEY

    store = _planted_store()

    class _StubVector:
        async def search(
            self,
            *,
            request: SearchRequest,
            workspace_id: uuid.UUID,
            as_of: datetime | None = None,
        ) -> SearchResult:
            return SearchResult(
                hits=[],
                query_stats=QueryStats(
                    total_matches_before_filters=0,
                    vector_matches=0,
                    text_matches=0,
                    duration_ms=0.0,
                ),
            )

    class _StubLegacy:
        async def search(self, request: SearchRequest) -> SearchResult:
            raise AssertionError("legacy not used")

    composer = GraphRAGComposer(
        graph_store=store,  # type: ignore[arg-type]
        vector_store=_StubVector(),
        legacy_service=_StubLegacy(),
    )
    composer._graphrag_active = True

    app = FastAPI()
    app.state.graph_store = store
    app.state.graph_rag_composer = composer

    parsed = _parse_as_of((_T2 + timedelta(seconds=1)).isoformat())
    payload = await mcp_search(
        app=app,
        query="ratings",
        filters={ANCHOR_FILTER_KEY: "svc.ratings"},
        workspace_id=_WS_A,
        as_of=parsed,
    )
    # Echo back is microsecond-faithful, in UTC.
    echo = datetime.fromisoformat(payload["effective_as_of"])
    assert echo == _T2 + timedelta(seconds=1)
    # Graph store saw as_of and the workspace.
    traverse_calls = [c for c in store.calls if c["op"] == "find_related"]
    assert len(traverse_calls) == 1
    assert traverse_calls[0]["as_of"] == _T2 + timedelta(seconds=1)
    assert traverse_calls[0]["workspace_id"] == _WS_A
