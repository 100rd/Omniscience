"""Tests for issue #126 — ingestion worker drives Neo4j + Qdrant writes.

Coverage map (per the issue's acceptance criteria):

* Pipeline-side three-store fan-out
    - ``upsert_document`` (Postgres) called with the placeholder chunks.
    - ``upsert_chunks`` (Qdrant) called with workspace_id in metadata,
      chunk lineage fields populated, embedding present.
    - ``upsert_graph`` (Neo4j) called with workspace_id stamped on
      every entity's metadata.
    - Call order: Postgres -> Qdrant -> Neo4j (matches the
      pipeline's documented sequence in the module docstring).

* ACL invariant
    - Tenant-less source: worker returns ``action="error"`` with a
      ``workspace_resolution_failed`` log AND the broker NAK is fired.
    - Missing source row: same fail-closed outcome (no silent default).
    - Adapter raises ``ValueError("upsert_graph_missing_workspace_id")``
      (Neo4j adapter's actual error shape) — propagates as a hard
      failure; pipeline returns ``action="error"``.

* Partial-failure semantics
    - Postgres OK + Qdrant raises -> action="error".
    - Postgres OK + Qdrant OK + Neo4j raises -> action="error".

All tests run with in-memory mocks of the protocols; no real Neo4j /
Qdrant / Postgres is started.  The opt-in integration test lives in
``test_ingestion_e2e.py`` and is gated on
``OMNISCIENCE_RUN_E2E_INGESTION=1``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from omniscience_index.workspace import MissingWorkspaceError
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.pipeline import IngestionPipeline
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

    connector = MagicMock()
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
    result = MagicMock()
    result.action = action
    result.chunks_written = 1
    result.document_id = uuid.uuid4()
    writer = MagicMock()
    writer.upsert_document = AsyncMock(return_value=result)
    writer.tombstone = AsyncMock(return_value=True)
    return writer


def _make_graph_store() -> MagicMock:
    store = MagicMock()
    store.upsert_graph = AsyncMock(return_value=None)
    return store


def _make_vector_store() -> MagicMock:
    outcome = MagicMock()
    outcome.action = "created"
    outcome.chunks_written = 1
    outcome.document_id = uuid.uuid4()
    outcome.doc_version = 1
    store = MagicMock()
    store.upsert_chunks = AsyncMock(return_value=outcome)
    store.delete_by_document = AsyncMock(return_value=True)
    return store


def _make_pipeline(
    *,
    connector: MagicMock | None = None,
    embedding_provider: MagicMock | None = None,
    index_writer: MagicMock | None = None,
    graph_store: MagicMock | None = None,
    vector_store: MagicMock | None = None,
    graph_extractor: Any | None = None,
) -> IngestionPipeline:
    return IngestionPipeline(
        connector=connector or _make_connector(),
        embedding_provider=embedding_provider or _make_embedding_provider(),
        index_writer=index_writer or _make_index_writer(),
        graph_store=graph_store or _make_graph_store(),
        vector_store=vector_store or _make_vector_store(),
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
    return src


_WORKSPACE_A: uuid.UUID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Pipeline three-store fan-out
# ---------------------------------------------------------------------------


class TestPipelineThreeStoreFanOut:
    @pytest.mark.asyncio
    async def test_postgres_qdrant_neo4j_all_called(self) -> None:
        """The pipeline writes Postgres + Qdrant + Neo4j when entities exist."""
        index_writer = _make_index_writer()
        graph_store = _make_graph_store()
        vector_store = _make_vector_store()

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
                graph_store=graph_store,
                vector_store=vector_store,
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
        index_writer.upsert_document.assert_awaited_once()
        vector_store.upsert_chunks.assert_awaited_once()
        graph_store.upsert_graph.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_qdrant_receives_workspace_id_in_metadata(self) -> None:
        """Qdrant upsert metadata always carries ``workspace_id`` (ADR-0006 §ACL)."""
        vector_store = _make_vector_store()
        pipeline = _make_pipeline(vector_store=vector_store)
        event = _make_event()
        await pipeline.run(event=event, config=None, secrets={}, workspace_id=_WORKSPACE_A)

        kwargs = vector_store.upsert_chunks.call_args.kwargs
        assert kwargs["metadata"]["workspace_id"] == str(_WORKSPACE_A)
        assert kwargs["source_id"] == event.source_id
        assert kwargs["external_id"] == event.external_id

    @pytest.mark.asyncio
    async def test_qdrant_payload_contains_lineage_and_embedding(self) -> None:
        """ChunkPayload carries the embedding + ADR-0006 lineage fields."""
        vector_store = _make_vector_store()
        provider = _make_embedding_provider(vectors=[[0.5, 0.6, 0.7, 0.8]])
        pipeline = _make_pipeline(vector_store=vector_store, embedding_provider=provider)
        await pipeline.run(event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A)

        kwargs = vector_store.upsert_chunks.call_args.kwargs
        chunks = kwargs["chunks"]
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk["embedding"] == [0.5, 0.6, 0.7, 0.8]
        assert chunk["embedding_model"] == "test-model"
        assert chunk["embedding_provider"] == "test-provider"
        assert chunk["parser_version"] == "placeholder-v0"
        assert chunk["chunker_strategy"] == "full-content-v0"
        assert chunk["ord"] == 0
        assert "text" in chunk

    @pytest.mark.asyncio
    async def test_qdrant_metadata_includes_content_hash_arg(self) -> None:
        """``content_hash`` keyword arg passed to Qdrant matches the Postgres hash."""
        vector_store = _make_vector_store()
        index_writer = _make_index_writer()
        pipeline = _make_pipeline(vector_store=vector_store, index_writer=index_writer)
        await pipeline.run(event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A)

        qdrant_kwargs = vector_store.upsert_chunks.call_args.kwargs
        pg_kwargs = index_writer.upsert_document.call_args.kwargs
        assert qdrant_kwargs["content_hash"] == pg_kwargs["content_hash"]

    @pytest.mark.asyncio
    async def test_neo4j_entities_tagged_with_workspace_id(self) -> None:
        """Every entity passed to ``upsert_graph`` carries ``workspace_id``."""
        graph_store = _make_graph_store()

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

            pipeline = _make_pipeline(graph_store=graph_store, graph_extractor=_extractor)
            await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        kwargs = graph_store.upsert_graph.call_args.kwargs
        for ent in kwargs["entities"]:
            assert ent.metadata["workspace_id"] == str(_WORKSPACE_A)

    @pytest.mark.asyncio
    async def test_call_order_postgres_then_vector_then_graph(self) -> None:
        """Pipeline calls Postgres before Qdrant before Neo4j."""
        order: list[str] = []
        index_writer = _make_index_writer()
        graph_store = _make_graph_store()
        vector_store = _make_vector_store()

        async def _pg_call(**kwargs: Any) -> Any:
            order.append("postgres")
            r = MagicMock()
            r.action = "created"
            r.document_id = uuid.uuid4()
            r.chunks_written = 1
            return r

        async def _qdrant_call(**kwargs: Any) -> Any:
            order.append("qdrant")
            o = MagicMock()
            o.action = "created"
            o.chunks_written = 1
            o.document_id = uuid.uuid4()
            o.doc_version = 1
            return o

        async def _neo4j_call(**kwargs: Any) -> None:
            order.append("neo4j")

        index_writer.upsert_document = AsyncMock(side_effect=_pg_call)
        vector_store.upsert_chunks = AsyncMock(side_effect=_qdrant_call)
        graph_store.upsert_graph = AsyncMock(side_effect=_neo4j_call)

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
                graph_store=graph_store,
                vector_store=vector_store,
                graph_extractor=_extractor,
            )
            await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        assert order == ["postgres", "qdrant", "neo4j"]


# ---------------------------------------------------------------------------
# ACL invariant — workspace resolution at the worker boundary
# ---------------------------------------------------------------------------


class TestWorkerWorkspaceResolution:
    def _build_worker(
        self,
        *,
        source: Any,
    ) -> tuple[IngestionWorker, MagicMock, MagicMock, MagicMock]:
        from omniscience_connectors.registry import ConnectorRegistry

        registry: ConnectorRegistry = MagicMock(spec=ConnectorRegistry)
        registry.get = MagicMock(return_value=_make_connector())
        graph_store = _make_graph_store()
        vector_store = _make_vector_store()
        index_writer = _make_index_writer()

        worker = IngestionWorker(
            queue_consumer=MagicMock(),
            connector_registry=registry,
            embedding_provider=_make_embedding_provider(),
            index_writer=index_writer,
            graph_store=graph_store,
            vector_store=vector_store,
            session_factory=_make_session_factory(source),
        )
        return worker, graph_store, vector_store, index_writer

    @pytest.mark.asyncio
    async def test_tenant_less_source_returns_error_result(self) -> None:
        """A source with ``tenant_id is None`` must produce ``action='error'``."""
        source = _make_source(tenant_id=None)
        worker, graph_store, vector_store, index_writer = self._build_worker(source=source)

        result = await worker.process_document(_make_event())

        assert result.action == "error"
        assert result.error is not None
        assert "tenant_id" in result.error
        # No adapter should have been touched — fail closed BEFORE any I/O.
        graph_store.upsert_graph.assert_not_awaited()
        vector_store.upsert_chunks.assert_not_awaited()
        index_writer.upsert_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_source_row_returns_error_result(self) -> None:
        """A source row that no longer exists must be treated as missing workspace."""
        worker, graph_store, vector_store, index_writer = self._build_worker(source=None)

        result = await worker.process_document(_make_event())

        assert result.action == "error"
        graph_store.upsert_graph.assert_not_awaited()
        vector_store.upsert_chunks.assert_not_awaited()
        index_writer.upsert_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_workspace_propagates_tenant_id(self) -> None:
        """The resolved workspace_id is exactly ``Source.tenant_id``."""
        target = uuid.uuid4()
        source = _make_source(tenant_id=target)
        worker, _graph, vector_store, _ = self._build_worker(source=source)

        await worker.process_document(_make_event())

        kwargs = vector_store.upsert_chunks.call_args.kwargs
        assert kwargs["metadata"]["workspace_id"] == str(target)

    @pytest.mark.asyncio
    async def test_resolve_workspace_raises_on_missing_workspace(self) -> None:
        """``_resolve_workspace`` raises ``MissingWorkspaceError`` for tenant-less sources.

        Direct unit test: catches the helper's behaviour even before the
        process_document call wraps it in an error result.
        """
        source = _make_source(tenant_id=None)
        worker, _, _, _ = self._build_worker(source=source)

        with pytest.raises(MissingWorkspaceError):
            await worker._resolve_workspace(uuid.uuid4())


# ---------------------------------------------------------------------------
# Adapter-side ACL rejection
# ---------------------------------------------------------------------------


class TestAdapterAclRejection:
    @pytest.mark.asyncio
    async def test_qdrant_missing_workspace_propagates(self) -> None:
        """Qdrant's ValueError on missing workspace_id surfaces as action='error'."""
        vector_store = _make_vector_store()
        vector_store.upsert_chunks = AsyncMock(
            side_effect=ValueError(
                "QdrantVectorStore upsert rejected: metadata['workspace_id'] "
                "is required and must be a non-null UUID (ADR-0006 §ACL)."
            )
        )
        pipeline = _make_pipeline(vector_store=vector_store)
        result = await pipeline.run(
            event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
        )
        assert result.action == "error"
        assert "workspace_id" in (result.error or "")

    @pytest.mark.asyncio
    async def test_neo4j_missing_workspace_propagates(self) -> None:
        """Neo4j's ValueError on missing workspace_id surfaces as action='error'."""
        graph_store = _make_graph_store()
        graph_store.upsert_graph = AsyncMock(
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

            pipeline = _make_pipeline(graph_store=graph_store, graph_extractor=_extractor)
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
    async def test_postgres_ok_qdrant_fails_returns_error(self) -> None:
        """Postgres write succeeds; Qdrant raises -> pipeline returns 'error'.

        Re-delivery is safe because both writers are idempotent: Postgres
        dedups by content_hash, Qdrant does the same in its adapter.
        """
        index_writer = _make_index_writer()
        vector_store = _make_vector_store()
        vector_store.upsert_chunks = AsyncMock(side_effect=RuntimeError("qdrant down"))

        pipeline = _make_pipeline(index_writer=index_writer, vector_store=vector_store)
        result = await pipeline.run(
            event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
        )

        assert result.action == "error"
        index_writer.upsert_document.assert_awaited_once()  # Postgres was hit
        vector_store.upsert_chunks.assert_awaited_once()  # Qdrant was attempted

    @pytest.mark.asyncio
    async def test_postgres_ok_qdrant_ok_neo4j_fails_returns_error(self) -> None:
        """Two writes succeed; Neo4j fails -> pipeline returns 'error'."""
        index_writer = _make_index_writer()
        vector_store = _make_vector_store()
        graph_store = _make_graph_store()
        graph_store.upsert_graph = AsyncMock(side_effect=RuntimeError("neo4j down"))

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
                vector_store=vector_store,
                graph_store=graph_store,
                graph_extractor=_extractor,
            )
            result = await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        assert result.action == "error"
        index_writer.upsert_document.assert_awaited_once()
        vector_store.upsert_chunks.assert_awaited_once()
        graph_store.upsert_graph.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parser_failure_does_not_abort_ingestion(self) -> None:
        """Parser/extractor errors stay best-effort — Postgres + Qdrant still succeed."""
        index_writer = _make_index_writer()
        vector_store = _make_vector_store()

        def _broken_extractor(parsed: Any, src_bytes: bytes) -> tuple[list[Any], list[Any]]:
            raise RuntimeError("extractor blew up")

        with patch("omniscience_parsers.TreeSitterParser") as parser_cls:
            parser = MagicMock()
            parser.can_handle = MagicMock(return_value=True)
            parser.parse = MagicMock(return_value=MagicMock())
            parser_cls.return_value = parser

            pipeline = _make_pipeline(
                index_writer=index_writer,
                vector_store=vector_store,
                graph_extractor=_broken_extractor,
            )
            result = await pipeline.run(
                event=_make_event(), config=None, secrets={}, workspace_id=_WORKSPACE_A
            )

        assert result.action == "created"
        index_writer.upsert_document.assert_awaited_once()
        vector_store.upsert_chunks.assert_awaited_once()


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
            graph_store=_make_graph_store(),
            vector_store=_make_vector_store(),
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
# Deletion path also targets Qdrant
# ---------------------------------------------------------------------------


class TestDeletePath:
    @pytest.mark.asyncio
    async def test_deleted_action_calls_qdrant_delete(self) -> None:
        """A 'deleted' event tombstones in Postgres AND Qdrant."""
        index_writer = _make_index_writer()
        vector_store = _make_vector_store()
        pipeline = _make_pipeline(index_writer=index_writer, vector_store=vector_store)

        event = _make_event(action="deleted")
        result = await pipeline.run(
            event=event, config=None, secrets={}, workspace_id=_WORKSPACE_A
        )
        assert result.action == "deleted"
        index_writer.tombstone.assert_awaited_once_with(event.source_id, event.external_id)
        vector_store.delete_by_document.assert_awaited_once()
        kwargs = vector_store.delete_by_document.call_args.kwargs
        assert kwargs["workspace_id"] == _WORKSPACE_A


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
        graph_store=_make_graph_store(),
        vector_store=_make_vector_store(),
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
        "omniscience_server.ingestion.worker.IngestionPipeline.run",
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
        graph_store=_make_graph_store(),
        vector_store=_make_vector_store(),
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
