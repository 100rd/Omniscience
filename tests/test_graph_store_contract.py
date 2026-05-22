"""Parametric ``GraphStore`` contract tests — shared across all backends.

This module defines the behavioural contract that every
``omniscience_core.storage.GraphStore`` implementation must satisfy.
Tests are parametrised over backends and skipped when that backend's
live dependency (Docker / Neo4j container) is not available in the test
environment.

Design
------

- **neo4j**: uses ``testcontainers[neo4j]`` to spin up a Neo4j 5.x
  Community Edition container per test session.  The container is
  expensive so each test re-uses the same instance; each test provisions
  a fresh database namespace via workspace_id scoping.

Historical note
---------------

Prior to v0.2 this file also parametrised the ``pgvector`` lane
against an in-memory fake adapter.  That lane was removed at the
#105 cutover alongside the ``PgVectorGraphStore`` class itself.
Only the Neo4j backend is supported going forward (ADR-0005).

Rationale
---------

ADR-0005 §Schema mandates that the Neo4j adapter fulfils the
``GraphStore`` protocol exactly, so that the retrieval layer can
continue to depend on the protocol rather than any backend.  This
suite is the regression fence for that guarantee — any behavioural
drift fails here.
"""

from __future__ import annotations

import shutil
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    """True iff testcontainers + a reachable Docker daemon are present.

    Issue #226 — this gate USED TO be opt-in via
    ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1``, which silently skipped
    the writer contract tests in CI for over a month and let the
    metadata-as-Map bug ship.  We mirror the pattern from
    ``tests/integration/test_neo4j_bootstrap_schema.py`` (PR #227):
    skip only when the live dependency genuinely cannot be reached,
    never on operator opt-in.
    """
    try:
        import testcontainers.neo4j  # noqa: F401
    except ImportError:
        return False
    if shutil.which("docker") is None:
        return False
    return _docker_daemon_reachable()


def _docker_daemon_reachable() -> bool:
    """Ping the Docker daemon endpoint; returns False on any failure.

    testcontainers itself will raise on a missing/unreachable daemon
    when the fixture spins up; pre-checking here keeps the pytest
    output clean (skip vs. error) and matches the pattern used by
    ``tests/integration/test_neo4j_bootstrap_schema.py``.
    """
    import os

    host = os.environ.get("DOCKER_HOST")
    if not host:
        return Path("/var/run/docker.sock").exists()
    parsed = urlparse(host)
    if parsed.scheme in ("unix", ""):
        return Path(parsed.path or host).exists()
    if parsed.hostname is None or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1.0):
            return True
    except OSError:
        return False


def _neo4j_factory() -> Callable[[], AsyncIterator[GraphStore]]:
    """Factory that spins up a Neo4j testcontainer per test."""
    from omniscience_index.stores.neo4j_store import (
        Neo4jGraphStore,
        Neo4jStoreConfig,
    )
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    async def _factory() -> AsyncIterator[GraphStore]:
        # Pass the password through the constructor — the wrapper writes
        # it into NEO4J_AUTH inside ``_configure`` and threads it back
        # through ``get_driver()``.  Using ``.with_env("NEO4J_AUTH", ...)``
        # here is silently overridden by the wrapper's own configure step
        # (the same trap the bootstrap test in PR #227 documents).
        with Neo4jContainer(
            "neo4j:5.19-community", password="contract_test_password"
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
                await _seed_neo4j_with_fixture(store)
                yield store
            finally:
                await store.close()

    return _factory


async def _seed_neo4j_with_fixture(store: Any) -> None:
    """Populate a fresh Neo4j with the two-workspace contract fixture.

    Two entities named ``svc.shared`` (one per workspace), each pointing
    to a workspace-local neighbour via a ``calls`` edge.  The parallel
    structure across workspaces is what makes the isolation assertions
    meaningful (a leak would surface the *other* workspace's neighbour).
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


_BACKENDS: list[tuple[str, Callable[[], Callable[[], AsyncIterator[GraphStore]]]]] = []

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
    result = await graph_store.get_entity(
        entity_name="svc.internal-a",
        workspace_id=_WORKSPACE_B,
    )
    assert result is None
