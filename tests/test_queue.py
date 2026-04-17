"""Tests for the NATS JetStream queue framework.

All tests mock the nats-py client — no real NATS server is required.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import nats.errors
import nats.js.api
import pytest
from omniscience_core.queue.consumer import QueueConsumer
from omniscience_core.queue.messages import DLQMessage, Message
from omniscience_core.queue.metrics import (
    QUEUE_CONSUMED_TOTAL,
    QUEUE_DLQ_TOTAL,
    QUEUE_PROCESSING_DURATION_SECONDS,
    QUEUE_PUBLISHED_TOTAL,
)
from omniscience_core.queue.producer import QueueProducer
from omniscience_core.queue.streams import (
    INGEST_CHANGES_STREAM,
    INGEST_DLQ_STREAM,
    ensure_streams,
)
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


class _SamplePayload(BaseModel):
    source_id: str
    doc_id: str
    version: int = 1


def _make_js_mock() -> MagicMock:
    """Return a MagicMock configured to behave like a JetStreamContext."""
    js = MagicMock()
    js.publish = AsyncMock()
    js.add_stream = AsyncMock()
    return js


def _make_raw_msg(
    subject: str = "ingest.changes.git",
    data: bytes = b'{"source_id":"s1","doc_id":"d1","version":1}',
    num_delivered: int = 1,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock NATS Msg object.

    The ``metadata`` property on nats.aio.msg.Msg is a regular sync property,
    so we set it as a direct attribute on the MagicMock instance.
    """
    msg = MagicMock()
    msg.subject = subject
    msg.data = data
    msg.headers = headers or {}
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    msg.term = AsyncMock()

    meta = MagicMock()
    meta.num_delivered = num_delivered
    # Direct attribute wins over auto-spec property in MagicMock
    msg.metadata = meta
    return msg


def _make_consumer(
    *,
    subject: str = "ingest.changes.git",
    max_deliver: int = 5,
    durable: str = "test-consumer",
) -> tuple[QueueConsumer[_SamplePayload], MagicMock]:
    """Build a QueueConsumer with a mocked pull subscription."""
    js = _make_js_mock()
    psub = MagicMock()
    psub.fetch = AsyncMock()
    js.pull_subscribe = AsyncMock(return_value=psub)

    consumer: QueueConsumer[_SamplePayload] = QueueConsumer(
        js=js,
        stream=INGEST_CHANGES_STREAM,
        subject=subject,
        durable=durable,
        payload_type=_SamplePayload,
        dlq_subject="ingest.dlq.git",
        max_deliver=max_deliver,
    )
    return consumer, psub


async def _drain_consumer(
    consumer: QueueConsumer[_SamplePayload],
    psub: MagicMock,
    messages: list[MagicMock],
) -> list[Message[_SamplePayload]]:
    """Drive the consumer through *messages* then stop it.

    Sets psub.fetch to return *messages* first, then raise TimeoutError so the
    consumer stops naturally when we call consumer.stop() inside the loop.
    """
    psub.fetch = AsyncMock(side_effect=[messages, nats.errors.TimeoutError()])
    collected: list[Message[_SamplePayload]] = []
    async for msg in consumer:
        collected.append(msg)
        consumer.stop()  # stop after collecting all yielded messages
    return collected


# ---------------------------------------------------------------------------
# QueueProducer tests
# ---------------------------------------------------------------------------


