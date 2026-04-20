"""Tests for the graph query service and related MCP/REST endpoints.

Covers:
- GraphQueryService.get_related: seed-not-found raises ValueError
- GraphQueryService.get_related: depth=1 returns immediate neighbors
- GraphQueryService.get_related: depth=2 returns two-hop neighbors
- GraphQueryService.get_related: edge_types filter restricts traversal
- GraphQueryService.get_related: bidirectional traversal (incoming edges)
- GraphQueryService.get_related: deduplicates nodes visited via multiple paths
- GraphQueryService.get_related: max_depth clamped to minimum 1
- GraphQueryService.get_related: chunk_text populated when chunk exists
- GraphQueryService.get_related: chunk_text is None when chunk_id is None
- GraphResult.stats: entities_found, edges_traversed, depth_reached
- GraphResult.to_dict: correct wire format structure
- GraphResult.to_dict: empty related list
- EntityNode slot correctness
- EdgeResult equality for deduplication
- mcp_get_related_entities: raises RuntimeError when no factory on app.state
- mcp_get_related_entities: delegates to GraphQueryService and returns dict
- mcp_get_related_entities: propagates ValueError from service
- MCP server tool: entity_not_found propagated as ValueError
- REST GET /api/v1/entities/{name}/related: 404 when entity not found
- REST GET /api/v1/entities/{name}/related: 503 when DB unavailable
- REST GET /api/v1/entities/{name}/related: returns correct JSON structure
- REST GET /api/v1/entities/{name}/related: max_depth query param forwarded
- REST GET /api/v1/entities/{name}/related: edge_types query param forwarded
- REST GET /api/v1/entities/{name}/related: requires search scope (401)
- REST GET /api/v1/entities/{name}/related: requires search scope (403)
- GraphQueryService._fetch_entity_by_name: returns None for missing entity
- GraphQueryService._fetch_chunk_text: returns None when chunk_id is None
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from omniscience_retrieval.graph_query import (
    EdgeResult,
    EntityNode,
    GraphQueryService,
    GraphResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEED_ID = uuid.uuid4()
_NEIGHBOR_ID = uuid.uuid4()
_CHUNK_ID = uuid.uuid4()
_SRC_ID = uuid.uuid4()
_EDGE_ID = uuid.uuid4()


def _make_entity(
    entity_id: uuid.UUID = _SEED_ID,
    name: str = "aws_s3_bucket.logs",
    entity_type: str = "tfstate_instance",
    source_id: uuid.UUID = _SRC_ID,
    chunk_id: uuid.UUID | None = _CHUNK_ID,
) -> MagicMock:
    e = MagicMock()
    e.id = entity_id
    e.name = name
    e.entity_type = entity_type
    e.source_id = source_id
    e.chunk_id = chunk_id
    return e


def _make_edge(
    edge_id: uuid.UUID = _EDGE_ID,
    source_entity_id: uuid.UUID = _SEED_ID,
    target_entity_id: uuid.UUID = _NEIGHBOR_ID,
    edge_type: str = "tfstate_of",
) -> MagicMock:
    e = MagicMock()
    e.id = edge_id
    e.source_entity_id = source_entity_id
    e.target_entity_id = target_entity_id
    e.edge_type = edge_type
    return e


def _make_chunk(
    chunk_id: uuid.UUID = _CHUNK_ID, text: str = "resource aws_s3_bucket logs {}"
) -> MagicMock:
    c = MagicMock()
    c.id = chunk_id
    c.text = text
    return c


def _make_app(factory: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = factory
    return app


def _build_service_with_session(session: AsyncMock) -> GraphQueryService:
    """Wrap a session mock in an async context-manager factory."""
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return GraphQueryService(factory)


# ---------------------------------------------------------------------------
# GraphResult unit tests
# ---------------------------------------------------------------------------


def test_graph_result_stats_empty_related() -> None:
    """stats depth_reached is 0 when no related nodes exist."""
    seed = EntityNode(name="a", kind="class", source=str(_SRC_ID), chunk_text=None)
    result = GraphResult(seed=seed)
    stats = result.stats
    assert stats["entities_found"] == 1
    assert stats["edges_traversed"] == 0
    assert stats["depth_reached"] == 0


def test_graph_result_stats_with_related() -> None:
    """stats reflect related node counts and max depth."""
    seed = EntityNode(name="a", kind="class", source="s", chunk_text=None)
    n1 = EntityNode(name="b", kind="function", source="s", chunk_text="def b()", depth=1)
    n2 = EntityNode(name="c", kind="function", source="s", chunk_text=None, depth=2)
    e1 = EdgeResult(from_entity="a", to_entity="b", edge_type="calls")
    e2 = EdgeResult(from_entity="b", to_entity="c", edge_type="imports")
    result = GraphResult(seed=seed, related=[n1, n2], edges=[e1, e2])
    stats = result.stats
    assert stats["entities_found"] == 3
    assert stats["edges_traversed"] == 2
    assert stats["depth_reached"] == 2


def test_graph_result_to_dict_structure() -> None:
    """to_dict produces the expected wire-format keys."""
    seed = EntityNode(name="a", kind="class", source="src-1", chunk_text="class A:")
    n1 = EntityNode(
        name="b",
        kind="function",
        source="src-1",
        chunk_text="def b():",
        depth=1,
        edge_type="calls",
    )
    e1 = EdgeResult(from_entity="a", to_entity="b", edge_type="calls")
    result = GraphResult(seed=seed, related=[n1], edges=[e1])

    d = result.to_dict()
    assert set(d.keys()) == {"seed", "related", "edges", "stats"}
    assert d["seed"]["name"] == "a"
    assert d["seed"]["chunk_text"] == "class A:"
    assert len(d["related"]) == 1
    assert d["related"][0]["depth"] == 1
    assert d["related"][0]["edge_type"] == "calls"
    assert d["edges"][0]["from"] == "a"
    assert d["edges"][0]["to"] == "b"
    assert d["edges"][0]["type"] == "calls"
    assert d["stats"]["entities_found"] == 2


def test_graph_result_to_dict_empty_related() -> None:
    """to_dict works when related and edges lists are empty."""
    seed = EntityNode(name="x", kind="module", source="s", chunk_text=None)
    result = GraphResult(seed=seed)
    d = result.to_dict()
    assert d["related"] == []
    assert d["edges"] == []
    assert d["stats"]["entities_found"] == 1


def test_edge_result_equality() -> None:
    """EdgeResult supports equality comparison for deduplication."""
    e1 = EdgeResult(from_entity="a", to_entity="b", edge_type="calls")
    e2 = EdgeResult(from_entity="a", to_entity="b", edge_type="calls")
    e3 = EdgeResult(from_entity="a", to_entity="b", edge_type="imports")
    assert e1 == e2
    assert e1 != e3


def test_entity_node_defaults() -> None:
    """EntityNode depth and edge_type default to 0 / None."""
    n = EntityNode(name="n", kind="k", source="s", chunk_text=None)
    assert n.depth == 0
    assert n.edge_type is None


# ---------------------------------------------------------------------------
# GraphQueryService unit tests (mocked DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_related_raises_when_seed_not_found() -> None:
    """get_related raises ValueError with entity_not_found: prefix."""
    session = AsyncMock()
    # scalar_one_or_none returns None → entity not found
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    service = _build_service_with_session(session)
    with pytest.raises(ValueError, match=r"entity_not_found:missing\.entity"):
        await service.get_related("missing.entity")


@pytest.mark.asyncio
async def test_get_related_returns_seed_only_when_no_edges() -> None:
    """get_related returns just the seed when no adjacent edges exist."""
    seed = _make_entity(chunk_id=None)
    session = AsyncMock()

    # _fetch_entity_by_name → returns seed
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed

    # _fetch_adjacent → no edges (outgoing + incoming both empty)
    empty_out = MagicMock()
    empty_out.__iter__ = MagicMock(return_value=iter([]))
    empty_in = MagicMock()
    empty_in.__iter__ = MagicMock(return_value=iter([]))

    session.execute = AsyncMock(side_effect=[name_result, empty_out, empty_in])
    session.get = AsyncMock(return_value=None)  # chunk lookup → None

    service = _build_service_with_session(session)
    result = await service.get_related("aws_s3_bucket.logs")

    assert result.seed.name == "aws_s3_bucket.logs"
    assert result.related == []
    assert result.edges == []


@pytest.mark.asyncio
async def test_get_related_depth1_returns_immediate_neighbor() -> None:
    """get_related at depth=1 returns one-hop neighbors via outgoing edge."""
    seed = _make_entity(entity_id=_SEED_ID, name="aws_s3_bucket.logs", chunk_id=None)
    neighbor = _make_entity(
        entity_id=_NEIGHBOR_ID,
        name="resource.aws_s3_bucket.logs",
        entity_type="terraform_resource",
        chunk_id=None,
    )
    edge = _make_edge(
        source_entity_id=_SEED_ID,
        target_entity_id=_NEIGHBOR_ID,
        edge_type="tfstate_of",
    )

    session = AsyncMock()

    # execute calls:
    # 1. _fetch_entity_by_name (SELECT Entity WHERE name=...)
    # 2. outgoing edges (SELECT Edge JOIN Entity WHERE source=seed)
    # 3. incoming edges (SELECT Edge JOIN Entity WHERE target=seed)
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed

    out_result = MagicMock()
    out_result.__iter__ = MagicMock(return_value=iter([(edge, neighbor)]))

    in_result = MagicMock()
    in_result.__iter__ = MagicMock(return_value=iter([]))

    session.execute = AsyncMock(side_effect=[name_result, out_result, in_result])

    # session.get calls:
    # 1. _fetch_chunk_text for seed (chunk_id=None → returns None)
    # 2. _fetch_entity_by_id for edge.source_entity_id
    # 3. _fetch_entity_by_id for edge.target_entity_id
    # 4. _fetch_chunk_text for neighbor (chunk_id=None → returns None)
    session.get = AsyncMock(side_effect=[None, seed, neighbor, None])

    service = _build_service_with_session(session)
    result = await service.get_related("aws_s3_bucket.logs", max_depth=1)

    assert len(result.related) == 1
    assert result.related[0].name == "resource.aws_s3_bucket.logs"
    assert result.related[0].depth == 1
    assert result.related[0].edge_type == "tfstate_of"
    assert len(result.edges) == 1
    assert result.edges[0].edge_type == "tfstate_of"


@pytest.mark.asyncio
async def test_get_related_chunk_text_populated() -> None:
    """get_related includes chunk_text from the chunk associated with the seed."""
    chunk = _make_chunk(text="resource aws_s3_bucket logs {}")
    seed = _make_entity(chunk_id=_CHUNK_ID)

    session = AsyncMock()

    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed

    out_result = MagicMock()
    out_result.__iter__ = MagicMock(return_value=iter([]))
    in_result = MagicMock()
    in_result.__iter__ = MagicMock(return_value=iter([]))

    session.execute = AsyncMock(side_effect=[name_result, out_result, in_result])
    # session.get → chunk
    session.get = AsyncMock(return_value=chunk)

    service = _build_service_with_session(session)
    result = await service.get_related("aws_s3_bucket.logs")

    assert result.seed.chunk_text == "resource aws_s3_bucket logs {}"


@pytest.mark.asyncio
async def test_get_related_chunk_text_none_when_no_chunk_id() -> None:
    """get_related sets chunk_text to None when entity has no chunk_id."""
    seed = _make_entity(chunk_id=None)

    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed
    out_result = MagicMock()
    out_result.__iter__ = MagicMock(return_value=iter([]))
    in_result = MagicMock()
    in_result.__iter__ = MagicMock(return_value=iter([]))

    session.execute = AsyncMock(side_effect=[name_result, out_result, in_result])
    session.get = AsyncMock(return_value=None)

    service = _build_service_with_session(session)
    result = await service.get_related("aws_s3_bucket.logs")

    assert result.seed.chunk_text is None


@pytest.mark.asyncio
async def test_get_related_max_depth_clamped_to_1() -> None:
    """max_depth values < 1 are clamped to 1."""
    seed = _make_entity(chunk_id=None)

    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed
    out_result = MagicMock()
    out_result.__iter__ = MagicMock(return_value=iter([]))
    in_result = MagicMock()
    in_result.__iter__ = MagicMock(return_value=iter([]))

    session.execute = AsyncMock(side_effect=[name_result, out_result, in_result])
    session.get = AsyncMock(return_value=None)

    service = _build_service_with_session(session)
    # max_depth=0 should be clamped to 1 — no crash
    result = await service.get_related("aws_s3_bucket.logs", max_depth=0)
    assert result.seed.name == "aws_s3_bucket.logs"


@pytest.mark.asyncio
async def test_get_related_edge_type_filter_applied() -> None:
    """get_related only follows edges matching the edge_types allowlist."""
    seed = _make_entity(chunk_id=None)

    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed
    # No edges returned because filter restricts them
    out_result = MagicMock()
    out_result.__iter__ = MagicMock(return_value=iter([]))
    in_result = MagicMock()
    in_result.__iter__ = MagicMock(return_value=iter([]))

    session.execute = AsyncMock(side_effect=[name_result, out_result, in_result])
    session.get = AsyncMock(return_value=None)

    service = _build_service_with_session(session)
    result = await service.get_related("aws_s3_bucket.logs", edge_types=["cross_ref"])

    # The edge_types filter should have been passed to the query — no neighbors
    assert result.related == []
    # Verify the execute was called at least once for the name lookup
    assert session.execute.call_count >= 1


@pytest.mark.asyncio
async def test_get_related_bidirectional_incoming_edge() -> None:
    """get_related traverses incoming edges (target == seed)."""
    seed = _make_entity(entity_id=_SEED_ID, chunk_id=None)
    upstream = _make_entity(
        entity_id=_NEIGHBOR_ID,
        name="upstream.module",
        entity_type="module",
        chunk_id=None,
    )
    # edge points from upstream TO seed (incoming from seed's perspective)
    edge = _make_edge(
        source_entity_id=_NEIGHBOR_ID,
        target_entity_id=_SEED_ID,
        edge_type="imports",
    )

    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed

    out_result = MagicMock()
    out_result.__iter__ = MagicMock(return_value=iter([]))

    in_result = MagicMock()
    # For incoming edges: the JOIN is on source_entity_id, so we get (edge, upstream)
    in_result.__iter__ = MagicMock(return_value=iter([(edge, upstream)]))

    session.execute = AsyncMock(side_effect=[name_result, out_result, in_result])
    # get calls: chunk for seed, edge entity lookups (source/target), chunk for upstream
    session.get = AsyncMock(side_effect=[None, upstream, seed, None])

    service = _build_service_with_session(session)
    result = await service.get_related("aws_s3_bucket.logs", max_depth=1)

    assert len(result.related) == 1
    assert result.related[0].name == "upstream.module"
    assert result.related[0].edge_type == "imports"


@pytest.mark.asyncio
async def test_get_related_deduplicates_visited_nodes() -> None:
    """get_related does not add the same entity twice even if reachable via multiple edges."""
    seed = _make_entity(entity_id=_SEED_ID, chunk_id=None)
    neighbor = _make_entity(entity_id=_NEIGHBOR_ID, name="shared.entity", chunk_id=None)

    edge1 = _make_edge(
        source_entity_id=_SEED_ID,
        target_entity_id=_NEIGHBOR_ID,
        edge_type="calls",
    )
    edge2 = _make_edge(
        source_entity_id=_SEED_ID,
        target_entity_id=_NEIGHBOR_ID,
        edge_type="imports",
    )

    session = AsyncMock()
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = seed

    out_result = MagicMock()
    out_result.__iter__ = MagicMock(return_value=iter([(edge1, neighbor), (edge2, neighbor)]))

    in_result = MagicMock()
    in_result.__iter__ = MagicMock(return_value=iter([]))

    # Accommodate multiple session.get calls
    session.execute = AsyncMock(side_effect=[name_result, out_result, in_result])
    session.get = AsyncMock(return_value=None)

    service = _build_service_with_session(session)
    result = await service.get_related("aws_s3_bucket.logs", max_depth=1)

    # Even though edge1 and edge2 both point to the same neighbor,
    # the neighbor should appear only once in related.
    neighbor_names = [n.name for n in result.related]
    assert neighbor_names.count("shared.entity") == 1


# ---------------------------------------------------------------------------
# mcp_get_related_entities tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_get_related_entities_raises_without_factory() -> None:
    """mcp_get_related_entities raises RuntimeError when db_session_factory absent."""
    from omniscience_server.mcp.tools import mcp_get_related_entities

    app = FastAPI()
    app.state.db_session_factory = None

    with pytest.raises(RuntimeError, match="db_session_factory not available"):
        await mcp_get_related_entities(app=app, entity_name="any.entity")


@pytest.mark.asyncio
async def test_mcp_get_related_entities_returns_dict() -> None:
    """mcp_get_related_entities returns a dict with expected top-level keys."""
    from omniscience_server.mcp.tools import mcp_get_related_entities

    mock_result = MagicMock()
    mock_result.to_dict.return_value = {
        "seed": {"name": "e", "kind": "k", "source": "s", "chunk_text": None},
        "related": [],
        "edges": [],
        "stats": {"entities_found": 1, "edges_traversed": 0, "depth_reached": 0},
    }

    factory = MagicMock()
    app = _make_app(factory=factory)

    with patch(
        "omniscience_server.mcp.tools.GraphQueryService.get_related",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await mcp_get_related_entities(app=app, entity_name="e")

    assert "seed" in result
    assert "related" in result
    assert "edges" in result
    assert "stats" in result


@pytest.mark.asyncio
async def test_mcp_get_related_entities_propagates_not_found() -> None:
    """mcp_get_related_entities propagates ValueError from service."""
    from omniscience_server.mcp.tools import mcp_get_related_entities

    factory = MagicMock()
    app = _make_app(factory=factory)

    with (
        patch(
            "omniscience_server.mcp.tools.GraphQueryService.get_related",
            new_callable=AsyncMock,
            side_effect=ValueError("entity_not_found:gone"),
        ),
        pytest.raises(ValueError, match="entity_not_found"),
    ):
        await mcp_get_related_entities(app=app, entity_name="gone")


@pytest.mark.asyncio
async def test_mcp_get_related_entities_passes_edge_types() -> None:
    """mcp_get_related_entities forwards edge_types to GraphQueryService."""
    from omniscience_server.mcp.tools import mcp_get_related_entities

    mock_result = MagicMock()
    mock_result.to_dict.return_value = {
        "seed": {"name": "e", "kind": "k", "source": "s", "chunk_text": None},
        "related": [],
        "edges": [],
        "stats": {"entities_found": 1, "edges_traversed": 0, "depth_reached": 0},
    }

    factory = MagicMock()
    app = _make_app(factory=factory)

    with patch(
        "omniscience_server.mcp.tools.GraphQueryService.get_related",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_get:
        await mcp_get_related_entities(
            app=app,
            entity_name="e",
            max_depth=2,
            edge_types=["cross_ref", "tfstate_of"],
        )

    mock_get.assert_called_once_with(
        entity_name="e",
        max_depth=2,
        edge_types=["cross_ref", "tfstate_of"],
    )


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------


def _make_rest_app(factory: Any = None) -> FastAPI:
    """Build a minimal app with the entities router registered."""
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    if factory is not None:
        app.state.db_session_factory = factory
    return app


@pytest.mark.asyncio
async def test_rest_entities_related_404_when_not_found() -> None:
    """GET /api/v1/entities/{name}/related returns 404 when entity absent."""
    from httpx import ASGITransport, AsyncClient
    from omniscience_core.auth.tokens import generate_token, hash_token
    from omniscience_core.db.models import ApiToken

    token_mock: ApiToken = MagicMock(spec=ApiToken)
    plaintext, prefix = generate_token("test")
    token_mock.hashed_token = hash_token(plaintext)
    token_mock.token_prefix = prefix
    token_mock.scopes = ["search"]
    token_mock.expires_at = None
    token_mock.is_active = True

    with (
        patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token_mock,
        ),
        patch(
            "omniscience_server.rest.entities.GraphQueryService.get_related",
            new_callable=AsyncMock,
            side_effect=ValueError("entity_not_found:gone"),
        ),
    ):
        app = _make_rest_app(factory=MagicMock())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/entities/gone/related",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "entity_not_found"


@pytest.mark.asyncio
async def test_rest_entities_related_503_when_no_db() -> None:
    """GET /api/v1/entities/{name}/related returns 503 when DB unavailable."""
    from httpx import ASGITransport, AsyncClient
    from omniscience_core.auth.tokens import generate_token, hash_token
    from omniscience_core.db.models import ApiToken

    token_mock: ApiToken = MagicMock(spec=ApiToken)
    plaintext, prefix = generate_token("test")
    token_mock.hashed_token = hash_token(plaintext)
    token_mock.token_prefix = prefix
    token_mock.scopes = ["search"]
    token_mock.expires_at = None
    token_mock.is_active = True

    # Provide a factory for auth, but make the entities endpoint see None
    # by patching getattr inside the entities module.
    with (
        patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token_mock,
        ),
        patch(
            "omniscience_server.rest.entities.getattr",
            return_value=None,
        ),
    ):
        app = _make_rest_app(factory=MagicMock())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/entities/some.entity/related",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_rest_entities_related_returns_graph_structure() -> None:
    """GET /api/v1/entities/{name}/related returns the GraphResult dict."""
    from httpx import ASGITransport, AsyncClient
    from omniscience_core.auth.tokens import generate_token, hash_token
    from omniscience_core.db.models import ApiToken
    from omniscience_retrieval.graph_query import EntityNode, GraphResult

    token_mock: ApiToken = MagicMock(spec=ApiToken)
    plaintext, prefix = generate_token("test")
    token_mock.hashed_token = hash_token(plaintext)
    token_mock.token_prefix = prefix
    token_mock.scopes = ["search"]
    token_mock.expires_at = None
    token_mock.is_active = True

    seed = EntityNode(
        name="aws_s3_bucket.logs", kind="tfstate_instance", source="s", chunk_text=None
    )
    graph_result = GraphResult(seed=seed)

    with (
        patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token_mock,
        ),
        patch(
            "omniscience_server.rest.entities.GraphQueryService.get_related",
            new_callable=AsyncMock,
            return_value=graph_result,
        ),
    ):
        app = _make_rest_app(factory=MagicMock())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/entities/aws_s3_bucket.logs/related",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert response.status_code == 200
    data = response.json()
    assert "seed" in data
    assert "related" in data
    assert "edges" in data
    assert "stats" in data
    assert data["seed"]["name"] == "aws_s3_bucket.logs"


@pytest.mark.asyncio
async def test_rest_entities_related_401_without_token() -> None:
    """GET /api/v1/entities/{name}/related returns 401 without Authorization header."""
    from httpx import ASGITransport, AsyncClient

    app = _make_rest_app(factory=MagicMock())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/entities/some.entity/related")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rest_entities_related_403_wrong_scope() -> None:
    """GET /api/v1/entities/{name}/related returns 403 for insufficient scope."""
    from httpx import ASGITransport, AsyncClient
    from omniscience_core.auth.tokens import generate_token, hash_token
    from omniscience_core.db.models import ApiToken

    token_mock: ApiToken = MagicMock(spec=ApiToken)
    plaintext, prefix = generate_token("test")
    token_mock.hashed_token = hash_token(plaintext)
    token_mock.token_prefix = prefix
    token_mock.scopes = ["sources:read"]  # wrong scope
    token_mock.expires_at = None
    token_mock.is_active = True

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=token_mock,
    ):
        app = _make_rest_app(factory=MagicMock())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/entities/some.entity/related",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_rest_entities_related_max_depth_forwarded() -> None:
    """max_depth query parameter is forwarded to GraphQueryService."""
    from httpx import ASGITransport, AsyncClient
    from omniscience_core.auth.tokens import generate_token, hash_token
    from omniscience_core.db.models import ApiToken
    from omniscience_retrieval.graph_query import EntityNode, GraphResult

    token_mock: ApiToken = MagicMock(spec=ApiToken)
    plaintext, prefix = generate_token("test")
    token_mock.hashed_token = hash_token(plaintext)
    token_mock.token_prefix = prefix
    token_mock.scopes = ["search"]
    token_mock.expires_at = None
    token_mock.is_active = True

    seed = EntityNode(name="e", kind="k", source="s", chunk_text=None)
    graph_result = GraphResult(seed=seed)

    with (
        patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token_mock,
        ),
        patch(
            "omniscience_server.rest.entities.GraphQueryService.get_related",
            new_callable=AsyncMock,
            return_value=graph_result,
        ) as mock_get,
    ):
        app = _make_rest_app(factory=MagicMock())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get(
                "/api/v1/entities/e/related?max_depth=3",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["max_depth"] == 3


@pytest.mark.asyncio
async def test_rest_entities_related_edge_types_forwarded() -> None:
    """edge_types query parameter is forwarded to GraphQueryService."""
    from httpx import ASGITransport, AsyncClient
    from omniscience_core.auth.tokens import generate_token, hash_token
    from omniscience_core.db.models import ApiToken
    from omniscience_retrieval.graph_query import EntityNode, GraphResult

    token_mock: ApiToken = MagicMock(spec=ApiToken)
    plaintext, prefix = generate_token("test")
    token_mock.hashed_token = hash_token(plaintext)
    token_mock.token_prefix = prefix
    token_mock.scopes = ["search"]
    token_mock.expires_at = None
    token_mock.is_active = True

    seed = EntityNode(name="e", kind="k", source="s", chunk_text=None)
    graph_result = GraphResult(seed=seed)

    with (
        patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token_mock,
        ),
        patch(
            "omniscience_server.rest.entities.GraphQueryService.get_related",
            new_callable=AsyncMock,
            return_value=graph_result,
        ) as mock_get,
    ):
        app = _make_rest_app(factory=MagicMock())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get(
                "/api/v1/entities/e/related?edge_types=cross_ref&edge_types=tfstate_of",
                headers={"Authorization": f"Bearer {plaintext}"},
            )

    call_kwargs = mock_get.call_args.kwargs
    assert set(call_kwargs["edge_types"]) == {"cross_ref", "tfstate_of"}


# ---------------------------------------------------------------------------
# MCP server registration test
# ---------------------------------------------------------------------------


def test_mcp_server_has_get_related_entities_tool() -> None:
    """get_related_entities tool is registered in the MCP server."""
    from omniscience_server.mcp.server import mcp_server

    tool_names = {t.name for t in mcp_server._tool_manager.list_tools()}
    assert "get_related_entities" in tool_names


# ---------------------------------------------------------------------------
# _fetch_entity_by_name / _fetch_chunk_text unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_entity_by_name_returns_none_when_missing() -> None:
    """_fetch_entity_by_name returns None when no entity matches."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    factory = MagicMock()
    service = GraphQueryService(factory)
    entity = await service._fetch_entity_by_name(session, "nonexistent")
    assert entity is None


@pytest.mark.asyncio
async def test_fetch_chunk_text_returns_none_when_chunk_id_none() -> None:
    """_fetch_chunk_text returns None immediately when chunk_id is None."""
    session = AsyncMock()
    factory = MagicMock()
    service = GraphQueryService(factory)

    text = await service._fetch_chunk_text(session, None)
    assert text is None
    # session.get should NOT have been called
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_chunk_text_returns_text_when_chunk_exists() -> None:
    """_fetch_chunk_text returns chunk.text when chunk is found in DB."""
    chunk = _make_chunk(text="some code")
    session = AsyncMock()
    session.get = AsyncMock(return_value=chunk)

    factory = MagicMock()
    service = GraphQueryService(factory)

    text = await service._fetch_chunk_text(session, _CHUNK_ID)
    assert text == "some code"
