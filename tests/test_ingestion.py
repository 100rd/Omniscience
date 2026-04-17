"""Tests for the ingestion pipeline.

All external dependencies (queue consumer, connector, embedding provider,
session factory, index writer) are mocked — no real NATS, Postgres, or
embedding service is required.

Coverage:
  - Happy path: fetch → parse → chunk → embed → index
  - Content hash dedup (unchanged)
  - Deleted document → tombstone
  - Error in fetch stage → error result + consumer nak
  - Error in embed stage → error result + consumer nak
  - IngestionRun counter tracking
  - Prometheus metrics increment
  - Graceful stop
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.metrics import (
    INGESTION_DOCUMENTS_PROCESSED_TOTAL,
    INGESTION_ERRORS_TOTAL,
    INGESTION_STAGE_DURATION_SECONDS,
)
from omniscience_server.ingestion.pipeline import IndexWriterProtocol, IngestionPipeline
from omniscience_server.ingestion.run_tracker import RunTracker
from omniscience_server.ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _source_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_event(
    source_id: uuid.UUID | None = None,
    source_type: str = "git",
    external_id: str = "abc/def.py",
    uri: str = "file://abc/def.py",
    action: str = "created",
) -> DocumentChangeEvent:
    return DocumentChangeEvent(
        source_id=source_id or _source_id(),
        source_type=source_type,
        external_id=external_id,
        uri=uri,
        action=action,  # type: ignore[arg-type]
    )


def _make_connector(content: bytes = b"hello world") -> MagicMock:
    """Return a mock Connector whose fetch() returns content."""
    from omniscience_connectors.base import DocumentRef, FetchedDocument

    connector = MagicMock()
    ref = DocumentRef(external_id="abc/def.py", uri="file://abc/def.py")
    fetched = FetchedDocument(ref=ref, content_bytes=content, content_type="text/plain")
    connector.fetch = AsyncMock(return_value=fetched)
    return connector


def _make_embedding_provider(
    vectors: list[list[float]] | None = None,
) -> MagicMock:
    """Return a mock EmbeddingProvider."""
    provider = MagicMock()
    provider.dim = 4
    provider.model_name = "test-model"
    provider.provider_name = "test-provider"
    provider.embed = AsyncMock(return_value=vectors or [[0.1, 0.2, 0.3, 0.4]])
    return provider


def _make_index_writer(upsert_action: str = "created") -> MagicMock:
    """Return a mock IndexWriter satisfying IndexWriterProtocol."""
    result = MagicMock()
    result.action = upsert_action
    result.chunks_written = 1

    writer = MagicMock(spec=IndexWriterProtocol)
    writer.upsert_document = AsyncMock(return_value=result)
    writer.tombstone = AsyncMock(return_value=True)
    return writer


def _make_session_factory() -> MagicMock:
    """Return a minimal async session factory mock."""
    session = AsyncMock()
    session.begin = MagicMock(return_value=session)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    factory = MagicMock()
    factory.return_value = session
    return factory


def _make_pipeline(
    connector: MagicMock | None = None,
    embedding_provider: MagicMock | None = None,
    index_writer: MagicMock | None = None,
) -> IngestionPipeline:
    return IngestionPipeline(
        connector=connector or _make_connector(),
        embedding_provider=embedding_provider or _make_embedding_provider(),
        index_writer=index_writer or _make_index_writer(),
    )


def _get_counter_value(counter: Any, labels: dict[str, str]) -> float:
    try:
        return counter.labels(**labels)._value.get()  # type: ignore[no-any-return]
    except Exception:
        return 0.0


def _get_histogram_count(histogram: Any, labels: dict[str, str]) -> float:
    """Return the observation count for a Histogram with given labels."""
    try:
        from prometheus_client import REGISTRY

        metric_name = histogram._name + "_count"
        for metric in REGISTRY.collect():
            for sample in metric.samples:
                if sample.name == metric_name and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return float(sample.value)
        return 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# IngestionPipeline tests
# ---------------------------------------------------------------------------


class TestIngestionPipelineHappyPath:
    @pytest.mark.asyncio
    async def test_created_document_returns_created_action(self) -> None:
        pipeline = _make_pipeline()
        event = _make_event(action="created")
        result = await pipeline.run(event, config=None, secrets={})
        assert result.action == "created"
        assert result.source_id == event.source_id
        assert result.external_id == event.external_id
        assert result.error is None

    @pytest.mark.asyncio
    async def test_updated_document_returns_updated_action(self) -> None:
        writer = _make_index_writer(upsert_action="updated")
        pipeline = _make_pipeline(index_writer=writer)
        event = _make_event(action="updated")
        result = await pipeline.run(event, config=None, secrets={})
        assert result.action == "updated"

    @pytest.mark.asyncio
    async def test_pipeline_calls_fetch(self) -> None:
        connector = _make_connector()
        pipeline = _make_pipeline(connector=connector)
        event = _make_event()
        await pipeline.run(event, config=None, secrets={})
        connector.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pipeline_calls_embed(self) -> None:
        provider = _make_embedding_provider()
        pipeline = _make_pipeline(embedding_provider=provider)
        event = _make_event()
        await pipeline.run(event, config=None, secrets={})
        provider.embed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pipeline_calls_upsert_document(self) -> None:
        writer = _make_index_writer()
        pipeline = _make_pipeline(index_writer=writer)
        event = _make_event()
        await pipeline.run(event, config=None, secrets={})
        writer.upsert_document.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duration_ms_is_positive(self) -> None:
        pipeline = _make_pipeline()
        event = _make_event()
        result = await pipeline.run(event, config=None, secrets={})
        assert result.duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_upsert_receives_correct_source_id(self) -> None:
        sid = uuid.uuid4()
        writer = _make_index_writer()
        pipeline = _make_pipeline(index_writer=writer)
        event = _make_event(source_id=sid)
        await pipeline.run(event, config=None, secrets={})
        call_kwargs = writer.upsert_document.call_args.kwargs
        assert call_kwargs["source_id"] == sid


class TestIngestionPipelineHashDedup:
    @pytest.mark.asyncio
    async def test_unchanged_content_returns_unchanged_action(self) -> None:
        """When index writer returns 'unchanged', pipeline returns 'unchanged'."""
        writer = _make_index_writer(upsert_action="unchanged")
        pipeline = _make_pipeline(index_writer=writer)
        event = _make_event(action="updated")
        result = await pipeline.run(event, config=None, secrets={})
        assert result.action == "unchanged"

    @pytest.mark.asyncio
    async def test_unchanged_content_still_calls_embed(self) -> None:
        """Embed is still called — dedup is resolved inside upsert_document."""
        writer = _make_index_writer(upsert_action="unchanged")
        provider = _make_embedding_provider()
        pipeline = _make_pipeline(embedding_provider=provider, index_writer=writer)
        event = _make_event(action="updated")
        await pipeline.run(event, config=None, secrets={})
        provider.embed.assert_awaited_once()


class TestIngestionPipelineDeletedDocument:
    @pytest.mark.asyncio
    async def test_deleted_action_tombstones(self) -> None:
        writer = _make_index_writer()
        pipeline = _make_pipeline(index_writer=writer)
        event = _make_event(action="deleted")
        result = await pipeline.run(event, config=None, secrets={})
        assert result.action == "deleted"
        writer.tombstone.assert_awaited_once_with(event.source_id, event.external_id)

    @pytest.mark.asyncio
    async def test_deleted_action_does_not_call_embed(self) -> None:
        provider = _make_embedding_provider()
        pipeline = _make_pipeline(embedding_provider=provider)
        event = _make_event(action="deleted")
        await pipeline.run(event, config=None, secrets={})
        provider.embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleted_not_found_returns_unchanged(self) -> None:
        writer = _make_index_writer()
        writer.tombstone = AsyncMock(return_value=False)
        pipeline = _make_pipeline(index_writer=writer)
        event = _make_event(action="deleted")
        result = await pipeline.run(event, config=None, secrets={})
        assert result.action == "unchanged"


class TestIngestionPipelineFetchError:
    @pytest.mark.asyncio
    async def test_fetch_error_returns_error_result(self) -> None:
        connector = _make_connector()
        connector.fetch = AsyncMock(side_effect=RuntimeError("network timeout"))
        pipeline = _make_pipeline(connector=connector)
        event = _make_event()
        result = await pipeline.run(event, config=None, secrets={})
        assert result.action == "error"
        assert result.error is not None
        assert "network timeout" in result.error

    @pytest.mark.asyncio
    async def test_fetch_error_increments_error_counter(self) -> None:
        source_type = "git_fetch_err_test"
        connector = _make_connector()
        connector.fetch = AsyncMock(side_effect=RuntimeError("boom"))
        pipeline = _make_pipeline(connector=connector)
        event = _make_event(source_type=source_type)

        before = _get_counter_value(
            INGESTION_ERRORS_TOTAL, {"source_type": source_type, "stage": "fetch"}
        )
        await pipeline.run(event, config=None, secrets={})
        after = _get_counter_value(
            INGESTION_ERRORS_TOTAL, {"source_type": source_type, "stage": "fetch"}
        )
        assert after - before == 1.0

    @pytest.mark.asyncio
    async def test_fetch_error_does_not_call_embed(self) -> None:
        connector = _make_connector()
        connector.fetch = AsyncMock(side_effect=RuntimeError("fail"))
        provider = _make_embedding_provider()
        pipeline = _make_pipeline(connector=connector, embedding_provider=provider)
        event = _make_event()
        await pipeline.run(event, config=None, secrets={})
        provider.embed.assert_not_awaited()


class TestIngestionPipelineEmbedError:
    @pytest.mark.asyncio
    async def test_embed_error_returns_error_result(self) -> None:
        provider = _make_embedding_provider()
        provider.embed = AsyncMock(side_effect=RuntimeError("model overloaded"))
        pipeline = _make_pipeline(embedding_provider=provider)
        event = _make_event()
        result = await pipeline.run(event, config=None, secrets={})
        assert result.action == "error"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_embed_error_increments_error_counter(self) -> None:
        source_type = "git_embed_err_test"
        provider = _make_embedding_provider()
        provider.embed = AsyncMock(side_effect=RuntimeError("fail"))
        pipeline = _make_pipeline(embedding_provider=provider)
        event = _make_event(source_type=source_type)

        before = _get_counter_value(
            INGESTION_ERRORS_TOTAL, {"source_type": source_type, "stage": "embed"}
        )
        await pipeline.run(event, config=None, secrets={})
        after = _get_counter_value(
            INGESTION_ERRORS_TOTAL, {"source_type": source_type, "stage": "embed"}
        )
        assert after - before == 1.0

    @pytest.mark.asyncio
    async def test_embed_error_does_not_call_upsert(self) -> None:
        provider = _make_embedding_provider()
        provider.embed = AsyncMock(side_effect=RuntimeError("fail"))
        writer = _make_index_writer()
        pipeline = _make_pipeline(embedding_provider=provider, index_writer=writer)
        event = _make_event()
        await pipeline.run(event, config=None, secrets={})
        writer.upsert_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------


class TestIngestionMetrics:
    @pytest.mark.asyncio
    async def test_stage_duration_recorded_for_fetch(self) -> None:
        pipeline = _make_pipeline()
        event = _make_event()
        before = _get_histogram_count(INGESTION_STAGE_DURATION_SECONDS, {"stage": "fetch"})
        await pipeline.run(event, config=None, secrets={})
        after = _get_histogram_count(INGESTION_STAGE_DURATION_SECONDS, {"stage": "fetch"})
        assert after > before

    @pytest.mark.asyncio
    async def test_stage_duration_recorded_for_embed(self) -> None:
        pipeline = _make_pipeline()
        event = _make_event()
        before = _get_histogram_count(INGESTION_STAGE_DURATION_SECONDS, {"stage": "embed"})
        await pipeline.run(event, config=None, secrets={})
        after = _get_histogram_count(INGESTION_STAGE_DURATION_SECONDS, {"stage": "embed"})
        assert after > before

    @pytest.mark.asyncio
    async def test_stage_duration_recorded_for_index(self) -> None:
        pipeline = _make_pipeline()
        event = _make_event()
        before = _get_histogram_count(INGESTION_STAGE_DURATION_SECONDS, {"stage": "index"})
        await pipeline.run(event, config=None, secrets={})
        after = _get_histogram_count(INGESTION_STAGE_DURATION_SECONDS, {"stage": "index"})
        assert after > before


# ---------------------------------------------------------------------------
# RunTracker tests
# ---------------------------------------------------------------------------


class TestRunTracker:
    def _make_tracker(self, run: Any = None) -> tuple[RunTracker, MagicMock, Any]:
        """Build a RunTracker with a mock session factory.

        Returns (tracker, session_mock, run_mock).

        The factory is called as async with factory() as session, so
        factory() must return an async context manager.  Inside that
        context, session.begin() is also used as an async CM.
        """
        from omniscience_core.db.models import IngestionRun, IngestionRunStatus

        run_obj = run
        if run_obj is None:
            run_obj = MagicMock(spec=IngestionRun)
            run_obj.id = uuid.uuid4()
            run_obj.source_id = _source_id()
            run_obj.docs_new = 0
            run_obj.docs_updated = 0
            run_obj.docs_removed = 0
            run_obj.run_errors = {}
            run_obj.status = IngestionRunStatus.running
            run_obj.finished_at = None

        # inner begin() CM — returned by session.begin()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.begin = MagicMock(return_value=begin_cm)
        session.add = MagicMock()
        session.flush = AsyncMock()

        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none = MagicMock(return_value=run_obj)
        session.execute = AsyncMock(return_value=scalar_result)

        # outer factory() CM
        factory_cm = MagicMock()
        factory_cm.__aenter__ = AsyncMock(return_value=session)
        factory_cm.__aexit__ = AsyncMock(return_value=False)

        factory = MagicMock(return_value=factory_cm)
        tracker = RunTracker(factory)
        return tracker, session, run_obj

    @pytest.mark.asyncio
    async def test_start_inserts_run(self) -> None:
        tracker, session, _ = self._make_tracker()
        run_id = await tracker.start(_source_id())
        session.add.assert_called_once()
        assert isinstance(run_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_record_new_increments_docs_new(self) -> None:
        tracker, _session, run_obj = self._make_tracker()
        run_obj.docs_new = 0
        run_id = uuid.uuid4()
        await tracker.record_new(run_id)
        assert run_obj.docs_new == 1

    @pytest.mark.asyncio
    async def test_record_updated_increments_docs_updated(self) -> None:
        tracker, _session, run_obj = self._make_tracker()
        run_obj.docs_updated = 2
        run_id = uuid.uuid4()
        await tracker.record_updated(run_id)
        assert run_obj.docs_updated == 3

    @pytest.mark.asyncio
    async def test_record_removed_increments_docs_removed(self) -> None:
        tracker, _session, run_obj = self._make_tracker()
        run_obj.docs_removed = 0
        run_id = uuid.uuid4()
        await tracker.record_removed(run_id)
        assert run_obj.docs_removed == 1

    @pytest.mark.asyncio
    async def test_record_error_appends_to_run_errors(self) -> None:
        tracker, _session, run_obj = self._make_tracker()
        run_obj.run_errors = {}
        run_id = uuid.uuid4()
        await tracker.record_error(run_id, "doc/path.py", "embed failed")
        assert "doc/path.py" in run_obj.run_errors
        assert run_obj.run_errors["doc/path.py"] == "embed failed"

    @pytest.mark.asyncio
    async def test_finish_sets_finished_at_and_status_ok(self) -> None:
        from omniscience_core.db.models import IngestionRunStatus

        tracker, _session, run_obj = self._make_tracker()
        run_id = uuid.uuid4()
        await tracker.finish(run_id, had_errors=False)
        assert run_obj.status == IngestionRunStatus.ok
        assert run_obj.finished_at is not None

    @pytest.mark.asyncio
    async def test_finish_sets_partial_status_on_errors(self) -> None:
        from omniscience_core.db.models import IngestionRunStatus

        tracker, _session, run_obj = self._make_tracker()
        run_id = uuid.uuid4()
        await tracker.finish(run_id, had_errors=True)
        assert run_obj.status == IngestionRunStatus.partial


# ---------------------------------------------------------------------------
# IngestionWorker helpers
# ---------------------------------------------------------------------------


def _make_queue_consumer(
    events: list[DocumentChangeEvent] | None = None,
) -> MagicMock:
    """Return a mock QueueConsumer that yields one message per event then stops."""
    consumer = MagicMock()

    async def _iter() -> Any:
        for evt in events or []:
            msg = MagicMock()
            msg.payload = evt
            msg.ack = AsyncMock()
            msg.nak = AsyncMock()
            yield msg

    consumer.__aiter__ = MagicMock(return_value=_iter())
    consumer.stop = MagicMock()
    return consumer


def _make_connector_registry(connector: MagicMock | None = None) -> MagicMock:
    registry = MagicMock()
    registry.get = MagicMock(return_value=connector or _make_connector())
    return registry


def _make_worker(
    events: list[DocumentChangeEvent] | None = None,
    connector: MagicMock | None = None,
    provider: MagicMock | None = None,
    writer: MagicMock | None = None,
) -> tuple[IngestionWorker, MagicMock]:
    queue_consumer = _make_queue_consumer(events or [])
    registry = _make_connector_registry(connector or _make_connector())
    embedding_provider = provider or _make_embedding_provider()
    index_writer = writer or _make_index_writer()
    session_factory = _make_session_factory()

    worker = IngestionWorker(
        queue_consumer=queue_consumer,
        connector_registry=registry,
        embedding_provider=embedding_provider,
        index_writer=index_writer,
        session_factory=session_factory,
    )
    return worker, queue_consumer


# ---------------------------------------------------------------------------
# IngestionWorker tests
# ---------------------------------------------------------------------------


class TestIngestionWorkerHappyPath:
    @pytest.mark.asyncio
    async def test_worker_process_document_returns_created(self) -> None:
        event = _make_event(action="created")
        worker, _ = _make_worker(events=[event])
        result = await worker.process_document(event)
        assert result.action == "created"

    @pytest.mark.asyncio
    async def test_process_document_increments_metric(self) -> None:
        source_type = "git_worker_metric_test"
        event = _make_event(source_type=source_type, action="created")
        worker, _ = _make_worker(events=[event])

        before = _get_counter_value(
            INGESTION_DOCUMENTS_PROCESSED_TOTAL,
            {"source_type": source_type, "action": "created"},
        )
        await worker.process_document(event)
        after = _get_counter_value(
            INGESTION_DOCUMENTS_PROCESSED_TOTAL,
            {"source_type": source_type, "action": "created"},
        )
        assert after - before == 1.0

    @pytest.mark.asyncio
    async def test_start_processes_all_events(self) -> None:
        events = [_make_event(action="created"), _make_event(action="updated")]
        writer = _make_index_writer()

        async def _upsert(**kwargs: Any) -> Any:
            result = MagicMock()
            result.action = "created"
            result.chunks_written = 1
            return result

        writer.upsert_document = AsyncMock(side_effect=_upsert)
        worker, _ = _make_worker(events=events, writer=writer)

        with (
            patch.object(worker._run_tracker, "start", AsyncMock(return_value=uuid.uuid4())),
            patch.object(worker._run_tracker, "record_new", AsyncMock()),
            patch.object(worker._run_tracker, "record_updated", AsyncMock()),
            patch.object(worker._run_tracker, "finish", AsyncMock()),
        ):
            await worker.start()

        assert writer.upsert_document.await_count == 2


class TestIngestionWorkerErrors:
    @pytest.mark.asyncio
    async def test_fetch_error_results_in_nak(self) -> None:
        connector = _make_connector()
        connector.fetch = AsyncMock(side_effect=RuntimeError("fetch failed"))
        event = _make_event()

        msg_mock = MagicMock()
        msg_mock.payload = event
        msg_mock.ack = AsyncMock()
        msg_mock.nak = AsyncMock()

        consumer = MagicMock()

        async def _iter() -> Any:
            yield msg_mock

        consumer.__aiter__ = MagicMock(return_value=_iter())
        consumer.stop = MagicMock()

        worker = IngestionWorker(
            queue_consumer=consumer,
            connector_registry=_make_connector_registry(connector),
            embedding_provider=_make_embedding_provider(),
            index_writer=_make_index_writer(),
            session_factory=_make_session_factory(),
        )

        with (
            patch.object(worker._run_tracker, "start", AsyncMock(return_value=uuid.uuid4())),
            patch.object(worker._run_tracker, "record_error", AsyncMock()),
            patch.object(worker._run_tracker, "finish", AsyncMock()),
        ):
            await worker.start()

        msg_mock.nak.assert_awaited_once()
        msg_mock.ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_embed_error_results_in_nak(self) -> None:
        provider = _make_embedding_provider()
        provider.embed = AsyncMock(side_effect=RuntimeError("embedding down"))
        event = _make_event()

        msg_mock = MagicMock()
        msg_mock.payload = event
        msg_mock.ack = AsyncMock()
        msg_mock.nak = AsyncMock()

        consumer = MagicMock()

        async def _iter() -> Any:
            yield msg_mock

        consumer.__aiter__ = MagicMock(return_value=_iter())
        consumer.stop = MagicMock()

        worker = IngestionWorker(
            queue_consumer=consumer,
            connector_registry=_make_connector_registry(),
            embedding_provider=provider,
            index_writer=_make_index_writer(),
            session_factory=_make_session_factory(),
        )

        with (
            patch.object(worker._run_tracker, "start", AsyncMock(return_value=uuid.uuid4())),
            patch.object(worker._run_tracker, "record_error", AsyncMock()),
            patch.object(worker._run_tracker, "finish", AsyncMock()),
        ):
            await worker.start()

        msg_mock.nak.assert_awaited_once()


class TestIngestionWorkerGracefulStop:
    @pytest.mark.asyncio
    async def test_stop_signals_consumer(self) -> None:
        worker, consumer = _make_worker(events=[])
        await worker.stop()
        consumer.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_finishes_run_if_started(self) -> None:
        run_id = uuid.uuid4()
        worker, _ = _make_worker(events=[])
        worker._run_id = run_id

        with patch.object(worker._run_tracker, "finish", AsyncMock()) as mock_finish:
            await worker.stop()
            mock_finish.assert_awaited_once_with(run_id, had_errors=False)

    @pytest.mark.asyncio
    async def test_stop_does_not_call_finish_if_run_not_started(self) -> None:
        worker, _ = _make_worker(events=[])

        with patch.object(worker._run_tracker, "finish", AsyncMock()) as mock_finish:
            await worker.stop()
            mock_finish.assert_not_awaited()