class TestQueueProducer:
    @pytest.mark.asyncio
    async def test_publish_calls_js_publish(self) -> None:
        """publish() should call js.publish with the correct subject and JSON bytes."""
        js = _make_js_mock()
        producer = QueueProducer(js)
        payload = _SamplePayload(source_id="s1", doc_id="d1")

        await producer.publish("ingest.changes.git", payload)

        js.publish.assert_awaited_once()
        call_kwargs = js.publish.call_args.kwargs
        assert call_kwargs["subject"] == "ingest.changes.git"
        assert b"s1" in call_kwargs["payload"]

    @pytest.mark.asyncio
    async def test_publish_serialises_all_fields(self) -> None:
        """Payload JSON must contain all model fields."""
        js = _make_js_mock()
        producer = QueueProducer(js)
        payload = _SamplePayload(source_id="src", doc_id="doc42", version=7)

        await producer.publish("ingest.changes.fs", payload)

        data: bytes = js.publish.call_args.kwargs["payload"]
        decoded = data.decode()
        assert "src" in decoded
        assert "doc42" in decoded
        assert "7" in decoded

    @pytest.mark.asyncio
    async def test_publish_increments_metric(self) -> None:
        """publish() should increment the published counter for the subject."""
        js = _make_js_mock()
        producer = QueueProducer(js)
        subject = "ingest.changes.unique_subject_metric_test"

        before = _get_counter_value(QUEUE_PUBLISHED_TOTAL, {"subject": subject})
        await producer.publish(subject, _SamplePayload(source_id="x", doc_id="y"))
        after = _get_counter_value(QUEUE_PUBLISHED_TOTAL, {"subject": subject})

        assert after - before == 1.0

    @pytest.mark.asyncio
    async def test_publish_raises_on_invalid_type(self) -> None:
        """publish() must raise when given a non-BaseModel object."""
        js = _make_js_mock()
        producer = QueueProducer(js)

        with pytest.raises((TypeError, AttributeError, ValidationError)):
            await producer.publish("ingest.changes.git", "not-a-model")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_publish_propagates_nats_error(self) -> None:
        """Errors from js.publish() must propagate to the caller."""
        js = _make_js_mock()
        js.publish = AsyncMock(side_effect=RuntimeError("nats down"))
        producer = QueueProducer(js)

        with pytest.raises(RuntimeError, match="nats down"):
            await producer.publish("ingest.changes.git", _SamplePayload(source_id="a", doc_id="b"))


# ---------------------------------------------------------------------------
# Message and DLQMessage tests
# ---------------------------------------------------------------------------


class TestMessage:
    def _make_message(self) -> Message[_SamplePayload]:
        ack = AsyncMock()
        nak = AsyncMock()
        term = AsyncMock()
        payload = _SamplePayload(source_id="s1", doc_id="d1")
        return Message(
            payload=payload,
            subject="ingest.changes.git",
            metadata={"header-x": "val"},
            ack=ack,
            nak=nak,
            term=term,
        )

    def test_message_stores_payload(self) -> None:
        msg = self._make_message()
        assert msg.payload.source_id == "s1"

    def test_message_stores_subject(self) -> None:
        msg = self._make_message()
        assert msg.subject == "ingest.changes.git"

    def test_message_stores_metadata(self) -> None:
        msg = self._make_message()
        assert msg.metadata["header-x"] == "val"

    @pytest.mark.asyncio
    async def test_message_ack_calls_callback(self) -> None:
        msg = self._make_message()
        await msg.ack()
        msg._ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_message_nak_calls_callback(self) -> None:
        msg = self._make_message()
        await msg.nak()
        msg._nak.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_message_term_calls_callback(self) -> None:
        msg = self._make_message()
        await msg.term()
        msg._term.assert_awaited_once()

    def test_message_repr(self) -> None:
        msg = self._make_message()
        r = repr(msg)
        assert "ingest.changes.git" in r


class TestDLQMessage:
    def test_dlq_message_required_fields(self) -> None:
        msg = DLQMessage(
            original_subject="ingest.changes.git",
            original_payload='{"x":1}',
            error="exceeded retries",
            attempt_count=5,
        )
        assert msg.original_subject == "ingest.changes.git"
        assert msg.attempt_count == 5
        assert msg.error == "exceeded retries"

    def test_dlq_message_failed_at_defaults_to_utc(self) -> None:
        msg = DLQMessage(
            original_subject="s",
            original_payload="{}",
            error="err",
            attempt_count=1,
        )
        assert msg.failed_at.tzinfo is not None
        assert msg.failed_at.tzinfo == datetime.UTC

    def test_dlq_message_serialises_to_json(self) -> None:
        msg = DLQMessage(
            original_subject="ingest.dlq.git",
            original_payload='{"a":1}',
            error="bad data",
            attempt_count=3,
        )
        json_str = msg.model_dump_json()
        assert "ingest.dlq.git" in json_str
        assert "bad data" in json_str

    def test_dlq_message_roundtrip(self) -> None:
        original = DLQMessage(
            original_subject="sub",
            original_payload="{}",
            error="e",
            attempt_count=2,
        )
        restored = DLQMessage.model_validate_json(original.model_dump_json())
        assert restored.original_subject == original.original_subject
        assert restored.attempt_count == original.attempt_count


