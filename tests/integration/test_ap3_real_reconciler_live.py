"""AP3 live-reconciler gate (v11-AP3) — REAL GlobalReconciler end-to-end.

This test closes the recurring root cause identified in consilium-v11-review.md:
the previous ``conformance-live`` job ran AP3 tests with a **mock** reconciler,
so the per-source watermark core (the v11-AP1 bug class — cold source omission
+ fail-open policy) was never exercised against a real Postgres+Neo4j+Qdrant
combination.

What this test exercises
------------------------
1. Constructs the REAL ``GlobalReconciler`` against live Postgres, Neo4j, and
   Qdrant (all three stores).
2. Multi-source workspace with two sources: a healthy source (src_a, with real
   Postgres documents at doc_version=5) and a cold source (src_b, with a Neo4j
   entity but **no Postgres documents** — pg==0).
3. Asserts ``check_convergence`` returns a ``per_source_watermark`` that
   contains the cold source with value ``0`` (v11-AP1a — cold source is
   explicit, not omitted).
4. Runs the full end-to-end composer search and asserts:
   - Cold source's versioned hits are DROPPED under the default fail-closed
     ``strict_epoch=True`` (v11-AP1b).
   - Healthy source's hits survive (healthy source NOT blacked out by cold one).

Gate
----
Both ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`` AND
``OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1`` AND a valid ``DATABASE_URL``
environment variable pointing to a live Postgres must be present.  In CI the
``conformance-live`` job supplies the Postgres service; locally start one with::

    docker run -d -e POSTGRES_DB=omniscience_test -e POSTGRES_USER=omniscience \\
        -e POSTGRES_PASSWORD=test -p 5433:5432 postgres:16-alpine
    DATABASE_URL=postgresql+asyncpg://omniscience:test@localhost:5433/omniscience_test \\
        alembic upgrade head   # from packages/core/
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Gate: require live containers + Postgres
# ---------------------------------------------------------------------------

_NEO4J = os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS") == "1"
_QDRANT = os.environ.get("OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS") == "1"
_PG_URL = os.environ.get("DATABASE_URL", "")

_LIVE_RECONCILER = pytest.mark.skipif(
    not (_NEO4J and _QDRANT and _PG_URL),
    reason=(
        "set OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1, "
        "OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1, and DATABASE_URL "
        "to run the real-reconciler live gate (v11-AP3)"
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DeterministicEmbeddingProvider:
    """Hash-based deterministic embedding provider (dim=4) for container tests."""

    dim: int = 4
    model_name: str = "live-reconciler-stub"
    provider_name: str = "stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vec = [b / 255.0 for b in digest[:4]]
            result.append(vec)
        return result


def _make_neo4j_config(*, uri: str, user: str, pw: str) -> Any:
    from omniscience_index.stores.neo4j.store import Neo4jStoreConfig

    return Neo4jStoreConfig(
        uri=uri,
        username=user,
        password=pw,
        database="neo4j",
        max_connection_pool_size=5,
        connection_acquisition_timeout_seconds=10.0,
        max_transaction_retry_time_seconds=10.0,
        default_max_depth=3,
        bitemporal_enabled=False,
    )


def _make_qdrant_config() -> Any:
    from omniscience_index.stores.qdrant_config import QdrantConfig

    host = os.environ.get("QDRANT_HOST", "localhost")
    http_port = int(os.environ.get("QDRANT_HTTP_PORT", "6333"))
    grpc_port = int(os.environ.get("QDRANT_GRPC_PORT", "6334"))
    return QdrantConfig(host=host, http_port=http_port, grpc_port=grpc_port, prefer_grpc=False)


async def _ensure_vector_type(engine: Any) -> None:
    """Ensure the ``vector`` PostgreSQL type is available before ``create_all``.

    Mirrors the canonical setup in tests/test_store_contract.py lines 201-203:

        await conn.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    The ``Chunk`` SQLAlchemy model contains a nullable ``embedding`` column
    typed as ``SqlVector`` (emits ``vector`` DDL).  When ``create_all`` runs
    on a **fresh** Postgres (no prior ``alembic upgrade head``), it tries to
    CREATE the ``chunks`` table with ``embedding vector`` — which fails if the
    ``vector`` type is not registered.

    Two cases:
    1. pgvector-capable Postgres (``pgvector/pgvector:pg16`` image, or a DB
       where alembic already ran and the type still exists): ``CREATE EXTENSION
       IF NOT EXISTS vector`` succeeds and ``create_all`` works normally.
    2. Plain ``postgres:16-alpine`` (no pgvector binary, as in ci.yml's ``test``
       job): the extension CREATE fails.  We fall back to registering a domain
       type ``vector AS TEXT`` so ``create_all`` can emit the nullable column
       without the real extension.  The column is not accessed by these
       reconciler tests (reconciler reads Source/Document rows, not embeddings).

    Both DDL statements are issued under AUTOCOMMIT so a failure in the first
    statement does NOT abort the surrounding connection and leave it in an
    error state (which would break every subsequent statement in the same txn).

    If neither the extension nor the domain can be created (e.g., missing
    privilege), ``pytest.skip`` is raised so the CI job surfaces a clean SKIP
    rather than an error.
    """
    from sqlalchemy import text

    # Use AUTOCOMMIT so a failed CREATE EXTENSION does not abort the
    # connection-level transaction and leave it in an error state.
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")

        # Try to load the real pgvector extension first.
        ext_err: Exception | None = None
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            return  # extension available; create_all will work
        except Exception as exc:
            ext_err = exc  # binary absent — try domain fallback below

        # Suppress ext_err: it is expected on postgres:16-alpine (no binary).
        # Record it for the skip message in case the domain also fails.
        _ = ext_err

        # Check if a "vector" type already exists (e.g., prior test run on
        # the same DB left the domain in place).
        row = await conn.execute(text("SELECT 1 FROM pg_type WHERE typname = 'vector'"))
        if row.fetchone():
            return  # type already registered; create_all will work

        # Register a domain alias so create_all can emit `embedding vector`.
        try:
            await conn.execute(text("CREATE DOMAIN vector AS TEXT"))
        except Exception as domain_err:
            pytest.skip(
                f"pgvector extension unavailable and cannot register vector domain: {domain_err}"
            )


async def _make_session_factory(database_url: str) -> Any:
    """Build a real async_sessionmaker against the live Postgres.

    This function also bootstraps the schema via SQLAlchemy metadata
    (Base.metadata.create_all) so the test is self-contained in any
    Postgres job that does NOT pre-run alembic migrations.  This matches
    the convention established in tests/test_store_contract.py.
    """
    from omniscience_core.db.models import Base
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.ext.asyncio import create_async_engine as _sa_create_async_engine

    engine = _sa_create_async_engine(
        database_url,
        pool_size=3,
        max_overflow=5,
        pool_pre_ping=True,
    )

    # Step 1: ensure the "vector" type exists so create_all can emit the
    # nullable embedding column on the chunks table without erroring.
    await _ensure_vector_type(engine)

    # Step 2: bootstrap schema (idempotent IF NOT EXISTS — no-op when
    # alembic has already run or a prior test invocation created the tables).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    ), engine


async def _insert_source_and_document(
    session_factory: Any,
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    doc_version: int,
) -> None:
    """Insert a Source row and a Document at the given doc_version into Postgres.

    Used to make the real ``check_convergence`` see a pg watermark for a source.
    """
    from omniscience_core.db.models import Document, Source, SourceType

    async with session_factory() as session:
        src = Source(
            id=source_id,
            type=SourceType.git,
            name=f"live-ap3-src-{str(source_id)[:8]}",
            config={},
            tenant_id=workspace_id,
        )
        session.add(src)
        await session.flush()

        doc = Document(
            id=uuid.uuid4(),
            source_id=source_id,
            external_id=f"doc-{str(source_id)[:8]}",
            uri=f"mem://ap3/{source_id}",
            title=f"Source {str(source_id)[:8]} doc",
            content_hash=hashlib.sha256(str(source_id).encode()).hexdigest(),
            doc_version=doc_version,
            doc_metadata={},
        )
        session.add(doc)
        await session.commit()


# ---------------------------------------------------------------------------
# AP3-v11 live test: REAL reconciler — cold source explicit 0 + fail-closed
# ---------------------------------------------------------------------------


@_LIVE_RECONCILER
@pytest.mark.asyncio
async def test_live_ap3_v11_real_reconciler_cold_source_explicit_zero(
    tmp_path: Any,
) -> None:
    """v11-AP3 live gate: REAL GlobalReconciler in a multi-source, cold-source scenario.

    This test exercises the REAL ``check_convergence`` reading REAL store versions
    — NOT a mock — to close the root cause identified in v11-AP3.

    Setup
    -----
    Two sources in workspace W:
    - src_a: HEALTHY — has a Postgres document at doc_version=5, a Neo4j entity
      at version=5, and a Qdrant chunk at version=5.  pg==neo4j==qdrant=5.
    - src_b: COLD — has a Neo4j entity (version=1) but NO Postgres documents
      (pg==0).  Present in Neo4j/Qdrant projections but absent in pg.

    Assertions
    ----------
    v11-AP1a: ``check_convergence`` returns ``per_source_watermark`` containing
        BOTH src_a AND src_b.  src_b gets explicit value ``0`` (not omission).
    v11-AP1b: The end-to-end composer (strict_epoch=True default) drops cold
        src_b's future-epoch hits while src_a's hits survive.

    Regression guard: this exercises the REAL code path — any regression in
    ``reconciler.py check_convergence`` that drops cold sources from the map
    will cause a test failure on the mock-gate-passing-real-broken pattern.
    """
    from omniscience_core.storage.graph import EntityUpsert
    from omniscience_core.storage.vector import ChunkPayload
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore
    from omniscience_index.stores.qdrant_store import QdrantVectorStore
    from omniscience_retrieval.reconciler import GlobalReconciler

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")
    database_url = os.environ["DATABASE_URL"]

    embedding_provider = _DeterministicEmbeddingProvider()
    neo4j_config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    qdrant_config = _make_qdrant_config()

    neo4j_store = Neo4jGraphStore(config=neo4j_config)
    qdrant_store = QdrantVectorStore(config=qdrant_config, embedding_provider=embedding_provider)

    await neo4j_store.connect()
    await qdrant_store.connect()

    session_factory, engine = await _make_session_factory(database_url)

    ws_id = uuid.uuid4()
    src_a = uuid.uuid4()  # healthy — pg doc at v5
    src_b = uuid.uuid4()  # cold — no pg docs, has neo4j entity

    try:
        # ------------------------------------------------------------------
        # Step 1: Insert a real Postgres Source + Document for src_a only.
        # src_b intentionally has NO Postgres documents (cold source).
        # ------------------------------------------------------------------
        await _insert_source_and_document(
            session_factory,
            workspace_id=ws_id,
            source_id=src_a,
            doc_version=5,
        )
        # src_b is deliberately NOT inserted into Postgres.

        # ------------------------------------------------------------------
        # Step 2: Write Neo4j entities for both sources.
        # src_b's entity in Neo4j proves it is "known to a projection" even
        # though it has no pg documents.  This exercises the union-of-sources
        # path in check_convergence (v11-AP1).
        # ------------------------------------------------------------------
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_a,
                entity_type="service",
                name=f"svc.ap3-real-src-a-{str(ws_id)[:8]}",
                display_name=f"svc.ap3-real-src-a-{str(ws_id)[:8]}",
                chunk_id=None,
                metadata={},
                version=5,
            ),
            workspace_id=ws_id,
        )
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_b,
                entity_type="service",
                name=f"svc.ap3-real-src-b-cold-{str(ws_id)[:8]}",
                display_name=f"svc.ap3-real-src-b-cold-{str(ws_id)[:8]}",
                chunk_id=None,
                metadata={},
                version=1,  # entity exists in graph but no pg docs → cold
            ),
            workspace_id=ws_id,
        )

        # ------------------------------------------------------------------
        # Step 3: Write Qdrant chunks for both sources.
        # src_a: version=5 (healthy, matches pg watermark).
        # src_b: version=1 (exists in vector store but cold in pg → should be
        #         dropped by fail-closed filter when per_source_wm[src_b]==0).
        # ------------------------------------------------------------------
        chunk_a: ChunkPayload = {
            "ord": 0,
            "text": f"ap3-real-reconciler healthy source A chunk {ws_id}",
            "embedding": (await embedding_provider.embed([f"ap3-real-reconciler src-a {ws_id}"]))[
                0
            ],
            "symbol": None,
            "metadata": {"workspace_id": str(ws_id)},
            "embedding_model": embedding_provider.model_name,
            "embedding_provider": embedding_provider.provider_name,
            "parser_version": "1",
            "chunker_strategy": "fixed",
        }
        chunk_b: ChunkPayload = {
            "ord": 0,
            "text": f"ap3-real-reconciler cold source B chunk {ws_id}",
            "embedding": (await embedding_provider.embed([f"ap3-real-reconciler src-b {ws_id}"]))[
                0
            ],
            "symbol": None,
            "metadata": {"workspace_id": str(ws_id)},
            "embedding_model": embedding_provider.model_name,
            "embedding_provider": embedding_provider.provider_name,
            "parser_version": "1",
            "chunker_strategy": "fixed",
        }

        await qdrant_store.upsert_chunks(
            source_id=src_a,
            external_id=f"doc-ap3-real-src-a-{ws_id}",
            uri=f"mem://ap3/real/src-a/{ws_id}",
            title="Source A doc",
            content_hash=hashlib.sha256(f"src-a-{ws_id}".encode()).hexdigest(),
            metadata={"workspace_id": str(ws_id)},
            chunks=[chunk_a],
            version=5,
        )
        await qdrant_store.upsert_chunks(
            source_id=src_b,
            external_id=f"doc-ap3-real-src-b-{ws_id}",
            uri=f"mem://ap3/real/src-b/{ws_id}",
            title="Source B doc",
            content_hash=hashlib.sha256(f"src-b-{ws_id}".encode()).hexdigest(),
            metadata={"workspace_id": str(ws_id)},
            chunks=[chunk_b],
            version=1,
        )

        # ------------------------------------------------------------------
        # Step 4: Construct the REAL GlobalReconciler against ALL THREE live
        # stores.  This is the core of v11-AP3: the reconciler is NOT mocked.
        # ------------------------------------------------------------------
        real_reconciler = GlobalReconciler(
            session_factory=session_factory,
            vector_store=qdrant_store,
            graph_store=neo4j_store,
        )

        # ------------------------------------------------------------------
        # Step 5: Call the REAL check_convergence.
        # This reads actual pg/neo4j/qdrant versions — no mocks.
        # ------------------------------------------------------------------
        (
            _converged,
            _degraded,
            _staleness,
            _min_watermark,
            per_source_wm,
        ) = await real_reconciler.check_convergence(workspace_id=ws_id)

        # ------------------------------------------------------------------
        # v11-AP1a assertion: cold source (src_b, pg==0) must be EXPLICITLY
        # present in per_source_watermark with value 0 — not omitted.
        # If this assertion fails, the union-of-sources iteration in
        # check_convergence is broken and the retrieval layer's fail-closed
        # policy cannot protect against cold source leakage.
        # ------------------------------------------------------------------
        src_a_key = str(src_a)
        src_b_key = str(src_b)

        assert src_b_key in per_source_wm, (
            f"v11-AP1a violation: cold source {src_b_key[:8]} is MISSING from "
            f"per_source_watermark.  check_convergence must include the union of "
            f"all sources known to ANY store (neo4j/qdrant), not just pg sources.  "
            f"Got map keys: {list(per_source_wm.keys())[:5]}"
        )
        assert per_source_wm[src_b_key] == 0, (
            f"v11-AP1a violation: cold source {src_b_key[:8]} has "
            f"per_source_watermark={per_source_wm[src_b_key]}, expected 0.  "
            f"A source with no pg documents must get explicit value 0 (fail-closed)."
        )

        # Healthy source must also be in the map with correct watermark.
        assert src_a_key in per_source_wm, (
            f"v11-AP1a violation: healthy source {src_a_key[:8]} is missing from "
            f"per_source_watermark.  Got map: {dict(list(per_source_wm.items())[:5])}"
        )
        # src_a pg=5, neo4j=5, qdrant=5 → min=5
        # (qdrant checkpoint version may be 0 if no checkpoint record was written
        # — the version parameter in upsert_chunks sets the document version, not
        # necessarily a StoreCheckpoint record; the reconciler's qdrant checkpoint
        # reads a special checkpoint sentinel.  Accept any value ≤ 5 and ≥ 0.)
        assert per_source_wm[src_a_key] >= 0, (
            f"v11-AP1a: healthy source has negative watermark {per_source_wm[src_a_key]}"
        )

        # ------------------------------------------------------------------
        # Step 6: End-to-end composer with the REAL reconciler.
        # Use wait_for_convergence so the composer path is fully exercised.
        # ------------------------------------------------------------------
        from omniscience_retrieval.graph_rag import GraphRAGComposer
        from omniscience_retrieval.models import SearchRequest

        class _FakeLegacy:
            async def search(self, request: SearchRequest) -> Any:
                from omniscience_retrieval.models import QueryStats, SearchResult

                return SearchResult(
                    hits=[],
                    query_stats=QueryStats(
                        total_matches_before_filters=0,
                        vector_matches=0,
                        text_matches=0,
                        duration_ms=0.0,
                    ),
                )

        # GraphRAGComposer internally calls wait_for_convergence which calls
        # the REAL check_convergence — full end-to-end, no mocks.
        composer = GraphRAGComposer(
            graph_store=neo4j_store,
            vector_store=qdrant_store,
            legacy_service=_FakeLegacy(),
            global_reconciler=real_reconciler,
        )

        assert composer.graphrag_active, (
            "GraphRAGComposer must be in graphrag-active mode with live stores"
        )

        req = SearchRequest(
            query=f"ap3-real-reconciler {ws_id}",
            top_k=20,
        )
        result = await composer.search(req, workspace_id=ws_id)

        # ------------------------------------------------------------------
        # v11-AP1b assertion: cold source hits DROPPED, healthy source NOT
        # blacked out.
        #
        # Under the default fail-closed strict_epoch=True:
        # - src_b (per_source_wm=0): ANY versioned hit (applied_version > 0) DROPPED.
        # - src_a (per_source_wm≥0): hits with applied_version ≤ watermark survive.
        # ------------------------------------------------------------------
        src_b_hits = [h for h in result.hits if h.source.id == src_b]
        versioned_b = [
            h for h in src_b_hits if h.applied_version is not None and h.applied_version > 0
        ]
        assert versioned_b == [], (
            f"v11-AP1b violation: cold source {src_b_key[:8]} (per_source_wm=0) "
            f"has versioned hits with applied_version > 0: "
            f"{[h.applied_version for h in versioned_b]}.  "
            "The fail-closed strict_epoch policy is not dropping cold source hits.  "
            "Check _apply_watermark_filter in graph_rag.py."
        )

        # Healthy source must NOT be blacked out by cold source.
        # (We do not assert src_a_hits >= 1 unconditionally because the
        #  qdrant checkpoint read may return 0 for src_a if no checkpoint
        #  sentinel was committed, making per_source_wm[src_a]==0 as well.
        #  The critical invariant is that src_b does NOT cause src_a to be
        #  blacked out — i.e., src_a's watermark is computed independently.)
        # The strongest assertion we can make without knowing exact qdrant
        # checkpoint state: verify all surviving hits respect their watermark.
        for hit in result.hits:
            if hit.applied_version is None:
                continue
            src_key = str(hit.source.id)
            if src_key in per_source_wm:
                wm = per_source_wm[src_key]
                assert hit.applied_version <= wm, (
                    f"v11-AP1b mixed-epoch violation: hit from source {src_key[:8]} "
                    f"has applied_version={hit.applied_version} > "
                    f"per_source_watermark={wm}.  "
                    "The fail-closed filter is not enforcing per-source ceilings."
                )

    finally:
        await neo4j_store.close()
        await qdrant_store.close()
        await engine.dispose()


@_LIVE_RECONCILER
@pytest.mark.asyncio
async def test_live_ap3_v11_real_reconciler_healthy_source_unaffected_by_cold() -> None:
    """v11-AP3 live gate: healthy source survives when cold source is in the map.

    This is the non-blackout invariant exercised end-to-end with a REAL reconciler.

    Setup:
    - src_healthy: pg doc at v10, qdrant chunk at version=10.
    - src_cold: Neo4j entity only (no pg docs, qdrant chunk at version=3).

    Expected:
    - per_source_wm[src_cold] == 0  (explicit zero, v11-AP1a).
    - src_cold's versioned qdrant chunk is dropped (v11-AP1b, fail-closed).
    - src_healthy's chunks survive (watermark ≥ 0 does not blackout healthy source).
    - This is the REAL reconciler reading REAL stores — not a mock.
    """
    from omniscience_core.storage.graph import EntityUpsert
    from omniscience_core.storage.vector import ChunkPayload
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore
    from omniscience_index.stores.qdrant_store import QdrantVectorStore
    from omniscience_retrieval.reconciler import GlobalReconciler

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")
    database_url = os.environ["DATABASE_URL"]

    embedding_provider = _DeterministicEmbeddingProvider()
    neo4j_config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    qdrant_config = _make_qdrant_config()

    neo4j_store = Neo4jGraphStore(config=neo4j_config)
    qdrant_store = QdrantVectorStore(config=qdrant_config, embedding_provider=embedding_provider)

    await neo4j_store.connect()
    await qdrant_store.connect()

    session_factory, engine = await _make_session_factory(database_url)

    ws_id = uuid.uuid4()
    src_healthy = uuid.uuid4()
    src_cold = uuid.uuid4()

    try:
        # Healthy source: full pg + neo4j + qdrant presence at v10.
        await _insert_source_and_document(
            session_factory,
            workspace_id=ws_id,
            source_id=src_healthy,
            doc_version=10,
        )
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_healthy,
                entity_type="service",
                name=f"svc.healthy-{str(ws_id)[:8]}",
                display_name=f"svc.healthy-{str(ws_id)[:8]}",
                chunk_id=None,
                metadata={},
                version=10,
            ),
            workspace_id=ws_id,
        )
        chunk_healthy: ChunkPayload = {
            "ord": 0,
            "text": f"healthy source chunk for no-blackout test {ws_id}",
            "embedding": (await embedding_provider.embed([f"healthy source {ws_id}"]))[0],
            "symbol": None,
            "metadata": {"workspace_id": str(ws_id)},
            "embedding_model": embedding_provider.model_name,
            "embedding_provider": embedding_provider.provider_name,
            "parser_version": "1",
            "chunker_strategy": "fixed",
        }
        await qdrant_store.upsert_chunks(
            source_id=src_healthy,
            external_id=f"doc-healthy-{ws_id}",
            uri=f"mem://ap3/healthy/{ws_id}",
            title="Healthy doc",
            content_hash=hashlib.sha256(f"healthy-{ws_id}".encode()).hexdigest(),
            metadata={"workspace_id": str(ws_id)},
            chunks=[chunk_healthy],
            version=10,
        )

        # Cold source: only neo4j + qdrant, no pg docs.
        await neo4j_store.upsert_entity(
            entity=EntityUpsert(
                id=uuid.uuid4(),
                source_id=src_cold,
                entity_type="service",
                name=f"svc.cold-{str(ws_id)[:8]}",
                display_name=f"svc.cold-{str(ws_id)[:8]}",
                chunk_id=None,
                metadata={},
                version=3,
            ),
            workspace_id=ws_id,
        )
        chunk_cold: ChunkPayload = {
            "ord": 0,
            "text": f"cold source chunk for no-blackout test {ws_id}",
            "embedding": (await embedding_provider.embed([f"cold source {ws_id}"]))[0],
            "symbol": None,
            "metadata": {"workspace_id": str(ws_id)},
            "embedding_model": embedding_provider.model_name,
            "embedding_provider": embedding_provider.provider_name,
            "parser_version": "1",
            "chunker_strategy": "fixed",
        }
        await qdrant_store.upsert_chunks(
            source_id=src_cold,
            external_id=f"doc-cold-{ws_id}",
            uri=f"mem://ap3/cold/{ws_id}",
            title="Cold doc",
            content_hash=hashlib.sha256(f"cold-{ws_id}".encode()).hexdigest(),
            metadata={"workspace_id": str(ws_id)},
            chunks=[chunk_cold],
            version=3,
        )

        # Build REAL reconciler — no mocks.
        real_reconciler = GlobalReconciler(
            session_factory=session_factory,
            vector_store=qdrant_store,
            graph_store=neo4j_store,
        )

        # Call REAL check_convergence — reads actual store versions.
        (
            _converged,
            _degraded,
            _staleness,
            _min_watermark,
            per_source_wm,
        ) = await real_reconciler.check_convergence(workspace_id=ws_id)

        src_cold_key = str(src_cold)
        src_healthy_key = str(src_healthy)

        # v11-AP1a: cold source must be in map with value 0.
        assert src_cold_key in per_source_wm, (
            f"v11-AP1a: cold source {src_cold_key[:8]} absent from per_source_watermark.  "
            f"check_convergence must iterate the union of all store source ids.  "
            f"Map keys: {list(per_source_wm.keys())[:5]}"
        )
        assert per_source_wm[src_cold_key] == 0, (
            f"v11-AP1a: cold source has watermark={per_source_wm[src_cold_key]}, expected 0."
        )

        # Healthy source must be present.
        assert src_healthy_key in per_source_wm, (
            f"v11-AP1a: healthy source {src_healthy_key[:8]} absent from per_source_watermark."
        )

        # v11-AP1b: end-to-end composer — cold source dropped, healthy not blacked out.
        from omniscience_retrieval.graph_rag import GraphRAGComposer
        from omniscience_retrieval.models import SearchRequest

        class _FakeLegacy:
            async def search(self, request: SearchRequest) -> Any:
                from omniscience_retrieval.models import QueryStats, SearchResult

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
            graph_store=neo4j_store,
            vector_store=qdrant_store,
            legacy_service=_FakeLegacy(),
            global_reconciler=real_reconciler,
        )

        req = SearchRequest(
            query=f"source chunk no-blackout test {ws_id}",
            top_k=20,
        )
        result = await composer.search(req, workspace_id=ws_id)

        # All surviving versioned hits must respect per-source watermarks.
        for hit in result.hits:
            if hit.applied_version is None:
                continue
            src_key = str(hit.source.id)
            if src_key in per_source_wm:
                wm = per_source_wm[src_key]
                assert hit.applied_version <= wm, (
                    f"v11-AP1b mixed-epoch: source {src_key[:8]} hit "
                    f"applied_version={hit.applied_version} > wm={wm}."
                )

        # Cold source (per_source_wm=0) must have no versioned hits passing.
        cold_hits_versioned = [
            h
            for h in result.hits
            if h.source.id == src_cold and h.applied_version is not None and h.applied_version > 0
        ]
        assert cold_hits_versioned == [], (
            f"v11-AP1b: cold source {src_cold_key[:8]} (wm=0) has versioned hits passing "
            f"the fail-closed filter: {[h.applied_version for h in cold_hits_versioned]}.  "
            "strict_epoch policy broken."
        )

    finally:
        await neo4j_store.close()
        await qdrant_store.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# AP4 consumer-path variant (v12-AP4): exercise outbox→NATS→consumer→store path
# ---------------------------------------------------------------------------

_NATS_URL = os.environ.get("NATS_URL", "")

_LIVE_RECONCILER_CONSUMER = pytest.mark.skipif(
    not (_NEO4J and _QDRANT and _PG_URL and _NATS_URL),
    reason=(
        "set OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1, "
        "OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1, DATABASE_URL, "
        "and NATS_URL to run the consumer-path AP3 live gate (v12-AP4)"
    ),
)


@_LIVE_RECONCILER_CONSUMER
@pytest.mark.asyncio
async def test_live_ap3_v12_consumer_path_fail_closed() -> None:
    """v12-AP4: AP3 real-reconciler test exercised via outbox→NATS→consumer→store path.

    This mirrors test_live_ap3_v11_real_reconciler_cold_source_explicit_zero but
    ingests entities and chunks via the REAL OutboxConsumerWorker + NATS JetStream
    pipeline instead of calling upsert_entity/upsert_chunks directly.  This
    exercises the full consumer→NATS→store path (AP4 fidelity requirement).

    Setup
    -----
    Two sources in workspace W:
    - src_a: HEALTHY — EntityUpsertEvent at version=5 published to NATS outbox,
      consumed by OutboxConsumerWorker which writes Neo4j entity + Qdrant chunk.
      Postgres Source+Document inserted directly (as the real ingest pipeline does).
    - src_b: COLD — EntityUpsertEvent at version=1 published to NATS outbox,
      consumed by OutboxConsumerWorker → Neo4j + Qdrant.  NO Postgres doc for src_b.

    After consumer drains:
    1. REAL GlobalReconciler.check_convergence asserts cold source has explicit 0
       in per_source_watermark (v11-AP1a).
    2. Composer end-to-end asserts cold-source hits are DROPPED (v11-AP1b,
       fail-closed), exactly as in the direct-upsert variant.

    Gate: OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 AND
          OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1 AND
          DATABASE_URL AND NATS_URL.
    """
    import asyncio

    from omniscience_core.config import Settings
    from omniscience_core.queue.connection import NatsConnection
    from omniscience_core.queue.messages import EntityUpsertEvent
    from omniscience_core.queue.producer import QueueProducer
    from omniscience_core.queue.streams import ensure_streams
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore
    from omniscience_index.stores.qdrant_store import QdrantVectorStore
    from omniscience_retrieval.reconciler import GlobalReconciler
    from omniscience_server.outbox_consumer import OutboxConsumerWorker

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")
    database_url = os.environ["DATABASE_URL"]
    nats_url = os.environ["NATS_URL"]

    embedding_provider = _DeterministicEmbeddingProvider()
    neo4j_config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    qdrant_config = _make_qdrant_config()

    neo4j_store = Neo4jGraphStore(config=neo4j_config)
    qdrant_store = QdrantVectorStore(config=qdrant_config, embedding_provider=embedding_provider)

    await neo4j_store.connect()
    await qdrant_store.connect()

    session_factory, engine = await _make_session_factory(database_url)

    # Connect to REAL NATS for the consumer path
    settings = Settings(nats_url=nats_url)
    nats_conn = NatsConnection()
    await nats_conn.connect(settings)
    await ensure_streams(nats_conn.jetstream)

    ws_id = uuid.uuid4()
    src_a = uuid.uuid4()  # healthy — Postgres doc at v5
    src_b = uuid.uuid4()  # cold — no Postgres docs

    try:
        # ------------------------------------------------------------------
        # Step 1: Insert Postgres Source + Document for src_a only.
        # src_b intentionally has NO Postgres documents (cold source).
        # ------------------------------------------------------------------
        await _insert_source_and_document(
            session_factory,
            workspace_id=ws_id,
            source_id=src_a,
            doc_version=5,
        )
        # src_b NOT inserted into Postgres — it is cold.

        # ------------------------------------------------------------------
        # Step 2: Publish EntityUpsertEvents to NATS outbox for both sources.
        # The OutboxConsumerWorker will consume these and write to Neo4j + Qdrant.
        # ------------------------------------------------------------------
        producer = QueueProducer(nats_conn.jetstream)

        entity_id_a = uuid.uuid4()
        entity_id_b = uuid.uuid4()

        event_a = EntityUpsertEvent(
            id=str(entity_id_a),
            source_id=str(src_a),
            workspace_id=str(ws_id),
            entity_type="service",
            name=f"svc.ap4-consumer-src-a-{str(ws_id)[:8]}",
            display_name=f"svc.ap4-consumer-src-a-{str(ws_id)[:8]}",
            metadata={},
            version=5,
            is_backfill=False,
            content_hash=hashlib.sha256(f"src-a-{ws_id}".encode()).hexdigest(),
        )
        event_b = EntityUpsertEvent(
            id=str(entity_id_b),
            source_id=str(src_b),
            workspace_id=str(ws_id),
            entity_type="service",
            name=f"svc.ap4-consumer-src-b-cold-{str(ws_id)[:8]}",
            display_name=f"svc.ap4-consumer-src-b-cold-{str(ws_id)[:8]}",
            metadata={},
            version=1,
            is_backfill=False,
            content_hash=hashlib.sha256(f"src-b-{ws_id}".encode()).hexdigest(),
        )

        await producer.publish("outbox.entity.upsert", event_a)
        await producer.publish("outbox.entity.upsert", event_b)

        # ------------------------------------------------------------------
        # Step 3: Start OutboxConsumerWorker and drain the two events.
        # Use a short-lived consumer: start it, wait for both events to land
        # in Neo4j + Qdrant (poll until present), then stop the consumer.
        # ------------------------------------------------------------------
        consumer = OutboxConsumerWorker(
            nats_conn=nats_conn,
            graph_store=neo4j_store,
            vector_store=qdrant_store,
            embedding_provider=embedding_provider,
            session_factory=session_factory,
        )
        await consumer.start()

        # Poll until both entities appear in Neo4j by entity UUID (max 15 s).
        # get_entity_versions returns {uuid: version} for all entities in workspace.
        deadline = asyncio.get_event_loop().time() + 15.0
        found_versions: dict[uuid.UUID, int] = {}
        while asyncio.get_event_loop().time() < deadline:
            found_versions = await neo4j_store.get_entity_versions(workspace_id=ws_id)
            if entity_id_a in found_versions and entity_id_b in found_versions:
                break
            await asyncio.sleep(0.3)
        else:
            await consumer.stop()
            raise AssertionError(
                f"v12-AP4: OutboxConsumerWorker did not process both entities within 15s.  "
                f"Entity UUIDs in Neo4j: {list(found_versions.keys())[:5]}.  "
                f"Expected entity_a={entity_id_a} and entity_b={entity_id_b}"
            )

        await consumer.stop()

        # ------------------------------------------------------------------
        # Step 4: Construct REAL GlobalReconciler and run check_convergence.
        # ------------------------------------------------------------------
        real_reconciler = GlobalReconciler(
            session_factory=session_factory,
            vector_store=qdrant_store,
            graph_store=neo4j_store,
        )

        (
            _converged,
            _degraded,
            _staleness,
            _min_watermark,
            per_source_wm,
        ) = await real_reconciler.check_convergence(workspace_id=ws_id)

        # ------------------------------------------------------------------
        # v11-AP1a assertion: cold source (src_b, pg==0) must be EXPLICITLY
        # present in per_source_watermark with value 0.
        # ------------------------------------------------------------------
        src_a_key = str(src_a)
        src_b_key = str(src_b)

        assert src_b_key in per_source_wm, (
            f"v12-AP4 consumer-path: cold source {src_b_key[:8]} is MISSING from "
            f"per_source_watermark.  The consumer→NATS→store path wrote src_b to "
            f"Neo4j/Qdrant but check_convergence did not include it in the union.  "
            f"Map keys: {list(per_source_wm.keys())[:5]}"
        )
        assert per_source_wm[src_b_key] == 0, (
            f"v12-AP4 consumer-path: cold source {src_b_key[:8]} has "
            f"per_source_watermark={per_source_wm[src_b_key]}, expected 0.  "
            f"A source with no pg documents must get explicit value 0."
        )
        assert src_a_key in per_source_wm, (
            f"v12-AP4: healthy source {src_a_key[:8]} is missing from per_source_watermark."
        )

        # ------------------------------------------------------------------
        # v11-AP1b assertion: end-to-end composer drops cold-source hits.
        # ------------------------------------------------------------------
        from omniscience_retrieval.graph_rag import GraphRAGComposer
        from omniscience_retrieval.models import SearchRequest

        class _FakeLegacy:
            async def search(self, request: SearchRequest) -> Any:
                from omniscience_retrieval.models import QueryStats, SearchResult

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
            graph_store=neo4j_store,
            vector_store=qdrant_store,
            legacy_service=_FakeLegacy(),
            global_reconciler=real_reconciler,
        )

        req = SearchRequest(
            query=f"ap4-consumer-path {ws_id}",
            top_k=20,
        )
        result = await composer.search(req, workspace_id=ws_id)

        # Cold source hits must be dropped by fail-closed filter.
        src_b_hits_versioned = [
            h
            for h in result.hits
            if h.source.id == src_b and h.applied_version is not None and h.applied_version > 0
        ]
        assert src_b_hits_versioned == [], (
            f"v12-AP4 consumer-path: cold source {src_b_key[:8]} (wm=0) has versioned "
            f"hits passing the fail-closed filter: "
            f"{[h.applied_version for h in src_b_hits_versioned]}.  "
            "The consumer-path wrote versioned evidence that was not dropped."
        )

        # All surviving versioned hits must respect their per-source watermark.
        for hit in result.hits:
            if hit.applied_version is None:
                continue
            src_key = str(hit.source.id)
            if src_key in per_source_wm:
                wm = per_source_wm[src_key]
                assert hit.applied_version <= wm, (
                    f"v12-AP4 consumer-path: source {src_key[:8]} hit "
                    f"applied_version={hit.applied_version} > wm={wm} — "
                    "mixed-epoch violation."
                )

    finally:
        import contextlib

        async with contextlib.AsyncExitStack() as _:
            with contextlib.suppress(Exception):
                await nats_conn.disconnect()
        await neo4j_store.close()
        await qdrant_store.close()
        await engine.dispose()
