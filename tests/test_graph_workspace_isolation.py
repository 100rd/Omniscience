"""Cross-workspace isolation tests for graph retrieval — regression for issue #117.

Problem
-------

Before this fix, ``GraphQueryService.get_related`` and its REST/MCP
callers did not apply the workspace filter.  A token scoped to workspace
A could retrieve entities and edges from workspace B by traversing the
graph.

Strategy
--------

We exercise three layers in one test file:

1. **Service layer** — a fake AsyncSession applies the workspace predicate
   exactly as Postgres would (it checks that the compiled SQL includes
   ``sources.tenant_id`` and it filters an in-memory fixture by that
   tenant).  This proves the SQL-level guarantee.
2. **SQL shape** — we compile the service's SELECT statements and assert
   the workspace predicate is present on EVERY read path (seed lookup,
   adjacency, endpoint resolve).  This is the "defence-in-depth" grep-like
   regression guard: future refactors cannot silently drop it.
3. **REST endpoint** — an end-to-end call through the FastAPI app proves
   the token→workspace resolution wires into the service correctly and
   that a cross-workspace request returns 404 (never a B-entity payload).
4. **MCP tool** — same check through the MCP tool path.

No real database is started; Postgres-specific column types (pgvector,
tsvector) prevent an in-memory SQLite fallback.  The fake session below
is a deliberate middle ground: it parses the compiled SQL parameters to
emulate workspace scoping and serves from an in-memory row set.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_retrieval.graph_query import GraphQueryService
from sqlalchemy.sql import Select

# ---------------------------------------------------------------------------
# Fixtures: two workspaces with deliberately crossing entity names
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_WS_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000b2")


@dataclass
class _FakeEntity:
    """Minimal duck-typed stand-in for :class:`omniscience_core.db.models.Entity`."""

    id: uuid.UUID
    name: str
    entity_type: str
    source_id: uuid.UUID
    chunk_id: uuid.UUID | None = None


@dataclass
class _FakeEdge:
    """Minimal duck-typed stand-in for :class:`omniscience_core.db.models.Edge`."""

    id: uuid.UUID
    source_entity_id: uuid.UUID
    target_entity_id: uuid.UUID
    edge_type: str


@dataclass
class _FakeSource:
    """Minimal duck-typed stand-in for :class:`omniscience_core.db.models.Source`."""

    id: uuid.UUID
    tenant_id: uuid.UUID | None


@dataclass
class _Fixture:
    """Two-workspace fixture with entities, edges, and crossing names."""

    sources: dict[uuid.UUID, _FakeSource] = field(default_factory=dict)
    entities: dict[uuid.UUID, _FakeEntity] = field(default_factory=dict)
    edges: list[_FakeEdge] = field(default_factory=list)

    def entities_by_name(self, name: str) -> Iterable[_FakeEntity]:
        return (e for e in self.entities.values() if e.name == name)

    def workspace_of(self, entity: _FakeEntity) -> uuid.UUID | None:
        return self.sources[entity.source_id].tenant_id


def _build_fixture() -> _Fixture:
    """Build a two-workspace fixture with two entities named ``svc.shared``.

    Workspace A:
        svc.shared   (entity A1)   --calls-->   svc.internal-a   (entity A2)
    Workspace B:
        svc.shared   (entity B1)   --calls-->   svc.internal-b   (entity B2)

    The name ``svc.shared`` crosses both workspaces on purpose.  A
    correctly-scoped query from A must return only A1/A2 and never
    acknowledge B1/B2's existence.
    """
    src_a = _FakeSource(id=uuid.uuid4(), tenant_id=_WS_A)
    src_b = _FakeSource(id=uuid.uuid4(), tenant_id=_WS_B)

    a1 = _FakeEntity(id=uuid.uuid4(), name="svc.shared", entity_type="service", source_id=src_a.id)
    a2 = _FakeEntity(
        id=uuid.uuid4(), name="svc.internal-a", entity_type="service", source_id=src_a.id
    )
    b1 = _FakeEntity(id=uuid.uuid4(), name="svc.shared", entity_type="service", source_id=src_b.id)
    b2 = _FakeEntity(
        id=uuid.uuid4(), name="svc.internal-b", entity_type="service", source_id=src_b.id
    )

    edge_a = _FakeEdge(
        id=uuid.uuid4(), source_entity_id=a1.id, target_entity_id=a2.id, edge_type="calls"
    )
    edge_b = _FakeEdge(
        id=uuid.uuid4(), source_entity_id=b1.id, target_entity_id=b2.id, edge_type="calls"
    )
    # Planted cross-tenant edge — simulates a poisoned row that references an
    # entity in the other workspace.  A correct implementation must NOT
    # traverse it.
    cross_edge = _FakeEdge(
        id=uuid.uuid4(),
        source_entity_id=a1.id,
        target_entity_id=b1.id,
        edge_type="calls",
    )

    fx = _Fixture()
    fx.sources = {src_a.id: src_a, src_b.id: src_b}
    fx.entities = {e.id: e for e in (a1, a2, b1, b2)}
    fx.edges = [edge_a, edge_b, cross_edge]
    return fx


# ---------------------------------------------------------------------------
# Fake session that interprets workspace scoping
# ---------------------------------------------------------------------------


def _extract_workspace_id_param(stmt: Select[Any]) -> uuid.UUID | None:
    """Return the workspace UUID bound into the compiled statement, if any.

    The predicate produced by ``graph_query._workspace_predicate`` is
    ``or_(Source.tenant_id == :tenant_id_1, Source.tenant_id IS NULL)``,
    so the bound param name always starts with ``tenant_id``.  We match
    by name to avoid confusing the workspace UUID with a pivot entity id.
    """
    params = stmt.compile().params
    for key, value in params.items():
        if key.startswith("tenant_id") and isinstance(value, uuid.UUID):
            return value
    return None


def _extract_name_param(stmt: Select[Any]) -> str | None:
    """Return the first string param bound into the statement."""
    params = stmt.compile().params
    for value in params.values():
        if isinstance(value, str):
            return value
    return None


def _extract_non_workspace_uuid_params(stmt: Select[Any]) -> list[uuid.UUID]:
    """Return every UUID param that is NOT the workspace predicate's tenant_id."""
    params = stmt.compile().params
    return [
        v for k, v in params.items() if isinstance(v, uuid.UUID) and not k.startswith("tenant_id")
    ]