# ---------------------------------------------------------------------------
# Stream creation tests
# ---------------------------------------------------------------------------


class TestEnsureStreams:
    @pytest.mark.asyncio
    async def test_creates_both_streams(self) -> None:
        """ensure_streams() must call add_stream for each defined stream."""
        js = _make_js_mock()
        await ensure_streams(js)

        assert js.add_stream.await_count == 2

    @pytest.mark.asyncio
    async def test_stream_names_correct(self) -> None:
        """Stream configs must reference the expected stream names."""
        js = _make_js_mock()
        await ensure_streams(js)

        stream_names = {call.kwargs["config"].name for call in js.add_stream.call_args_list}
        assert INGEST_CHANGES_STREAM in stream_names
        assert INGEST_DLQ_STREAM in stream_names

    @pytest.mark.asyncio
    async def test_stream_subjects_correct(self) -> None:
        """Each stream config must have the correct subject filter."""
        js = _make_js_mock()
        await ensure_streams(js)

        subjects_by_name: dict[str, list[str]] = {}
        for call in js.add_stream.call_args_list:
            cfg = call.kwargs["config"]
            subjects_by_name[cfg.name] = cfg.subjects

        assert subjects_by_name[INGEST_CHANGES_STREAM] == ["ingest.changes.*"]
        assert subjects_by_name[INGEST_DLQ_STREAM] == ["ingest.dlq.*"]

    @pytest.mark.asyncio
    async def test_idempotent_on_bad_request_error(self) -> None:
        """ensure_streams() must silently handle stream-already-exists errors."""
        from nats.js.errors import BadRequestError

        js = _make_js_mock()
        js.add_stream = AsyncMock(side_effect=BadRequestError())

        # Should not raise
        await ensure_streams(js)

    @pytest.mark.asyncio
    async def test_stream_max_age_seven_days(self) -> None:
        """Both streams must have a 7-day max_age in nanoseconds."""
        seven_days_ns = 7 * 24 * 60 * 60 * 10**9
        js = _make_js_mock()
        await ensure_streams(js)

        for call in js.add_stream.call_args_list:
            cfg = call.kwargs["config"]
            assert cfg.max_age == seven_days_ns

    @pytest.mark.asyncio
    async def test_storage_type_is_file(self) -> None:
        """Both streams must use FILE storage."""
        js = _make_js_mock()
        await ensure_streams(js)

        for call in js.add_stream.call_args_list:
            cfg = call.kwargs["config"]
            assert cfg.storage == nats.js.api.StorageType.FILE


# ---------------------------------------------------------------------------
# QueueConsumer tests
# ---------------------------------------------------------------------------


