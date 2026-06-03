"""Tests for issue #126 — ingestion worker drives Neo4j + Qdrant writes.

Coverage map (per the issue's acceptance criteria):

* Pipeline-side three-store fan-out
    - ``upsert_document`` (Postgres + Qdrant) called with workspace_id,
      chunk lineage fields populated, embedding present.
    - ``upsert_graph`` (Neo4j) called with workspace_id stamped on
      every entity's metadata.
    - Call order: ``upsert_document`` before ``upsert_graph`` — this is
      the observable pipeline ordering (the IndexWriter fans out
      Postgres→Qdrant inside ``upsert_document``; Neo4j is reached via
      the separate ``upsert_graph`` call that follows).

    NOTE: Qdrant fan-out (``vector_store.upsert_chunks``) and Neo4j fan-out
    (``graph_store.upsert_graph``) are now internal to the concrete
    ``IndexWriter`` (packages/index/writer.py), not directly observable at
    the pipeline layer.  The pipeline tests therefore assert against the
    orchestrated ``index_writer`` calls (``upsert_document`` and
    ``upsert_graph``).  Coverage of the concrete IndexWriter's fan-out lives
    in ``tests/test_index_writer.py``.

* ACL invariant
    - Tenant-less source: worker returns ``action="error"`` with a
      ``workspace_resolution_failed`` log AND the broker NAK is fired.
    - Missing source row: same fail-closed outcome (no silent default).
    - Adapter raises ``ValueError("upsert_graph_missing_workspace_id")``
      (Neo4j adapter's actual error shape) — propagates as a hard
      failure; pipeline returns ``action="error"``.

* Partial-failure semantics
    - ``upsert_document`` raises (covers Postgres/Qdrant failure) -> action="error".
    - ``upsert_document`` OK + ``upsert_graph`` raises -> action="error".

All tests run with in-memory mocks of the protocols; no real Neo4j /
Qdrant / Postgres is started.  The opt-in integration test lives in
``test_ingestion_e2e.py`` and is gated on
``OMNISCIENCE_RUN_E2E_INGESTION=1``.

Assertions retargeted from previous store-level mocks
------------------------------------------------------
Previously ``test_qdrant_receives_workspace_id_in_metadata`` asserted on
``vector_store.upsert_chunks(..., metadata={"workspace_id": ...})``.  That
call is now internal to ``IndexWriter.upsert_document``; the equivalent
pipeline-layer assertion is that ``index_writer.upsert_document`` receives
``workspace_id=<target>``.

``test_neo4j_entities_tagged_with_workspace_id`` previously asserted on
``graph_store.upsert_graph(entities=[...with workspace_id...])``; now
asserts on ``index_writer.upsert_graph`` receiving those entities
(the tagging happens in IndexWriter.upsert_graph before the Neo4j call).

``test_call_order_postgres_then_vector_then_graph``: the strict
Postgres→Qdrant→Neo4j ordering is now partially internal to
``IndexWriter.upsert_document`` (Postgres first, then Qdrant inside it).
The pipeline-observable order is ``upsert_document`` then ``upsert_graph``.
Full three-way ordering coverage lives in ``tests/test_index_writer.py``.

``test_deleted_action_calls_qdrant_delete``: Qdrant delete is internal to
``IndexWriter.tombstone``. The pipeline-layer assertion is that
``index_writer.tombstone`` is called with the correct ``workspace_id``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.pipeline import IndexWriterProtocol, IngestionPipeline
from omniscience_server.ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Test helpers — keep them local to avoid leaking into the existing
# tests/test_ingestion.py mock set; those fixtures don't model the
# three-store fan-out.
# ---------------------------------------------------------------------------


def _make_event(
    source_id: uuid.UUID | None = None,
    source_type: str = "git",
    external_id: str = "abc/def.py",
    uri: str = "file://abc/def.py",
    action: str = "created",
) -> DocumentChangeEvent:
    return DocumentChangeEvent(
        source_id=source_id or uuid.uuid4(),
        source_type=source_type,
        external_id=external_id,
        uri=uri,
        action=action,  # type: ignore[arg-type]
    )


def _make_connector(content: bytes = b"def foo():\n    pass\n") -> MagicMock:
    from omniscience_connectors.base import DocumentRef, FetchedDocument
    from pydantic import BaseModel

    class _EmptyConfig(BaseModel):
        pass

    connector = MagicMock()
    connector.config_schema = _EmptyConfig
    ref = DocumentRef(external_id="abc/def.py", uri="file://abc/def.py")
    fetched = FetchedDocument(ref=ref, content_bytes=content, content_type="text/plain")
    connector.fetch = AsyncMock(return_value=fetched)
    return connector


def _make_embedding_provider(
    vectors: list[list[float]] | None = None,
) -> MagicMock:
    provider = MagicMock()
    provider.dim = 4
    provider.model_name = "test-model"
    provider.provider_name = "test-provider"
    provider.embed = AsyncMock(return_value=vectors or [[0.1, 0.2, 0.3, 0.4]])
    return provider


def _make_index_writer(action: str = "created") -> MagicMock:
    """Return a mock IndexWriter satisfying IndexWriterProtocol.

    ``upsert_document`` covers the Postgres + Qdrant fan-out path.
    ``upsert_graph`` covers the Neo4j path.
    Both are stubbed so individual tests can assert on either.
    """
    result = MagicMock()
    result.action = action
    result.chunks_written = 1
    result.document_id = uuid.uuid4()
    writer = MagicMock(spec=IndexWriterProtocol)
    writer.upsert_document = AsyncMock(return_value=result)
    writer.upsert_graph = AsyncMock(return_value=None)
    writer.tombstone = AsyncMock(return_value=True)
    return writer


def _make_pipeline(
    *,
    connector: MagicMock | None = None,
    embedding_provider: MagicMock | None = None,
    index_writer: MagicMock | None = None,
    graph_extractor: Any | None = None,
) -> IngestionPipeline:
    """Build a pipeline with the orchestrated index_writer API.

    graph_store and vector_store are NOT passed here: they are internal
    to the concrete IndexWriter and are not part of IngestionPipeline's
    public interface.
    """
    return IngestionPipeline(
        connector=connector or _make_connector(),
        embedding_provider=embedding_provider or _make_embedding_provider(),
        index_writer=index_writer or _make_index_writer(),
        graph_extractor=graph_extractor,
    )


def _make_session_factory(source: Any | None) -> MagicMock:
    """Async session factory that yields ``source`` from scalar_one_or_none."""
    inner_session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=source)
    inner_session.execute = AsyncMock(return_value=scalar_result)

    factory_cm = MagicMock()
    factory_cm.__aenter__ = AsyncMock(return_value=inner_session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=factory_cm)
    return factory


def _make_source(tenant_id: uuid.UUID | None) -> Any:
    src = MagicMock()
    src.id = uuid.uuid4()
    src.name = "issue-126-test-source"
    src.tenant_id = tenant_id
    src.config = {}
    src.secrets_ref = None
    return src


_WORKSPACE_A: uuid.UUID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Pipeline three-store fan-out
# ---------------------------------------------------------------------------


class TestPipelineThreeStoreFanOut:
    @pytest.mark.asyncio
    async def test_postgres_qdrant_neo4j_all_called(self) -> None:
        """The pipeline calls upsert_document (Postgres+Qdrant) and
        upsert_graph (Neo4j) when entities exist.

        Previously this asserted on three separate store mocks.  Under the
        orchestrated design both calls go through index_writer.
        """
        index_writer = _make_index_writer()

        # Stub graph extractor so _stage_graph reaches the adapter call.
        def _extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
            ent = MagicMock()
            ent.id = uuid.uuid4()
            ent.metadata = {}
            return [ent], []

        # Force can_handle("", ".py") to True via stubbed parser.
        with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
            parser = MagicMock()
            parser.can_handle = MagicMock(return_value=True)
            parsed_doc = MagicMock()
            parser.parse = MagicMock(return_value=parsed_doc)
            parser_cls.return_value = parser

            pipeline = _make_pipeline(
                index_writer=index_writer,
                graph_extractor=_extractor,
            )
            event = _make_event()
            result = await pipeline.run(
                event=event,
                config=None,
                secrets={},
                workspace_id=_WORKSPACE_A,
            )

        assert result.action == "created"
        # Postgres+Qdrant fan-out via upsert_document
        index_writer.upsert_document.assert_awaited_once()
        # Neo4j fan-out via upsert_graph
        index_writer.upsert_graph.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_qdrant_receives_workspace_id_in_metadata(self) -> None:
        """upsert_document is called with workspace_id (ADR-0006 §ACL).

        Previously asserted on vector_store.upsert_chunks metadata.  The
        orchestrated IndexWriter carries workspace_id into Qdrant internally;
        the pipeline-layer guarantee is that upsert_document receives the
        correct workspace_id kwarg.
        """
        index_writer = _make_index_writer()
        pipeline = _make_pipeline(index_writer=index_writer)
        event = _make_event()
        await pipeline.run(event=event, config=None, secrets={}, workspace_id=_WORKSPACE_A)

        kwargs = index_writer.upsert_document.call_args.kwargs
        assert kwargs["workspace_id"] == _WORKSPACE_A
        assert kwargs["source_id"] == event.source_id
        assert kwargs["external_id"] == event.external_id

    @pytest.mark.asyncio
    async def test_qdrant_payload_contains_lineage_and_embedding(self) -> None:
        """Chunks passed to upsert_document carry embedding + ADR-0006 lineage fields.

        Previously asserted on vector_store.upsert_chunks(..., chunks=[...]).
        Now asserts on the chunks list passed to index_writer.upsert_document.
        """
        index_writer = _make_index_writer()
        provider = _make_embedding_provider(vectors=[[0.5, 0.6, 0.7, 0.8]])
        pipeline = _make_pipeline(index_writer=index_writer, embedding_provider=provider)
        await pipeline.run(event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A)

        kwargs = index_writer.upsert_document.call_args.kwargs
        chunks = kwargs["chunks"]
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.embedding == [0.5, 0.6, 0.7, 0.8]
        assert chunk.embedding_model == "test-model"
        assert chunk.embedding_provider == "test-provider"
        assert chunk.parser_version == "placeholder-v0"
        assert chunk.chunker_strategy == "full-content-v0"
        assert chunk.ord == 0
        assert hasattr(chunk, "text")

    @pytest.mark.asyncio
    async def test_qdrant_metadata_includes_content_hash_arg(self) -> None:
        """content_hash kwarg passed to upsert_document is consistent.

        Previously compared vector_store and index_writer content_hash kwargs.
        Both are now part of the same upsert_document call.
        """
        index_writer = _make_index_writer()
        pipeline = _make_pipeline(index_writer=index_writer)
        await pipeline.run(event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A)

        pg_kwargs = index_writer.upsert_document.call_args.kwargs
        # content_hash must be a non-empty hex string
        assert pg_kwargs["content_hash"]
        assert len(pg_kwargs["content_hash"]) == 64  # SHA-256 hex

    @pytest.mark.asyncio
    async def test_neo4j_entities_tagged_with_workspace_id(self) -> None:
        """Every entity passed to upsert_graph carries workspace_id (ACL tag).

        Previously asserted on graph_store.upsert_graph entities kwarg.
        Now asserts on index_writer.upsert_graph which is what the pipeline
        calls after extraction.  The concrete IndexWriter then stamps
        workspace_id on entities before forwarding to Neo4j.
        """
        index_writer = _make_index_writer()

        def _extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
            entities = []
            for _ in range(3):
                e = MagicMock()
                e.id = uuid.uuid4()
                e.metadata = {}
                entities.append(e)
            return entities, []

        with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
            parser = MagicMock()
            parser.can_handle = MagicMock(return_value=True)
            parser.parse = MagicMock(return_value=MagicMock())
            parser_cls.return_value = parser

            pipeline = _make_pipeline(index_writer=index_writer, graph_extractor=_extractor)
            await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        kwargs = index_writer.upsert_graph.call_args.kwargs
        # workspace_id must be passed through to upsert_graph for the
        # IndexWriter to tag entities before forwarding to Neo4j.
        assert kwargs["workspace_id"] == _WORKSPACE_A
        assert len(kwargs["entities"]) == 3

    @pytest.mark.asyncio
    async def test_call_order_upsert_document_then_upsert_graph(self) -> None:
        """Pipeline calls upsert_document before upsert_graph.

        Previously verified three discrete calls (postgres/qdrant/neo4j).
        Under the orchestrated design: upsert_document (Postgres+Qdrant
        fan-out, managed by IndexWriter) runs before upsert_graph (Neo4j
        fan-out).  Full three-way Postgres→Qdrant→Neo4j ordering is tested
        in tests/test_index_writer.py at the IndexWriter layer.
        """
        order: list[str] = []
        index_writer = _make_index_writer()

        async def _pg_qdrant_call(**kwargs: Any) -> Any:
            order.append("upsert_document")
            r = MagicMock()
            r.action = "created"
            r.document_id = uuid.uuid4()
            r.chunks_written = 1
            return r

        async def _neo4j_call(**kwargs: Any) -> None:
            order.append("upsert_graph")

        index_writer.upsert_document = AsyncMock(side_effect=_pg_qdrant_call)
        index_writer.upsert_graph = AsyncMock(side_effect=_neo4j_call)

        def _extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
            e = MagicMock()
            e.id = uuid.uuid4()
            e.metadata = {}
            return [e], []

        with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
            parser = MagicMock()
            parser.can_handle = MagicMock(return_value=True)
            parser.parse = MagicMock(return_value=MagicMock())
            parser_cls.return_value = parser

            pipeline = _make_pipeline(
                index_writer=index_writer,
                graph_extractor=_extractor,
            )
            await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        assert order == ["upsert_document", "upsert_graph"]


# ---------------------------------------------------------------------------
# ACL invariant — workspace resolution at the worker boundary
# ---------------------------------------------------------------------------


class TestWorkerWorkspaceResolution:
    def _build_worker(
        self,
        *,
        source: Any,
    ) -> tuple[IngestionWorker, MagicMock]:
        from omniscience_connectors.registry import ConnectorRegistry

        registry: ConnectorRegistry = MagicMock(spec=ConnectorRegistry)
        registry.get = MagicMock(return_value=_make_connector())
        index_writer = _make_index_writer()

        worker = IngestionWorker(
            queue_consumer=MagicMock(),
            connector_registry=registry,
            embedding_provider=_make_embedding_provider(),
            index_writer=index_writer,
            session_factory=_make_session_factory(source),
        )
        return worker, index_writer

    @pytest.mark.asyncio
    async def test_tenant_less_source_returns_error_result(self) -> None:
        """A source with ``tenant_id is None`` must produce ``action='error'``."""
        source = _make_source(tenant_id=None)
        worker, index_writer = self._build_worker(source=source)

        result = await worker.process_document(_make_event())

        assert result.action == "error"
        assert result.error is not None
        assert "tenant_id" in result.error
        # No adapter should have been touched — fail closed BEFORE any I/O.
        index_writer.upsert_document.assert_not_awaited()
        index_writer.upsert_graph.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_source_row_returns_error_result(self) -> None:
        """A source row that no longer exists must be treated as missing workspace."""
        worker, index_writer = self._build_worker(source=None)

        result = await worker.process_document(_make_event())

        assert result.action == "error"
        index_writer.upsert_document.assert_not_awaited()
        index_writer.upsert_graph.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_workspace_propagates_tenant_id(self) -> None:
        """The resolved workspace_id is exactly ``Source.tenant_id``."""
        target = uuid.uuid4()
        source = _make_source(tenant_id=target)
        worker, index_writer = self._build_worker(source=source)

        await worker.process_document(_make_event())

        kwargs = index_writer.upsert_document.call_args.kwargs
        assert kwargs["workspace_id"] == target

    @pytest.mark.asyncio
    async def test_resolve_workspace_raises_on_missing_workspace(self) -> None:
        """A source with ``tenant_id is None`` produces ``action='error'``.

        The original test called ``_resolve_workspace`` directly; that helper was
        merged into ``_load_source_and_workspace`` (single DB round-trip). We now
        assert the observable outcome via ``process_document`` instead.
        """
        source = _make_source(tenant_id=None)
        worker, _ = self._build_worker(source=source)

        result = await worker.process_document(_make_event())
        assert result.action == "error"


# ---------------------------------------------------------------------------
# Adapter-side ACL rejection
# ---------------------------------------------------------------------------


class TestAdapterAclRejection:
    @pytest.mark.asyncio
    async def test_qdrant_missing_workspace_propagates(self) -> None:
        """index_writer.upsert_document raising on missing workspace_id surfaces as action='error'.

        Previously this raised inside vector_store.upsert_chunks.  Now the
        same error propagates via index_writer.upsert_document (which is
        where the Qdrant call lives in the concrete IndexWriter).
        """
        index_writer = _make_index_writer()
        index_writer.upsert_document = AsyncMock(
            side_effect=ValueError(
                "QdrantVectorStore upsert rejected: metadata['workspace_id'] "
                "is required and must be a non-null UUID (ADR-0006 §ACL)."
            )
        )
        pipeline = _make_pipeline(index_writer=index_writer)
        result = await pipeline.run(
            event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
        )
        assert result.action == "error"
        assert "workspace_id" in (result.error or "")

    @pytest.mark.asyncio
    async def test_neo4j_missing_workspace_propagates(self) -> None:
        """index_writer.upsert_graph raising on missing workspace_id surfaces as action='error'.

        Previously raised inside graph_store.upsert_graph directly.  Now
        the same error propagates via index_writer.upsert_graph.
        """
        index_writer = _make_index_writer()
        index_writer.upsert_graph = AsyncMock(
            side_effect=ValueError("upsert_graph_missing_workspace_id")
        )

        def _extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
            e = MagicMock()
            e.id = uuid.uuid4()
            e.metadata = {}
            return [e], []

        with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
            parser = MagicMock()
            parser.can_handle = MagicMock(return_value=True)
            parser.parse = MagicMock(return_value=MagicMock())
            parser_cls.return_value = parser

            pipeline = _make_pipeline(index_writer=index_writer, graph_extractor=_extractor)
            result = await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        assert result.action == "error"
        assert "workspace" in (result.error or "")


# ---------------------------------------------------------------------------
# Partial-failure semantics
# ---------------------------------------------------------------------------


class TestPartialFailureSemantics:
    @pytest.mark.asyncio
    async def test_upsert_document_fails_returns_error(self) -> None:
        """index_writer.upsert_document raises -> pipeline returns 'error'.

        This covers the Postgres or Qdrant failure path (both are inside
        upsert_document in the concrete IndexWriter).  Re-delivery is safe
        because both writers are idempotent.
        """
        index_writer = _make_index_writer()
        index_writer.upsert_document = AsyncMock(side_effect=RuntimeError("index down"))

        pipeline = _make_pipeline(index_writer=index_writer)
        result = await pipeline.run(
            event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
        )

        assert result.action == "error"
        index_writer.upsert_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_document_ok_upsert_graph_fails_returns_error(self) -> None:
        """upsert_document succeeds; upsert_graph (Neo4j) fails -> pipeline returns 'error'."""
        index_writer = _make_index_writer()
        index_writer.upsert_graph = AsyncMock(side_effect=RuntimeError("neo4j down"))

        def _extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
            e = MagicMock()
            e.id = uuid.uuid4()
            e.metadata = {}
            return [e], []

        with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
            parser = MagicMock()
            parser.can_handle = MagicMock(return_value=True)
            parser.parse = MagicMock(return_value=MagicMock())
            parser_cls.return_value = parser

            pipeline = _make_pipeline(
                index_writer=index_writer,
                graph_extractor=_extractor,
            )
            result = await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        assert result.action == "error"
        index_writer.upsert_document.assert_awaited_once()
        index_writer.upsert_graph.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parser_failure_does_not_abort_ingestion(self) -> None:
        """Parser/extractor errors stay best-effort — upsert_document still succeeds."""
        index_writer = _make_index_writer()

        def _broken_extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
            raise RuntimeError("extractor blew up")

        with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
            parser = MagicMock()
            parser.can_handle = MagicMock(return_value=True)
            parser.parse = MagicMock(return_value=MagicMock())
            parser_cls.return_value = parser

            pipeline = _make_pipeline(
                index_writer=index_writer,
                graph_extractor=_broken_extractor,
            )
            result = await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        assert result.action == "created"
        index_writer.upsert_document.assert_awaited_once()
        # upsert_graph must NOT have been called (extractor blew up before entities)
        index_writer.upsert_graph.assert_not_awaited()


# ---------------------------------------------------------------------------
# Worker NAK behaviour on resolution failure
# ---------------------------------------------------------------------------


class TestWorkerNakOnResolutionFailure:
    @pytest.mark.asyncio
    async def test_tenant_less_source_results_in_nak(self) -> None:
        """The broker NAKs when workspace resolution fails so the DLQ pipeline runs."""
        from omniscience_connectors.registry import ConnectorRegistry

        registry: ConnectorRegistry = MagicMock(spec=ConnectorRegistry)
        registry.get = MagicMock(return_value=_make_connector())
        source = _make_source(tenant_id=None)

        event = _make_event()
        msg = MagicMock()
        msg.payload = event
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()

        consumer = MagicMock()

        async def _iter() -> Any:
            yield msg

        consumer.__aiter__ = MagicMock(return_value=_iter())
        consumer.stop = MagicMock()

        worker = IngestionWorker(
            queue_consumer=consumer,
            connector_registry=registry,
            embedding_provider=_make_embedding_provider(),
            index_writer=_make_index_writer(),
            session_factory=_make_session_factory(source),
        )

        with (
            patch.object(worker._run_tracker, "start", AsyncMock(return_value=uuid.uuid4())),
            patch.object(worker._run_tracker, "record_error", AsyncMock()),
            patch.object(worker._run_tracker, "finish", AsyncMock()),
        ):
            await worker.start()

        msg.nak.assert_awaited_once()
        msg.ack.assert_not_awaited()


# ---------------------------------------------------------------------------
# Deletion path — tombstone carries workspace_id through index_writer
# ---------------------------------------------------------------------------


class TestDeletePath:
    @pytest.mark.asyncio
    async def test_deleted_action_calls_tombstone_with_workspace_id(self) -> None:
        """A 'deleted' event calls index_writer.tombstone with the correct workspace_id.

        Previously asserted on vector_store.delete_by_document.  The Qdrant
        deletion is now internal to IndexWriter.tombstone; the pipeline-layer
        guarantee is that index_writer.tombstone receives the correct
        workspace_id so the IndexWriter can propagate it to Qdrant.
        """
        index_writer = _make_index_writer()
        pipeline = _make_pipeline(index_writer=index_writer)

        event = _make_event(action="deleted")
        result = await pipeline.run(
            event=event, config=None, secrets={}, workspace_id=_WORKSPACE_A
        )
        assert result.action == "deleted"
        index_writer.tombstone.assert_awaited_once_with(
            event.source_id, event.external_id, workspace_id=_WORKSPACE_A
        )


# ---------------------------------------------------------------------------
# Smoke test: assert the worker passes the resolved workspace into the
# pipeline.run kwargs (catches a regression where the kwarg is dropped).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_passes_workspace_to_pipeline_run() -> None:
    from omniscience_connectors.registry import ConnectorRegistry

    target = uuid.uuid4()
    source = _make_source(tenant_id=target)

    registry: ConnectorRegistry = MagicMock(spec=ConnectorRegistry)
    registry.get = MagicMock(return_value=_make_connector())

    worker = IngestionWorker(
        queue_consumer=MagicMock(),
        connector_registry=registry,
        embedding_provider=_make_embedding_provider(),
        index_writer=_make_index_writer(),
        session_factory=_make_session_factory(source),
    )

    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        from omniscience_server.ingestion.events import ProcessResult

        return ProcessResult(
            source_id=kwargs["event"].source_id,
            external_id=kwargs["event"].external_id,
            action="created",
            duration_ms=1.0,
        )

    with patch(
        "omniscience_server.ingestion.worker.IngestionPipeline.run_ref",
        new=AsyncMock(side_effect=_capture),
    ):
        await worker.process_document(_make_event())

    assert captured["workspace_id"] == target


# ---------------------------------------------------------------------------
# Regression guard for ``_make_session_factory``: the call sequence is
# ``factory()`` -> async-with -> ``execute()``.  call_count == 1 confirms
# the worker resolves the workspace exactly once per document (no
# retries inside ``_resolve_workspace``).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_resolved_exactly_once_per_document() -> None:
    from omniscience_connectors.registry import ConnectorRegistry

    source = _make_source(tenant_id=uuid.uuid4())
    factory = _make_session_factory(source)

    registry: ConnectorRegistry = MagicMock(spec=ConnectorRegistry)
    registry.get = MagicMock(return_value=_make_connector())

    worker = IngestionWorker(
        queue_consumer=MagicMock(),
        connector_registry=registry,
        embedding_provider=_make_embedding_provider(),
        index_writer=_make_index_writer(),
        session_factory=factory,
    )

    await worker.process_document(_make_event())
    assert factory.call_count == 1


# ---------------------------------------------------------------------------
# Helper coverage: prove the call_args kwargs exist (avoids accidental
# regressions to positional args, which would silently break the worker
# when called by the production app DI wiring).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_calls_index_writer_with_keyword_arguments_only() -> None:
    pipeline = _make_pipeline()
    await pipeline.run(event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A)
    # If the production code regresses to positional args, .call_args.kwargs
    # would be empty and the next assertion fails.
    pg_call: call = pipeline._index_writer.upsert_document.call_args  # type: ignore[attr-defined]
    assert pg_call.kwargs, "IndexWriter.upsert_document must be called with kwargs"
