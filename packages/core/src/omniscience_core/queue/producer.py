"""NATS JetStream message producer.

``QueueProducer`` validates a Pydantic model, serialises it to JSON, and
publishes it to NATS JetStream with a synchronous acknowledgement.
"""

from __future__ import annotations

import nats.js
import structlog
from pydantic import BaseModel

from omniscience_core.queue.metrics import QUEUE_PUBLISHED_TOTAL

log = structlog.get_logger(__name__)


class QueueProducer:
    """Publishes Pydantic-validated payloads to NATS JetStream.

    Args:
        js: An active JetStream context obtained from a connected NATS client.
    """

    def __init__(self, js: nats.js.JetStreamContext) -> None:
        self._js = js

    async def publish(self, subject: str, payload: BaseModel) -> None:
        """Validate, serialise, and publish *payload* to *subject*.

        Pydantic validates the model before serialisation so malformed objects
        never reach the wire.  After a successful publish the
        ``omniscience_queue_published_total`` counter is incremented.

        Args:
            subject: NATS subject string, e.g. ``"ingest.changes.git"``.
            payload: A Pydantic model instance to publish.

        Raises:
            pydantic.ValidationError: If *payload* fails Pydantic validation.
            nats.js.errors.PublishAckNotReceived: If JetStream acknowledgement
                is not received within the timeout.
        """
        # model_validate ensures the object satisfies its own schema — fast
        # path when the caller has already constructed a valid model, but
        # catches issues with subclasses that bypass __init__.
        payload.model_validate(payload.model_dump())

        data = payload.model_dump_json().encode()

        await self._js.publish(subject=subject, payload=data)

        QUEUE_PUBLISHED_TOTAL.labels(subject=subject).inc()
        log.debug("queue_published", subject=subject, payload_type=type(payload).__name__)