class TestQueueConsumerMessages:
    @pytest.mark.asyncio
    async def test_yields_typed_message(self) -> None:
        """Consumer must yield Message[T] with correctly typed payload."""
        consumer, psub = _make_consumer()
        raw = _make_raw_msg(num_delivered=1)

        messages = await _drain_consumer(consumer, psub, [raw])

        assert len(messages) == 1
        assert messages[0].payload.source_id == "s1"
        assert messages[0].subject == "ingest.changes.git"

    @pytest.mark.asyncio
    async def test_routes_to_dlq_on_max_deliver(self) -> None:
        """Messages at or beyond max_deliver must be routed to the DLQ."""
        consumer, psub = _make_consumer(max_deliver=3)
        raw = _make_raw_msg(num_delivered=3)  # exactly at the limit
        psub.fetch = AsyncMock(side_effect=[[raw], nats.errors.TimeoutError()])

        with patch.object(consumer._dlq_producer, "publish", new_callable=AsyncMock) as mock_pub:
            consumer.stop()  # stop after first batch — no yielded messages expected
            async for _ in consumer:
                pass

        mock_pub.assert_awaited_once()
        dlq_call = mock_pub.call_args
        assert dlq_call.args[0] == "ingest.dlq.git"
        dlq_msg: DLQMessage = dlq_call.args[1]
        assert dlq_msg.original_subject == "ingest.changes.git"
        assert dlq_msg.attempt_count == 3

    @pytest.mark.asyncio
    async def test_terms_message_after_dlq_routing(self) -> None:
        """After routing to DLQ, the raw message must be term()ed."""
        consumer, psub = _make_consumer(max_deliver=3)
        raw = _make_raw_msg(num_delivered=3)
        psub.fetch = AsyncMock(side_effect=[[raw], nats.errors.TimeoutError()])

        with patch.object(consumer._dlq_producer, "publish", new_callable=AsyncMock):
            consumer.stop()
            async for _ in consumer:
                pass

        raw.term.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_naks_on_decode_error(self) -> None:
        """Messages with invalid JSON must be nak()ed."""
        consumer, psub = _make_consumer()
        raw = _make_raw_msg(data=b"not-json", num_delivered=1)
        psub.fetch = AsyncMock(side_effect=[[raw], nats.errors.TimeoutError()])

        consumer.stop()
        async for _ in consumer:
            pass

        raw.nak.assert_awaited_once()

    def test_stop_sets_running_false(self) -> None:
        """stop() must mark the consumer as not running."""
        consumer, _ = _make_consumer()
        assert consumer._running is True
        consumer.stop()
        assert consumer._running is False

    @pytest.mark.asyncio
    async def test_continues_on_timeout(self) -> None:
        """Consumer must not crash on fetch timeout — it loops and retries."""
        consumer, psub = _make_consumer()
        raw = _make_raw_msg(num_delivered=1)
        # First fetch times out, second returns a message
        psub.fetch = AsyncMock(
            side_effect=[nats.errors.TimeoutError(), [raw], nats.errors.TimeoutError()]
        )

        messages: list[Message[_SamplePayload]] = []
        async for msg in consumer:
            messages.append(msg)
            consumer.stop()
            break

        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_does_not_yield_dlq_messages(self) -> None:
        """Messages routed to DLQ must NOT be yielded to the caller."""
        consumer, psub = _make_consumer(max_deliver=2)
        raw = _make_raw_msg(num_delivered=2)
        psub.fetch = AsyncMock(side_effect=[[raw], nats.errors.TimeoutError()])

        with patch.object(consumer._dlq_producer, "publish", new_callable=AsyncMock):
            consumer.stop()
            yielded = [msg async for msg in consumer]

        assert len(yielded) == 0


# ---------------------------------------------------------------------------
# Metrics increment tests
# ---------------------------------------------------------------------------


class TestMetricsIncrements:
    @pytest.mark.asyncio
    async def test_published_counter_increments(self) -> None:
        js = _make_js_mock()
        producer = QueueProducer(js)
        subject = "ingest.changes.metrics_inc_test"

        before = _get_counter_value(QUEUE_PUBLISHED_TOTAL, {"subject": subject})
        await producer.publish(subject, _SamplePayload(source_id="a", doc_id="b"))
        after = _get_counter_value(QUEUE_PUBLISHED_TOTAL, {"subject": subject})

        assert after - before == 1.0

    @pytest.mark.asyncio
    async def test_dlq_counter_increments(self) -> None:
        """DLQ counter must increment when a message is routed to the DLQ."""
        subject = "ingest.changes.dlq_metric_test_2"
        consumer, psub = _make_consumer(subject=subject, max_deliver=3, durable="dlq-durable-2")
        raw = _make_raw_msg(subject=subject, num_delivered=3)
        psub.fetch = AsyncMock(side_effect=[[raw], nats.errors.TimeoutError()])

        before = _get_counter_value(QUEUE_DLQ_TOTAL, {"subject": subject})

        with patch.object(consumer._dlq_producer, "publish", new_callable=AsyncMock):
            consumer.stop()
            async for _ in consumer:
                pass

        after = _get_counter_value(QUEUE_DLQ_TOTAL, {"subject": subject})
        assert after - before == 1.0

    @pytest.mark.asyncio
    async def test_consumed_counter_increments_on_delivered(self) -> None:
        """Consumed counter must increment with status=delivered for normal messages."""
        subject = "ingest.changes.consumed_metric_test_2"
        consumer, psub = _make_consumer(subject=subject, durable="consumed-durable-2")
        raw = _make_raw_msg(subject=subject, num_delivered=1)

        before = _get_counter_value(
            QUEUE_CONSUMED_TOTAL, {"subject": subject, "status": "delivered"}
        )
        await _drain_consumer(consumer, psub, [raw])
        after = _get_counter_value(
            QUEUE_CONSUMED_TOTAL, {"subject": subject, "status": "delivered"}
        )
        assert after - before == 1.0

    def test_processing_duration_histogram_exists(self) -> None:
        """The processing duration histogram must be importable and named correctly."""
        assert QUEUE_PROCESSING_DURATION_SECONDS._name == (
            "omniscience_queue_processing_duration_seconds"
        )


