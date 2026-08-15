"""End-to-end ingestion test (issue #126).

Spins up real Neo4j + Qdrant containers via ``testcontainers``, drives a
single document through the live :class:`IngestionPipeline`, and asserts
the document is observable in **both** Neo4j (entities) and Qdrant
(chunks).  Postgres is exercised at the protocol level via a
hand-rolled in-memory writer so the test stays focused on the new
three-store fan-out and does not require a database container.

Gating
------

This test is **opt-in**: it runs only when
``OMNISCIENCE_RUN_E2E_INGESTION=1``.  The default CI pipeline skips it
because:

1. Spinning two containers per run is slow.
2. Some hosted runners cannot pull large container images.

The same gating shape is used by the issue-#104 (Neo4j) and issue-#106
(Qdrant) contract tests — see ``tests/test_neo4j_store.py`` and
``tests/test_qdrant_contract.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

# Skip the entire module unless the operator opted in.
_E2E_FLAG: str = "OMNISCIENCE_RUN_E2E_INGESTION"
if os.environ.get(_E2E_FLAG) != "1":
    pytest.skip(
        f"Set {_E2E_FLAG}=1 to run live ingestion E2E.",
        allow_module_level=True,
    )

# These imports are guarded by the skip above so a missing
# ``testcontainers[neo4j,qdrant]`` extra does not break collection.
pytest.importorskip("testcontainers.qdrant")
pytest.importorskip("testcontainers.neo4j")


from omniscience_core.storage.graph import GraphWriteResult  # noqa: E402  -- gated import
from omniscience_index.stores.neo4j_store import (  # noqa: E402  -- gated import
    Neo4jGraphStore,
    Neo4jStoreConfig,
)
from omniscience_index.stores.qdrant_config import QdrantConfig  # noqa: E402  -- gated
from omniscience_index.stores.qdrant_store import (  # noqa: E402  -- gated import
    QdrantVectorStore,
)
from omniscience_server.ingestion.events import (  # noqa: E402  -- gated import
    DocumentChangeEvent,
)
from omniscience_server.ingestion.pipeline import (  # noqa: E402  -- gated import
    IngestionPipeline,
)

# ---------------------------------------------------------------------------
# Stub embedding provider
# ---------------------------------------------------------------------------


class _StubEmbedding:
    """Tiny deterministic embedding provider for E2E tests."""

    dim = 8
    model_name = "stub-embedding"
    provider_name = "stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import math

        out: list[list[float]] = []
        for t in texts:
            h = hash(t)
            raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(self.dim)]
            n = math.sqrt(sum(x * x for x in raw)) or 1.0
            out.append([x / n for x in raw])
        return out

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# In-memory IndexWriter — the test does not run Postgres.
# ---------------------------------------------------------------------------


class _InMemoryIndexWriter:
    """In-memory orchestrator satisfying ``IndexWriterProtocol``.

    Stands in for the production :class:`omniscience_index.writer.IndexWriter`
    *Postgres* side only (content-hash dedup + document-id stability kept
    in memory so the test needs no Postgres container).  The Neo4j and
    Qdrant fan-out is **not** stubbed: this writer drives the real
    ``graph_store``/``vector_store`` exactly as the production writer does,
    so the pipeline's worker-write path (``index_writer.upsert_document``
    -> Qdrant, ``index_writer.upsert_graph`` -> Neo4j) is exercised
    end-to-end against live containers.
    """

    def __init__(self, graph_store: Any, vector_store: Any) -> None:
        self._documents: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
        self._graph_store = graph_store
        self._vector_store = vector_store

    async def upsert_document(
        self,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[Any],
        workspace_id: uuid.UUID | None = None,
        ingestion_run_id: uuid.UUID | None = None,
    ) -> Any:
        from dataclasses import dataclass

        @dataclass
        class _Result:
            action: str
            document_id: uuid.UUID
            chunks_written: int
            doc_version: int

        key = (source_id, external_id)
        existing = self._documents.get(key)
        if existing is not None and existing["content_hash"] == content_hash:
            return _Result(
                action="unchanged",
                document_id=existing["id"],
                chunks_written=0,
                doc_version=existing["version"],
            )

        if existing is None:
            doc_id = uuid.uuid4()
            self._documents[key] = {"id": doc_id, "content_hash": content_hash, "version": 1}
            action = "created"
        else:
            existing["content_hash"] = content_hash
            existing["version"] += 1
            doc_id = existing["id"]
            action = "updated"

        # Qdrant fan-out — mirrors IndexWriter.upsert_document.
        if self._vector_store is not None and workspace_id is not None:
            vector_metadata = dict(metadata)
            vector_metadata["workspace_id"] = str(workspace_id)
            payloads = [
                {
                    "ord": c.ord,
                    "text": c.text,
                    "embedding": c.embedding,
                    "symbol": c.symbol,
                    "metadata": c.metadata,
                    "embedding_model": c.embedding_model,
                    "embedding_provider": c.embedding_provider,
                    "parser_version": c.parser_version,
                    "chunker_strategy": c.chunker_strategy,
                }
                for c in chunks
            ]
            await self._vector_store.upsert_chunks(
                source_id=source_id,
                external_id=external_id,
                uri=uri,
                title=title,
                content_hash=content_hash,
                metadata=vector_metadata,
                chunks=payloads,
                ingestion_run_id=ingestion_run_id,
            )

        return _Result(
            action=action,
            document_id=doc_id,
            chunks_written=len(chunks),
            doc_version=self._documents[key]["version"],
        )

    async def upsert_graph(
        self,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        workspace_id: uuid.UUID | None = None,
        snapshot_at: Any | None = None,
        version: int | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> GraphWriteResult:
        # Neo4j fan-out — mirrors IndexWriter.upsert_graph, including its
        # return type.  Handing back ``None`` here made this double the only
        # thing in the suite exercising ``getattr(result, "applied", True)``,
        # so that optimistic default was never observed to be wrong.
        if self._graph_store is not None and workspace_id is not None:
            for ent in entities:
                if hasattr(ent, "metadata"):
                    ent.metadata["workspace_id"] = str(workspace_id)
                elif isinstance(ent, dict):
                    ent.setdefault("metadata", {})["workspace_id"] = str(workspace_id)
            return await self._graph_store.upsert_graph(
                source_id=source_id,
                document_id=document_id,
                entities=entities,
                edges=edges,
                workspace_id=workspace_id,
                snapshot_at=snapshot_at,
                version=version,
                epoch=epoch,
                forced_replay=forced_replay,
            )
        return GraphWriteResult(applied=False, skip_reason="no_graph_store")

    async def tombstone(
        self,
        source_id: uuid.UUID,
        external_id: str,
        workspace_id: uuid.UUID | None = None,
    ) -> bool:
        key = (source_id, external_id)
        if key in self._documents:
            del self._documents[key]
            return True
        return False


# ---------------------------------------------------------------------------
# Container fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def neo4j_container() -> Any:
    from testcontainers.neo4j import Neo4jContainer

    # Pass the password through the constructor so the wrapper's _configure()
    # sets NEO4J_AUTH correctly.  Using .with_env("NEO4J_AUTH", ...) is
    # silently overridden by the wrapper's own default — the container would
    # then reject the connection below with an AuthError.  This mirrors every
    # other testcontainers Neo4j fixture in the suite (e.g.
    # tests/test_graph_store_contract.py, tests/integration/test_replay.py).
    with Neo4jContainer("neo4j:5.20", password="test_password") as c:
        yield c


@pytest.fixture(scope="module")
def qdrant_container() -> Any:
    from testcontainers.qdrant import QdrantContainer

    with QdrantContainer(image="qdrant/qdrant:v1.12.4") as c:
        yield c


@pytest_asyncio.fixture()
async def neo4j_store(neo4j_container: Any) -> AsyncIterator[Neo4jGraphStore]:
    config = Neo4jStoreConfig(
        uri=neo4j_container.get_connection_url(),
        username="neo4j",
        password="test_password",
        database="neo4j",
        max_connection_pool_size=4,
        connection_acquisition_timeout_seconds=30.0,
        max_transaction_retry_time_seconds=30.0,
        default_max_depth=2,
    )
    store = Neo4jGraphStore(config=config)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture()
async def qdrant_store(qdrant_container: Any) -> AsyncIterator[QdrantVectorStore]:
    host = qdrant_container.get_container_host_ip()
    http_port = int(qdrant_container.get_exposed_port(6333))
    grpc_port = int(qdrant_container.get_exposed_port(6334))
    config = QdrantConfig(
        host=host,
        grpc_port=grpc_port,
        http_port=http_port,
        api_key=None,
        prefer_grpc=False,
    )
    embedding = _StubEmbedding()
    store = QdrantVectorStore(config=config, embedding_provider=embedding)
    # Unique collection per test run.
    store._collection_name = (  # type: ignore[attr-defined]
        f"omniscience__stub-embedding__d8__t{uuid.uuid4().hex[:8]}"
    )
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingestion_writes_to_all_three_stores(
    neo4j_store: Neo4jGraphStore,
    qdrant_store: QdrantVectorStore,
) -> None:
    """One document goes through the worker; all three stores observe it."""
    from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument

    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()

    # ---- mock connector that returns a small Python file -----------------

    class _StubConnector:
        async def fetch(
            self,
            config: Any,
            secrets: dict[str, str],
            ref: DocumentRef,
        ) -> FetchedDocument:
            return FetchedDocument(
                ref=ref,
                content_bytes=b"def greet():\n    return 'hi'\n",
                content_type="text/x-python",
            )

    connector: Connector = _StubConnector()  # type: ignore[assignment]

    # ---- graph extractor ------------------------------------------------

    def _stub_extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
        from omniscience_parsers.code.graph import ExtractedEntity

        ent = ExtractedEntity(
            id=uuid.uuid4(),
            entity_type="function",
            name="example.greet",
            display_name="greet",
            symbol="greet",
            metadata={},
        )
        return [ent], []

    # The pipeline writes to all three stores *through* the index_writer
    # (the worker-write architecture from #126): Postgres + Qdrant via
    # upsert_document, Neo4j via upsert_graph.  The pipeline itself does
    # not take graph_store/vector_store — those live behind the writer.
    index_writer = _InMemoryIndexWriter(graph_store=neo4j_store, vector_store=qdrant_store)
    pipeline = IngestionPipeline(
        connector=connector,
        embedding_provider=_StubEmbedding(),  # type: ignore[arg-type]
        index_writer=index_writer,  # type: ignore[arg-type]
        graph_extractor=_stub_extractor,
    )

    event = DocumentChangeEvent(
        source_id=source_id,
        source_type="git",
        external_id="example.py",
        uri="file://example.py",
        action="created",  # type: ignore[arg-type]
    )

    # Force the parser to "handle" .py without spinning up tree-sitter
    # (which is heavyweight and orthogonal to this test's purpose).
    with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
        from unittest.mock import MagicMock

        parser = MagicMock()
        parser.can_handle = MagicMock(return_value=True)
        parser.parse = MagicMock(return_value=MagicMock())
        parser_cls.return_value = parser

        result = await pipeline.run(
            event=event,
            config=None,
            secrets={},
            workspace_id=workspace_id,
        )

    assert result.action == "created"

    # ---- assert Qdrant received the chunk -------------------------------
    qdrant_count = await qdrant_store.count(source_id=source_id, workspace_id=workspace_id)
    assert qdrant_count == 1

    # ---- assert Neo4j received the entity -------------------------------
    entity = await neo4j_store.get_entity(entity_name="example.greet", workspace_id=workspace_id)
    assert entity is not None
    assert entity.kind == "function"
