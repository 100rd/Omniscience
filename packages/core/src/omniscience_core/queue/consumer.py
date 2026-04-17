"""NATS JetStream message consumer.

``QueueConsumer`` is an async-iterator-based consumer that:

- Delivers typed ``Message[T]`` objects to the caller.
- Forwards messages that have exceeded ``max_deliver`` to the DLQ.
- Records Prometheus metrics for consumed, DLQ-routed, and processing duration.
- Drains in-flight processing on graceful shutdown.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import TypeVar

import nats.errors
import nats.js
import nats.js.api
import structlog
from nats.aio.msg import Msg
from pydantic import BaseModel

from omniscience_core.queue.messages import DLQMessage, Message
from omniscience_core.queue.metrics import (
    QUEUE_CONSUMED_TOTAL,
    QUEUE_DLQ_TOTAL,
    QUEUE_PROCESSING_DURATION_SECONDS,
)
from omniscience_core.queue.producer import QueueProducer

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_DEFAULT_ACK_WAIT_SECONDS = 30
_DEFAULT_MAX_DELIVER = 5
_DEFAULT_FETCH_BATCH = 10


class QueueConsumer[T: BaseModel]:
    """Async iterator that yields typed ``Message[T]`` objects from a NATS consumer.

    Callers are responsible for calling ``message.ack()``, ``message.nak()``,
    or ``message.term()`` on each yielded message.

    When a raw NATS message has been delivered more times than *max_deliver*,
    the consumer publishes it to the Dead Letter Queue and terms it at the
    broker rather than yielding it to the caller.

    Args:
        js: Active JetStream context.
        stream: Name of the stream to consume.
        subject: Filter subject, e.g. ``"ingest.changes.git"``.
        durable: Durable consumer name (enables resumable subscriptions).
        payload_type: Pydantic model class used to deserialise message bodies.
        dlq_subject: Subject to publish DLQ messages to.
        max_deliver: Max redelivery attempts before DLQ routing.
        ack_wait: Seconds the broker waits for an ack before redelivering.
        fetch_batch: Number of messages to pull from NATS per request.
    """

    def __init__(
        self,
        *,
        js: nats.js.JetStreamContext,
        stream: str,
        subject: str,
        durable: str,
        payload_type: type[T],
        dlq_subject: str,
        max_deliver: int = _DEFAULT_MAX_DELIVER,
        ack_wait: int = _DEFAULT_ACK_WAIT_SECONDS,
        fetch_batch: int = _DEFAULT_FETCH_BATCH,
    ) -> None:
        self._js = js
        self._stream = stream
        self._subject = subject
        self._durable = durable
        self._payload_type = payload_type
        self._dlq_subject = dlq_subject
        self._max_deliver = max_deliver
        self._ack_wait = ack_wait
        self._fetch_batch = fetch_batch
        self._dlq_producer = QueueProducer(js)
        self._running = True

    # ------------------------------------------------------------------
    # Async iterator protocol
    # ------------------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[Message[T]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Message[T]]:
        """Pull messages in batches and yield typed wrappers.

        The loop runs at least one fetch cycle before checking ``_running``.
        This ensures that calling ``stop()`` from inside the ``async for``
        body causes the iterator to drain the current batch and then exit,
        rather than preventing the first batch from being fetched at all.
        """
        consumer_config = nats.js.api.ConsumerConfig(
            durable_name=self._durable,
            max_deliver=self._max_deliver,
            ack_wait=self._ack_wait,
        )
        psub = await self._js.pull_subscribe(
            subject=self._subject,
            durable=self._durable,
            stream=self._stream,
            config=consumer_config,
        )

        while True:
            try:
                raw_msgs = await psub.fetch(self._fetch_batch, timeout=5)
            except nats.errors.TimeoutError:
                if not self._running:
                    return
                continue
            except Exception as exc:
                log.error("consumer_fetch_error", stream=self._stream, error=str(exc))
                if not self._running:
                    return
                continue

            for raw in raw_msgs:
                msg = await self._process_raw(raw)
                if msg is not None:
                    yield msg

            if not self._running:
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _process_raw(self, raw: Msg) -> Message[T] | None:
        """Decode a raw NATS message, routing to DLQ if max deliveries exceeded."""
        start = time.monotonic()
        attempt = _get_num_delivered(raw)

        if attempt >= self._max_deliver:
            await self._route_to_dlq(raw, attempt)
            QUEUE_CONSUMED_TOTAL.labels(subject=self._subject, status="dlq").inc()
            elapsed = time.monotonic() - start
            QUEUE_PROCESSING_DURATION_SECONDS.labels(subject=self._subject).observe(elapsed)
            return None

        try:
            payload = self._payload_type.model_validate_json(raw.data)
        except Exception as exc:
            log.warning(
                "consumer_decode_error",
                subject=self._subject,
                error=str(exc),
            )
            await raw.nak()
            QUEUE_CONSUMED_TOTAL.labels(subject=self._subject, status="decode_error").inc()
            return None

        msg: Message[T] = Message(
            payload=payload,
            subject=raw.subject,
            metadata=dict(raw.headers or {}),
            ack=raw.ack,
            nak=raw.nak,
            term=raw.term,
        )

        elapsed = time.monotonic() - start
        QUEUE_PROCESSING_DURATION_SECONDS.labels(subject=self._subject).observe(elapsed)
        QUEUE_CONSUMED_TOTAL.labels(subject=self._subject, status="delivered").inc()
        return msg

    async def _route_to_dlq(self, raw: Msg, attempt_count: int) -> None:
        """Publish a DLQ entry and term the original message at the broker."""
        dlq_msg = DLQMessage(
            original_subject=raw.subject,
            original_payload=raw.data.decode(errors="replace"),
            error=f"Exceeded max_deliver={self._max_deliver}",
            attempt_count=attempt_count,
        )
        try:
            await self._dlq_producer.publish(self._dlq_subject, dlq_msg)
            QUEUE_DLQ_TOTAL.labels(subject=self._subject).inc()
            log.warning(
                "consumer_dlq_routed",
                subject=self._subject,
                dlq_subject=self._dlq_subject,
                attempt_count=attempt_count,
            )
        except Exception as exc:
            log.error("consumer_dlq_publish_error", error=str(exc))
        finally:
            await raw.term()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the iterator to stop after the current batch completes.

        The current fetch and processing cycle will finish before the iterator
        exits.  Calling ``stop()`` from inside the ``async for`` body is the
        recommended pattern for test helpers and one-shot consumers.
        """
        self._running = False
        log.info("consumer_stopping", stream=self._stream, subject=self._subject)


def _get_num_delivered(raw: Msg) -> int:
    """Extract delivery count from message metadata; return 1 if unavailable."""
    try:
        meta = raw.metadata  # sync property on nats.aio.msg.Msg
        if meta is not None and hasattr(meta, "num_delivered"):
            return int(meta.num_delivered)
    except Exception as exc:
        log.debug("consumer_metadata_unavailable", error=str(exc))
    return 1
