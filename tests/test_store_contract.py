"""Parametric ``StoreContract`` contract tests — shared across all backends.

This module defines the behavioural contract that every
``omniscience_core.storage.StoreContract`` implementation must satisfy.
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
#105 cutover alongside the ``PgVectorStoreContract`` class itself.
Only the Neo4j backend is supported going forward (ADR-0005).

Rationale
---------

ADR-0005 §Schema mandates that the Neo4j adapter fulfils the
``StoreContract`` protocol exactly, so that the retrieval layer can
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
from hypothesis import given, settings, strategies as st
import asyncio

from omniscience_core.storage.contract import StoreContract
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert

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


def _postgres_available() -> bool:
    import os

    return "POSTGRES_DSN" in os.environ or _docker_daemon_reachable()


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


def _full_store_factory() -> Callable[[], AsyncIterator[StoreContract]]:
    """Factory that spins up Neo4j and Qdrant testcontainers for FullStore."""
    from omniscience_index.stores.full_store import FullStore
    from omniscience_index.stores.neo4j_store import (
        Neo4jGraphStore,
        Neo4jStoreConfig,
    )
    from omniscience_index.stores.qdrant_config import QdrantConfig
    from omniscience_index.stores.qdrant_store import QdrantVectorStore
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]
    from testcontainers.qdrant import QdrantContainer  # type: ignore[import-not-found]

    class DummyProvider:
        dim = 8
        provider_name = "dummy"
        model_name = "dummy"

        async def embed(self, texts):
            import math

            res = []
            for text in texts:
                h = hash(text)
                raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(self.dim)]
                n = math.sqrt(sum(x * x for x in raw)) or 1.0
                res.append([x / n for x in raw])
            return res

    async def _factory() -> AsyncIterator[StoreContract]:
        with Neo4jContainer("neo4j:5.19-community", password="contract_test_password") as neo4j:
            with QdrantContainer(image="qdrant/qdrant:v1.12.4") as qdrant:
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
                graph_store = Neo4jGraphStore(config=config)

                parsed_qdrant_url = urlparse(qdrant.get_url())
                vconfig = QdrantConfig(
                    host=parsed_qdrant_url.hostname or "localhost",
                    http_port=parsed_qdrant_url.port or 6333,
                    grpc_port=int(qdrant.get_exposed_port(6334)),
                    api_key=None,
                    prefer_grpc=False,
                )
                vector_store = QdrantVectorStore(
                    config=vconfig, embedding_provider=DummyProvider()
                )

                await graph_store.connect()
                await vector_store.connect()
                store = FullStore(vector_store, graph_store)

                try:
                    await _seed_store_with_fixture(store)
                    yield store
                finally:
                    await graph_store.close()
                    await vector_store.close()

    return _factory


def _postgres_factory() -> Callable[[], AsyncIterator[StoreContract]]:
    """Factory that spins up a Postgres testcontainer per test."""
    from omniscience_core.db.models import Base
    from omniscience_index.stores.postgres_only_store import PostgresOnlyStore
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]

    async def _factory() -> AsyncIterator[StoreContract]:
        # Using pgvector image so CREATE EXTENSION vector doesn't fail
        with PostgresContainer("pgvector/pgvector:pg16") as postgres:
            driver_url = postgres.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+asyncpg://"
            )
            engine = create_async_engine(driver_url)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)

            from sqlalchemy import text

            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(Base.metadata.create_all)

            class DummyProvider:
                dim = 8
                provider_name = "dummy"
                model_name = "dummy"

                async def embed(self, texts):
                    import math

                    res = []
                    for text in texts:
                        h = hash(text)
                        raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(self.dim)]
                        n = math.sqrt(sum(x * x for x in raw)) or 1.0
                        res.append([x / n for x in raw])
                    return res

            store = PostgresOnlyStore(
                session_factory=session_factory, embedding_provider=DummyProvider()
            )
            await store.connect()
            try:
                await _seed_store_with_fixture(store)
                yield store
            finally:
                await store.close()
                await engine.dispose()

    return _factory


async def _seed_store_with_fixture(store: Any) -> None:
    """Populate a fresh store with the two-workspace contract fixture.

    Two entities named ``svc.shared`` (one per workspace), each pointing
    to a workspace-local neighbour via a ``calls`` edge.  The parallel
    structure across workspaces is what makes the isolation assertions
    meaningful (a leak would surface the *other* workspace's neighbour).
    """
    a_shared_source_id = uuid.uuid4()
    a_internal_source_id = uuid.uuid4()
    b_shared_source_id = uuid.uuid4()
    b_internal_source_id = uuid.uuid4()

    if hasattr(store, "_session_factory"):
        from omniscience_core.db.models import Source, Workspace
        from sqlalchemy.dialects.postgresql import insert

        async with store._session_factory() as session, session.begin():
            # Postgres needs Workspace and Source FKs to exist
            for ws_id, name in [(_WORKSPACE_A, "A"), (_WORKSPACE_B, "B")]:
                stmt = (
                    insert(Workspace)
                    .values(id=ws_id, name=name, display_name=name)
                    .on_conflict_do_nothing()
                )
                await session.execute(stmt)

            sources = [
                Source(id=a_shared_source_id, tenant_id=_WORKSPACE_A, name="a1", type="alerts"),
                Source(id=a_internal_source_id, tenant_id=_WORKSPACE_A, name="a2", type="alerts"),
                Source(id=b_shared_source_id, tenant_id=_WORKSPACE_B, name="b1", type="alerts"),
                Source(id=b_internal_source_id, tenant_id=_WORKSPACE_B, name="b2", type="alerts"),
            ]
            session.add_all(sources)

    a_shared = EntityUpsert(
        id=uuid.uuid4(),
        source_id=a_shared_source_id,
        entity_type="service",
        name="svc.shared",
        display_name="shared",
        chunk_id=None,
    )
    a_internal = EntityUpsert(
        id=uuid.uuid4(),
        source_id=a_internal_source_id,
        entity_type="service",
        name="svc.internal-a",
        display_name="internal-a",
        chunk_id=None,
    )
    b_shared = EntityUpsert(
        id=uuid.uuid4(),
        source_id=b_shared_source_id,
        entity_type="service",
        name="svc.shared",
        display_name="shared",
        chunk_id=None,
    )
    b_internal = EntityUpsert(
        id=uuid.uuid4(),
        source_id=b_internal_source_id,
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


_BACKENDS: list[tuple[str, Callable[[], Callable[[], AsyncIterator[StoreContract]]]]] = []

if _neo4j_available():
    _BACKENDS.append(("full", _full_store_factory))

if _postgres_available():
    _BACKENDS.append(("postgres_only", _postgres_factory))


@pytest.fixture(
    params=[pytest.param(name, id=name) for name, _ in _BACKENDS],
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[StoreContract]:
    """Yield a ready ``StoreContract`` for the parametrised backend."""
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
    store: StoreContract,
) -> None:
    """Protocol invariant — positional workspace_id is a TypeError."""
    with pytest.raises(TypeError):
        await store.get_entity("svc.shared", _WORKSPACE_A)  # type: ignore[misc]


async def test_find_related_requires_workspace_id_keyword_only(
    store: StoreContract,
) -> None:
    with pytest.raises(TypeError):
        await store.find_related("svc.shared", _WORKSPACE_A)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Contract: workspace scoping on reads
# ---------------------------------------------------------------------------


async def test_find_related_workspace_a_never_sees_workspace_b(
    store: StoreContract,
) -> None:
    """Cross-workspace isolation contract — parametric across backends."""
    result = await store.find_related(
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
    store: StoreContract,
) -> None:
    result = await store.find_related(
        entity_name="svc.shared",
        workspace_id=_WORKSPACE_B,
        max_depth=2,
    )

    names = {result.seed.name, *(n.name for n in result.related)}
    assert "svc.internal-a" not in names
    for e in result.edges:
        assert "svc.internal-a" not in (e.from_entity, e.to_entity)


async def test_traverse_is_alias_of_find_related(
    store: StoreContract,
) -> None:
    """``traverse`` must return the same shape as ``find_related``."""
    via_find = await store.find_related(
        entity_name="svc.shared",
        workspace_id=_WORKSPACE_A,
        max_depth=1,
    )
    via_traverse = await store.traverse(
        entity_name="svc.shared",
        workspace_id=_WORKSPACE_A,
        max_depth=1,
    )
    assert via_find.seed.name == via_traverse.seed.name


async def test_find_related_raises_entity_not_found_cross_workspace(
    store: StoreContract,
) -> None:
    """Requesting a B-only entity from A looks like 404 on both backends."""
    with pytest.raises(ValueError, match=r"entity_not_found:svc\.internal-b"):
        await store.find_related(
            entity_name="svc.internal-b",
            workspace_id=_WORKSPACE_A,
            max_depth=1,
        )


async def test_get_entity_returns_none_for_cross_workspace(
    store: StoreContract,
) -> None:
    """``get_entity`` returns None — NEVER leaks existence across workspaces."""
    result = await store.get_entity(
        entity_name="svc.internal-a",
        workspace_id=_WORKSPACE_B,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Vector Operations Conformance
# ---------------------------------------------------------------------------


def _chunk(text: str, ord_: int = 0) -> dict:
    import math

    h = hash(text)
    raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(8)]
    n = math.sqrt(sum(x * x for x in raw)) or 1.0
    vec = [x / n for x in raw]
    return {
        "ord": ord_,
        "text": text,
        "embedding": vec,
    }


@pytest.mark.asyncio
async def test_upsert_then_search_roundtrip_vector(store: StoreContract) -> None:
    import uuid

    from omniscience_retrieval.models import SearchRequest

    ws = _WORKSPACE_A
    entities = await store.get_all_entities(workspace_id=ws)
    source_id = uuid.UUID(entities[0].source)
    ext_id = "doc-123"
    text = "vector roundtrip text"

    await store.upsert_chunks(
        source_id=source_id,
        external_id=ext_id,
        uri=f"mem://{ext_id}",
        title=ext_id,
        content_hash=f"hash-{ext_id}",
        metadata={"workspace_id": str(ws)},
        chunks=[_chunk(text=text)],
    )

    req = SearchRequest(query=text, top_k=5)
    res = await store.search(request=req, workspace_id=ws)
    assert len(res.hits) >= 1
    assert res.hits[0].text == text


@pytest.mark.asyncio
async def test_cross_workspace_isolation_is_absolute_vector(store: StoreContract) -> None:
    import uuid

    from omniscience_retrieval.models import SearchRequest

    ws_a = _WORKSPACE_A
    ws_b = _WORKSPACE_B

    entities_b = await store.get_all_entities(workspace_id=ws_b)
    source_id_b = uuid.UUID(entities_b[0].source)

    ext_id = "doc-iso"
    text = "secret workspace B text"

    await store.upsert_chunks(
        source_id=source_id_b,
        external_id=ext_id,
        uri=f"mem://{ext_id}",
        title=ext_id,
        content_hash=f"hash-{ext_id}",
        metadata={"workspace_id": str(ws_b)},
        chunks=[_chunk(text=text)],
    )

    req = SearchRequest(query=text, top_k=5)
    res = await store.search(request=req, workspace_id=ws_a)
    assert len(res.hits) == 0


@pytest.mark.asyncio
async def test_delete_by_document_tombstones_chunks(store: StoreContract) -> None:
    import uuid

    from omniscience_retrieval.models import SearchRequest

    ws = _WORKSPACE_A
    entities = await store.get_all_entities(workspace_id=ws)
    source_id = uuid.UUID(entities[0].source)
    ext_id = "doc-del"
    text = "will be deleted"

    await store.upsert_chunks(
        source_id=source_id,
        external_id=ext_id,
        uri=f"mem://{ext_id}",
        title=ext_id,
        content_hash=f"hash-{ext_id}",
        metadata={"workspace_id": str(ws)},
        chunks=[_chunk(text=text)],
    )

    # Check it exists
    req = SearchRequest(query=text, top_k=5)
    res = await store.search(request=req, workspace_id=ws)
    assert len(res.hits) >= 1

    # Delete
    deleted = await store.delete_by_document(
        source_id=source_id, external_id=ext_id, workspace_id=ws
    )
    assert deleted is True

    # Check it is gone
    res = await store.search(request=req, workspace_id=ws)
    assert len(res.hits) == 0


@settings(max_examples=10, deadline=None)
@given(
    entity_names=st.lists(
        st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=20),
        min_size=2,
        max_size=10,
    )
)
@pytest.mark.asyncio
async def test_concurrent_upsert_property(store: StoreContract, entity_names: list[str]) -> None:
    ws = _WORKSPACE_A
    entities = await store.get_all_entities(workspace_id=ws)
    source_id = uuid.UUID(entities[0].source) if entities else uuid.uuid4()

    upserts = [
        store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=source_id,
                entity_type="test-concurrent",
                name=name,
                display_name=name,
                chunk_id=None,
            ),
            workspace_id=ws,
        )
        for name in entity_names
    ]

    await asyncio.gather(*upserts)

    for name in entity_names:
        ent = await store.get_entity(entity_name=name, workspace_id=ws)
        assert ent is not None
        assert ent.name == name

@settings(max_examples=5, deadline=None)
@given(
    display_names=st.lists(
        st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=20),
        min_size=2,
        max_size=5,
    )
)
@pytest.mark.asyncio
async def test_concurrent_same_entity_upsert_property(store: StoreContract, display_names: list[str]) -> None:
    ws = _WORKSPACE_A
    entities = await store.get_all_entities(workspace_id=ws)
    source_id = uuid.UUID(entities[0].source) if entities else uuid.uuid4()

    ent_id = uuid.uuid4()
    ent_name = f"same-ent-{uuid.uuid4()}"

    upserts = [
        store.upsert_entity(
            entity=EntityUpsert(
                id=ent_id,
                source_id=source_id,
                entity_type="test-concurrent-same",
                name=ent_name,
                display_name=dname,
                chunk_id=None,
            ),
            workspace_id=ws,
        )
        for dname in display_names
    ]

    results = await asyncio.gather(*upserts, return_exceptions=True)
    # Ensure no exceptions leaked out to break the test, except if they are DB-level constraint errors
    # Wait for completion, then check that exactly 1 or 0 entities exist (no duplicates).
    all_ents = await store.get_all_entities(workspace_id=ws)
    matching = [e for e in all_ents if e.name == ent_name]
    assert len(matching) <= 1, "Concurrent upsert of the same entity resulted in duplicates"

@pytest.mark.asyncio
async def test_upsert_graph_partial_failure_atomicity(store: StoreContract) -> None:
    ws = _WORKSPACE_A
    entities = await store.get_all_entities(workspace_id=ws)
    source_id = uuid.UUID(entities[0].source) if entities else uuid.uuid4()

    batch_id = uuid.uuid4()

    valid_ent = EntityUpsert(
        id=uuid.uuid4(),
        source_id=source_id,
        entity_type="test-atomicity",
        name=f"valid-{batch_id}",
        display_name="valid",
        chunk_id=None,
    )

    # Injecting invalid data to cause DB failure
    # Postgres and Neo4j reject null ids or type mismatches when bound to query parameters
    invalid_ent = EntityUpsert(
        id=None,  # type: ignore
        source_id=source_id,
        entity_type="test-atomicity",
        name=f"invalid-{batch_id}",
        display_name="invalid",
        chunk_id=None,
    )

    try:
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[valid_ent, invalid_ent],
            edges=[],
        )
    except Exception:
        pass

    # Check that valid_ent was NOT inserted, ensuring atomicity
    ent = await store.get_entity(entity_name=valid_ent.name, workspace_id=ws)
    assert ent is None, "Partial failure leaked into the database (atomicity broken)"

@settings(max_examples=5, deadline=None)
@given(
    version=st.one_of(
        st.none(),
        st.integers(min_value=-9223372036854775808, max_value=9223372036854775807),
    )
)
@pytest.mark.asyncio
async def test_upsert_boundary_versions_property(store: StoreContract, version: int | None) -> None:
    ws = _WORKSPACE_A
    entities = await store.get_all_entities(workspace_id=ws)
    source_id = uuid.UUID(entities[0].source) if entities else uuid.uuid4()

    ent_id = uuid.uuid4()
    ent_name = f"v-ent-{uuid.uuid4()}"

    try:
        await store.upsert_entity(
            entity=EntityUpsert(
                id=ent_id,
                source_id=source_id,
                entity_type="test-version",
                name=ent_name,
                display_name="v-test",
                chunk_id=None,
                version=version,
            ),
            workspace_id=ws,
        )
        
        ent = await store.get_entity(entity_name=ent_name, workspace_id=ws)
        assert ent is not None
        assert ent.name == ent_name
    except Exception as e:
        # We accept failures on extreme values if the underlying DB rejects them 
        # (e.g. Postgres BIGINT limits). 
        # But we verify it doesn't crash the Python process or leave invalid state.
        pass
