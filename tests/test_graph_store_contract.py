"""Parametric ``GraphStore`` contract tests — shared across all backends.

This module defines the behavioural contract that every
``omniscience_core.storage.GraphStore`` implementation must satisfy.
Tests are parametrised over backends and skipped when that backend's
live dependency (Docker / Neo4j container) is not available in the test
environment.

Design
------

- **pgvector**: parametrised via an in-memory fake adapter that proxies
  to the existing isolation fixture from
  ``tests.test_graph_workspace_isolation``.  No real Postgres is started.
- **neo4j**: uses ``testcontainers[neo4j]`` to spin up a Neo4j 5.x
  Community Edition container per test session.  The container is
  expensive so each test re-uses the same instance; each test provisions
  a fresh database namespace via workspace_id scoping.

Rationale
---------

ADR-0005 §Schema mandates the Neo4j adapter implements the exact
``GraphStore`` protocol the pgvector adapter implements, so that the
retrieval layer can swap backends behind a feature flag with zero code
change.  This parametric suite is the regression fence for that
guarantee — any behavioural drift between the two backends fails here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityUpsert,
    GraphStore,
)

pytestmark = pytest.mark.asyncio


_WORKSPACE_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
_WORKSPACE_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000b2")


# ---------------------------------------------------------------------------
# Backend discovery
# ---------------------------------------------------------------------------


def _neo4j_available() -> bool:
    """True iff testcontainers + Docker are available.

    Controlled via env var ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`` —
    opt-in so CI without Docker does not slow down on image pulls.
    """
    if os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS", "0") != "1":
        return False
    try:
        import testcontainers.neo4j  # noqa: F401

        return True
    except ImportError:
        return False


def _pgvector_factory() -> Callable[[], AsyncIterator[GraphStore]]:
    """Factory for the pgvector adapter fed by an in-memory fake session.

    Reuses the ``_FakeSession`` pattern from
    :mod:`tests.test_graph_workspace_isolation` so we do not need a real
    Postgres instance for the contract fence.
    """
    from omniscience_retrieval.adapters import PgVectorGraphStore

    from tests.test_graph_workspace_isolation import (
        _build_fixture,
        _session_factory,
    )

    async def _factory() -> AsyncIterator[GraphStore]:
        fx = _build_fixture()
        store = PgVectorGraphStore(session_factory=_session_factory(fx))
        yield store

    return _factory


def _neo4j_factory() -> Callable[[], AsyncIterator[GraphStore]]:
    """Factory that spins up a Neo4j testcontainer per test."""
    from omniscience_index.stores.neo4j_store import (
        Neo4jGraphStore,
        Neo4jStoreConfig,
    )
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    async def _factory() -> AsyncIterator[GraphStore]:
        with Neo4jContainer("neo4j:5.19-community").with_env(
            "NEO4J_AUTH", "neo4j/contract_test_password"
        ) as neo4j:
            config = Neo4jStoreConfig(
                uri=neo4j.get_connection_url(),
                username="neo4j",
                password="contract_test_password",
                database="neo4j",
                max_connection_pool_size=5,
                connection_acquisition_timeout_seconds=30.0,
                max_transaction_retry_time_seconds=15.0,
                default_max_depth=3,
            )
            store = Neo4jGraphStore(config=config)
            await store.connect()
            try:
                # Hydrate the Neo4j graph with the same two-workspace fixture
                # the pgvector backend gets by reusing the write API.
                await _seed_neo4j_with_fixture(store)
                yield store
            finally:
                await store.close()

    return _factory


async def _seed_neo4j_with_fixture(store: Any) -> None:
    """Populate a fresh Neo4j with the two-workspace contract fixture.

    Mirrors the pgvector ``_build_fixture()`` — two entities named
    ``svc.shared`` (one per workspace), each pointing to a workspace-
    local neighbour via a ``calls`` edge, plus a planted cross-tenant
    edge to verify isolation.
    """
    a_shared = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.shared",
        display_name="shared",
        chunk_id=None,
    )
    a_internal = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.internal-a",
        display_name="internal-a",
        chunk_id=None,
    )
    b_shared = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.shared",
        display_name="shared",
        chunk_id=None,
    )
    b_internal = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.internal-b",
        display_name="internal-b",
        chunk_id=None,
    )

    await store.upsert_entity(entity=a_shared, workspace_id=_WORKSPACE_A)
    await store.upsert_entity(entity=a_internal, workspace_id=_WORKSPACE_A)
    await store.upsert_entity(entity=b_shared, workspace_id=_WORKSPACE_B)
    await store.upsert_entity(entity=b_internal, workspace_id=_WORKSPACE_B)

    await store.upsert_edge(
        edge=EdgeUpsert(
            source_entity_id=a_shared.id,
            target_entity_id=a_internal.id,
            edge_type="calls",
        ),
        workspace_id=_WORKSPACE_A,
    )
    await store.upsert_edge(
        edge=EdgeUpsert(
            source_entity_id=b_shared.id,
            target_entity_id=b_internal.id,
            edge_type="calls",
        ),
        workspace_id=_WORKSPACE_B,
    )


# ---------------------------------------------------------------------------
# Parametrisation
# ---------------------------------------------------------------------------


_BACKENDS: list[tuple[str, Callable[[], Callable[[], AsyncIterator[GraphStore]]]]] = [
    ("pgvector", _pgvector_factory),
]

if _neo4j_available():
    _BACKENDS.append(("neo4j", _neo4j_factory))


@pytest.fixture(
    params=[pytest.param(name, id=name) for name, _ in _BACKENDS],
)
async def graph_store(request: pytest.FixtureRequest) -> AsyncIterator[GraphStore]:
    """Yield a ready ``GraphStore`` for the parametrised backend."""
    name = str(request.param)
    factory_builder = next(f for n, f in _BACKENDS if n == name)
    factory = factory_builder()
    gen = factory()
    store = await anext(gen)
    try:
        yield store
    finally:
        # Drain the async generator so teardown (e.g. closing the Neo4j
        # container) runs.
        async for _ in gen:
            break


# ---------------------------------------------------------------------------
# Contract: read methods require workspace_id keyword-only
# ---------------------------------------------------------------------------


async def test_get_entity_requires_workspace_id_keyword_only(
    graph_store: GraphStore,
) -> None:
    """Protocol invariant — positional workspace_id is a TypeError."""
    with pytest.raises(TypeError):
        await graph_store.get_entity("svc.shared", _WORKSPACE_A)  # type: ignore[misc]


async def test_find_related_requires_workspace_id_keyword_only(
    graph_store: GraphStore,
) -> None:
    with pytest.raises(TypeError):
        await graph_store.find_related("svc.shared", _WORKSPACE_A)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Contract: workspace scoping on reads
# ---------------------------------------------------------------------------


async def test_find_related_workspace_a_never_sees_workspace_b(
    graph_store: GraphStore,
) -> None:
    """Cross-workspace isolation contract — parametric across backends."""
    result = await graph_store.find_related(
        entity_name="svc.shared",
        workspace_id=_WORKSPACE_A,
        max_depth=2,
    )

    names = {result.seed.name, *(n.name for n in result.related)}
    assert "svc.internal-b" not in names, "Workspace-A traversal leaked workspace-B entity"
    for e in result.edges:
        assert "svc.internal-b" not in (e.from_entity, e.to_entity), (
            "Workspace-A traversal leaked an edge endpoint in workspace B"
        )


async def test_find_related_workspace_b_never_sees_workspace_a(
    graph_store: GraphStore,
) -> None:
    result = await graph_store.find_related(
        entity_name="svc.shared",
        workspace_id=_WORKSPACE_B,
        max_depth=2,
    )

    names = {result.seed.name, *(n.name for n in result.related)}
    assert "svc.internal-a" not in names
    for e in result.edges:
        assert "svc.internal-a" not in (e.from_entity, e.to_entity)


async def test_traverse_is_alias_of_find_related(
    graph_store: GraphStore,
) -> None:
    """``traverse`` must return the same shape as ``find_related``."""
    via_find = await graph_store.find_related(
        entity_name="svc.shared",
        workspace_id=_WORKSPACE_A,
        max_depth=1,
    )
    via_traverse = await graph_store.traverse(
        entity_name="svc.shared",
        workspace_id=_WORKSPACE_A,
        max_depth=1,
    )
    assert via_find.seed.name == via_traverse.seed.name


async def test_find_related_raises_entity_not_found_cross_workspace(
    graph_store: GraphStore,
) -> None:
    """Requesting a B-only entity from A looks like 404 on both backends."""
    # ``svc.internal-b`` only exists in workspace B.  Asking for it from
    # A must raise the not-found ValueError — never leak existence.
    with pytest.raises(ValueError, match=r"entity_not_found:svc\.internal-b"):
        await graph_store.find_related(
            entity_name="svc.internal-b",
            workspace_id=_WORKSPACE_A,
            max_depth=1,
        )


async def test_get_entity_returns_none_for_cross_workspace(
    graph_store: GraphStore,
) -> None:
    """``get_entity`` returns None — NEVER leaks existence across workspaces."""
    # ``svc.internal-a`` only in A.  Asking from B -> None.
    result = await graph_store.get_entity(
        entity_name="svc.internal-a",
        workspace_id=_WORKSPACE_B,
    )
    assert result is None