class _FakeResult:
    """Matches the slice of AsyncSession.execute()'s return type we use."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeSession:
    """An AsyncSession stand-in that enforces workspace scoping.

    It inspects the compiled SQL of each incoming ``Select`` and routes it
    to one of the fixture helpers.  The helpers apply the workspace
    predicate before returning rows — exactly mirroring what Postgres
    would do for the real query.
    """

    def __init__(self, fixture: _Fixture) -> None:
        self._fx = fixture

    async def execute(self, stmt: Select[Any]) -> _FakeResult:
        non_ws_params = _extract_non_workspace_uuid_params(stmt)
        workspace_id = _extract_workspace_id_param(stmt)

        # Seed lookup: SELECT entities … JOIN sources … WHERE entities.name = ?
        # Discriminate on the bound param name rather than SELECT-list
        # substrings (adjacency's SELECT also mentions entities.name).
        param_names = list(stmt.compile().params.keys())
        has_name_where = any(k.startswith("name") for k in param_names)
        has_entity_id_where = any(k.startswith("id_") or k == "id" for k in param_names)
        has_edge_pivot_where = any(
            k.startswith("source_entity_id") or k.startswith("target_entity_id")
            for k in param_names
        )

        if has_name_where:
            name = _extract_name_param(stmt)
            if name is None or workspace_id is None:
                return _FakeResult([])
            matches = [
                e
                for e in self._fx.entities_by_name(name)
                if self._fx.workspace_of(e) in (workspace_id, None)
            ]
            return _FakeResult(matches[:1])

        # Entity-by-id lookup used to resolve edge endpoints.
        if has_entity_id_where and not has_edge_pivot_where:
            if workspace_id is None or not non_ws_params:
                return _FakeResult([])
            target_id = next(
                (p for p in non_ws_params),
                None,
            )
            if target_id is None:
                return _FakeResult([])
            ent = self._fx.entities.get(target_id)
            if ent is None:
                return _FakeResult([])
            if self._fx.workspace_of(ent) not in (workspace_id, None):
                return _FakeResult([])
            return _FakeResult([ent])

        # Adjacency lookup: SELECT edges, entities … WHERE edges.source_entity_id = ?
        # OR WHERE edges.target_entity_id = ?.  The bound param name tells us
        # which side is the pivot (source_entity_id vs target_entity_id).
        if has_edge_pivot_where:
            if workspace_id is None or not non_ws_params:
                return _FakeResult([])
            param_dict = stmt.compile().params
            is_outgoing = any(k.startswith("source_entity_id") for k in param_dict)
            pivot_key = "source_entity_id" if is_outgoing else "target_entity_id"
            pivot = next(
                (v for k, v in param_dict.items() if k.startswith(pivot_key)),
                None,
            )
            if pivot is None:
                return _FakeResult([])
            pairs: list[tuple[_FakeEdge, _FakeEntity]] = []
            for edge in self._fx.edges:
                if is_outgoing:
                    if edge.source_entity_id != pivot:
                        continue
                    neighbour = self._fx.entities.get(edge.target_entity_id)
                else:
                    if edge.target_entity_id != pivot:
                        continue
                    neighbour = self._fx.entities.get(edge.source_entity_id)
                if neighbour is None:
                    continue
                if self._fx.workspace_of(neighbour) not in (workspace_id, None):
                    # Neighbour is in another workspace: simulate the SQL
                    # WHERE (source.tenant_id = ws OR source.tenant_id IS NULL)
                    # filtering it out at the DB level.
                    continue
                pairs.append((edge, neighbour))
            return _FakeResult(pairs)

        return _FakeResult([])

    async def get(self, model_cls: Any, pk: uuid.UUID) -> Any | None:
        # Only called for Chunk lookups in graph_query.  Tests use chunk_id=None,
        # so this should never actually be called — return None defensively.
        return None

    # The auth middleware calls flush/commit on the shared session while it
    # updates ``last_used_at``.  We stub them out as no-ops so the fake session
    # is a drop-in replacement.
    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _session_factory(fixture: _Fixture) -> Any:
    """Build a factory returning an async context-manager wrapping _FakeSession."""
    session = _FakeSession(fixture)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# ---------------------------------------------------------------------------
# 1. Service-layer isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_scoped_to_workspace_a_never_returns_workspace_b_entities() -> None:
    """Cross-workspace regression for #117 at the service level.

    Token scoped to workspace A asks for ``svc.shared``.  The entity
    ``svc.shared`` exists in both A and B, and a cross-tenant edge is
    planted pointing from A1 to B1.  The result MUST contain only A2 and
    must never mention any B-entity by name.
    """
    fx = _build_fixture()
    service = GraphQueryService(_session_factory(fx))

    result = await service.get_related(
        entity_name="svc.shared",
        workspace_id=_WS_A,
        max_depth=2,
    )

    names_in_result = {result.seed.name, *(n.name for n in result.related)}
    assert "svc.internal-a" in names_in_result
    # The B-side names MUST NOT appear.
    assert "svc.internal-b" not in names_in_result
    # Edges must not leak the cross-tenant B1 endpoint.
    for e in result.edges:
        assert "svc.internal-b" not in (e.from_entity, e.to_entity)


@pytest.mark.asyncio
async def test_service_scoped_to_workspace_b_never_returns_workspace_a_entities() -> None:
    """Symmetric check: B must not see any A-side entities."""
    fx = _build_fixture()
    service = GraphQueryService(_session_factory(fx))

    result = await service.get_related(
        entity_name="svc.shared",
        workspace_id=_WS_B,
        max_depth=2,
    )

    names_in_result = {result.seed.name, *(n.name for n in result.related)}
    assert "svc.internal-b" in names_in_result
    assert "svc.internal-a" not in names_in_result
    for e in result.edges:
        assert "svc.internal-a" not in (e.from_entity, e.to_entity)


@pytest.mark.asyncio
async def test_service_raises_not_found_for_entity_in_other_workspace() -> None:
    """An entity that exists only in workspace B looks like 404 to workspace A.

    We never leak existence of cross-tenant rows — "not found" is the
    only legal signal.
    """
    fx = _build_fixture()
    # Remove the A-side svc.shared so that svc.shared only exists in B.
    a1_id = next(
        e.id
        for e in fx.entities.values()
        if e.name == "svc.shared" and fx.workspace_of(e) == _WS_A
    )
    del fx.entities[a1_id]
    fx.edges = [e for e in fx.edges if e.source_entity_id != a1_id]

    service = GraphQueryService(_session_factory(fx))

    with pytest.raises(ValueError, match=r"entity_not_found:svc\.shared"):
        await service.get_related(
            entity_name="svc.shared",
            workspace_id=_WS_A,
            max_depth=1,
        )


# ---------------------------------------------------------------------------
# 2. SQL-shape regression guards — prevent silent re-introduction of the bug
# ---------------------------------------------------------------------------


def test_seed_lookup_sql_includes_workspace_predicate() -> None:
    """The seed-lookup SELECT must reference sources.tenant_id."""

    # Build a Service instance without calling __init__ side effects on a real factory.
    service = GraphQueryService(session_factory=MagicMock())

    # Monkey-patch execute to capture the compiled statement.
    captured: dict[str, Select[Any]] = {}

    async def _capture(stmt: Select[Any]) -> Any:
        captured["stmt"] = stmt

        class _Empty:
            def scalar_one_or_none(self) -> Any | None:
                return None

        return _Empty()

    sess = AsyncMock()
    sess.execute = AsyncMock(side_effect=_capture)

    import asyncio

    asyncio.run(service._fetch_entity_by_name(sess, "whatever", workspace_id=_WS_A))

    sql = str(captured["stmt"].compile()).lower()
    assert "sources.tenant_id" in sql, (
        "Seed lookup lost the workspace predicate — graph retrieval would "
        "leak cross-tenant entities (regression of issue #117)"
    )


def test_adjacency_sql_includes_workspace_predicate() -> None:
    """Both outgoing and incoming adjacency SELECTs must reference sources.tenant_id."""

    service = GraphQueryService(session_factory=MagicMock())

    captured: list[Select[Any]] = []

    async def _capture(stmt: Select[Any]) -> Any:
        captured.append(stmt)

        class _Empty:
            def __iter__(self) -> Any:
                return iter([])

        return _Empty()

    sess = AsyncMock()
    sess.execute = AsyncMock(side_effect=_capture)

    import asyncio

    asyncio.run(
        service._fetch_adjacent(
            sess,
            str(uuid.uuid4()),
            edge_types=None,
            workspace_id=_WS_A,
        )
    )

    assert len(captured) == 2, "adjacency should issue one outgoing + one incoming query"
    for stmt in captured:
        sql = str(stmt.compile()).lower()
        assert "sources.tenant_id" in sql, (
            "Adjacency query lost the workspace predicate — transitive graph "
            "leaks possible (regression of issue #117)"
        )


def test_entity_by_id_sql_includes_workspace_predicate() -> None:
    """Endpoint resolve query must reference sources.tenant_id."""

    service = GraphQueryService(session_factory=MagicMock())

    captured: dict[str, Select[Any]] = {}

    async def _capture(stmt: Select[Any]) -> Any:
        captured["stmt"] = stmt

        class _Empty:
            def scalar_one_or_none(self) -> Any | None:
                return None

        return _Empty()

    sess = AsyncMock()
    sess.execute = AsyncMock(side_effect=_capture)

    import asyncio

    asyncio.run(service._fetch_entity_by_id(sess, str(uuid.uuid4()), workspace_id=_WS_A))

    sql = str(captured["stmt"].compile()).lower()
    assert "sources.tenant_id" in sql


# ---------------------------------------------------------------------------
# 3. REST endpoint — end-to-end isolation
# ---------------------------------------------------------------------------


def _make_rest_app() -> FastAPI:
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    return create_app(settings=settings)


def _auth_token(workspace_id: uuid.UUID | None) -> tuple[MagicMock, str]:
    plaintext, prefix = generate_token("test")
    token: MagicMock = MagicMock(spec=ApiToken)
    token.hashed_token = hash_token(plaintext)
    token.token_prefix = prefix
    token.scopes = ["search"]
    token.expires_at = None
    token.is_active = True
    token.workspace_id = workspace_id
    return token, plaintext


@pytest.mark.asyncio
async def test_rest_endpoint_rejects_workspace_a_probing_workspace_b() -> None:
    """End-to-end: A-scoped caller cannot fetch B-only entities via REST."""
    fx = _build_fixture()
    # Drop the A-side ``svc.shared`` so only B owns that name.
    a1_id = next(
        e.id
        for e in fx.entities.values()
        if e.name == "svc.shared" and fx.workspace_of(e) == _WS_A
    )
    del fx.entities[a1_id]
    fx.edges = [e for e in fx.edges if e.source_entity_id != a1_id]

    token_a, plaintext = _auth_token(workspace_id=_WS_A)
    factory = _session_factory(fx)

    app = _make_rest_app()
    app.state.db_session_factory = factory

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=token_a,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/entities/svc.shared/related",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["error"]["code"] == "entity_not_found"
    # Sanity: response body must NOT contain any B-side internal names.
    assert "svc.internal-b" not in response.text


@pytest.mark.asyncio
async def test_rest_endpoint_rejects_unscoped_token() -> None:
    """Tokens without a workspace are rejected with 403 (fail-closed)."""
    fx = _build_fixture()
    token, plaintext = _auth_token(workspace_id=None)
    factory = _session_factory(fx)

    app = _make_rest_app()
    app.state.db_session_factory = factory

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=token,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/entities/svc.shared/related",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_rest_endpoint_workspace_a_traversal_never_includes_b() -> None:
    """End-to-end traversal returns only workspace-A entities."""
    fx = _build_fixture()
    token_a, plaintext = _auth_token(workspace_id=_WS_A)
    factory = _session_factory(fx)

    app = _make_rest_app()
    app.state.db_session_factory = factory

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=token_a,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/entities/svc.shared/related?max_depth=2",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert response.status_code == 200, response.text
    data = response.json()

    related_names = {n["name"] for n in data["related"]}
    assert "svc.internal-a" in related_names
    assert "svc.internal-b" not in related_names
    for edge in data["edges"]:
        assert edge["from"] != "svc.internal-b"
        assert edge["to"] != "svc.internal-b"


# ---------------------------------------------------------------------------
# 4. MCP tool — end-to-end isolation at the tool helper level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tool_workspace_a_never_returns_b_entities() -> None:
    """The MCP ``get_related_entities`` helper enforces workspace scoping."""
    from omniscience_server.mcp.tools import mcp_get_related_entities

    fx = _build_fixture()
    factory = _session_factory(fx)

    app = FastAPI()
    app.state.db_session_factory = factory

    result = await mcp_get_related_entities(
        app=app,
        entity_name="svc.shared",
        workspace_id=_WS_A,
        max_depth=2,
    )

    related_names = {n["name"] for n in result["related"]}
    assert "svc.internal-a" in related_names
    assert "svc.internal-b" not in related_names
    for edge in result["edges"]:
        assert edge["from"] != "svc.internal-b"
        assert edge["to"] != "svc.internal-b"


@pytest.mark.asyncio
async def test_mcp_tool_workspace_b_never_returns_a_entities() -> None:
    """Symmetric MCP-layer check for workspace B."""
    from omniscience_server.mcp.tools import mcp_get_related_entities

    fx = _build_fixture()
    factory = _session_factory(fx)

    app = FastAPI()
    app.state.db_session_factory = factory

    result = await mcp_get_related_entities(
        app=app,
        entity_name="svc.shared",
        workspace_id=_WS_B,
        max_depth=2,
    )

    related_names = {n["name"] for n in result["related"]}
    assert "svc.internal-b" in related_names
    assert "svc.internal-a" not in related_names