# ---------------------------------------------------------------------------
# NatsConnection tests
# ---------------------------------------------------------------------------


class TestNatsConnection:
    @pytest.mark.asyncio
    async def test_connect_sets_is_connected(self) -> None:
        """After connect(), is_connected must be True."""
        from omniscience_core.config import Settings
        from omniscience_core.queue.connection import NatsConnection

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.jetstream = MagicMock(return_value=MagicMock())

        with patch(
            "omniscience_core.queue.connection.nats.connect", new_callable=AsyncMock
        ) as mock_connect:
            mock_connect.return_value = mock_client

            conn = NatsConnection()
            settings = Settings(nats_url="nats://localhost:4222")
            await conn.connect(settings)

            assert conn.is_connected is True
            mock_connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_idempotent(self) -> None:
        """Calling connect() twice must not open a second connection."""
        from omniscience_core.config import Settings
        from omniscience_core.queue.connection import NatsConnection

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.jetstream = MagicMock(return_value=MagicMock())

        with patch(
            "omniscience_core.queue.connection.nats.connect", new_callable=AsyncMock
        ) as mock_connect:
            mock_connect.return_value = mock_client

            conn = NatsConnection()
            settings = Settings(nats_url="nats://localhost:4222")
            await conn.connect(settings)
            await conn.connect(settings)

            mock_connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_drains_client(self) -> None:
        """disconnect() must call drain() on the client."""
        from omniscience_core.config import Settings
        from omniscience_core.queue.connection import NatsConnection

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.drain = AsyncMock()
        mock_client.connected_url = MagicMock()
        mock_client.connected_url.netloc = "localhost:4222"
        mock_client.jetstream = MagicMock(return_value=MagicMock())

        with patch(
            "omniscience_core.queue.connection.nats.connect", new_callable=AsyncMock
        ) as mock_connect:
            mock_connect.return_value = mock_client

            conn = NatsConnection()
            settings = Settings(nats_url="nats://localhost:4222")
            await conn.connect(settings)
            await conn.disconnect()

            mock_client.drain.assert_awaited_once()

    def test_client_raises_when_not_connected(self) -> None:
        """Accessing .client before connect() must raise RuntimeError."""
        from omniscience_core.queue.connection import NatsConnection

        conn = NatsConnection()
        with pytest.raises(RuntimeError, match="not connected"):
            _ = conn.client

    def test_jetstream_raises_when_not_connected(self) -> None:
        """Accessing .jetstream before connect() must raise RuntimeError."""
        from omniscience_core.queue.connection import NatsConnection

        conn = NatsConnection()
        with pytest.raises(RuntimeError, match="not connected"):
            _ = conn.jetstream


# ---------------------------------------------------------------------------
# Health check integration
# ---------------------------------------------------------------------------


class TestHealthNatsCheck:
    @pytest.mark.asyncio
    async def test_check_nats_healthy_when_connected(self) -> None:
        from omniscience_core.queue.connection import NatsConnection
        from omniscience_server.routes.health import _check_nats

        conn = MagicMock(spec=NatsConnection)
        conn.is_connected = True

        result = await _check_nats(conn)
        assert result == "healthy"

    @pytest.mark.asyncio
    async def test_check_nats_unhealthy_when_disconnected(self) -> None:
        from omniscience_core.queue.connection import NatsConnection
        from omniscience_server.routes.health import _check_nats

        conn = MagicMock(spec=NatsConnection)
        conn.is_connected = False

        result = await _check_nats(conn)
        assert result == "unhealthy"

    @pytest.mark.asyncio
    async def test_check_nats_unchecked_when_none(self) -> None:
        from omniscience_server.routes.health import _check_nats

        result = await _check_nats(None)
        assert result == "unchecked"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_counter_value(counter: Any, labels: dict[str, str]) -> float:
    """Read the current value of a prometheus_client Counter for given labels."""
    try:
        return counter.labels(**labels)._value.get()  # type: ignore[no-any-return]
    except Exception:
        return 0.0
