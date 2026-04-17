"""NATS JetStream queue framework for Omniscience.

Public surface:

    - :class:`NatsConnection` — lifecycle wrapper (connect / disconnect).
    - :func:`ensure_streams` — idempotent JetStream stream setup.
    - :class:`QueueProducer` — publish Pydantic models to a subject.
    - :class:`QueueConsumer` — async-iterator consumer with DLQ support.
    - :class:`Message` — typed wrapper around a consumed message.
    - :class:`DLQMessage` — schema for Dead Letter Queue entries.
"""

from __future__ import annotations

from omniscience_core.queue.connection import NatsConnection
from omniscience_core.queue.consumer import QueueConsumer
from omniscience_core.queue.messages import DLQMessage, Message
from omniscience_core.queue.producer import QueueProducer
from omniscience_core.queue.streams import ensure_streams

__all__ = [
    "DLQMessage",
    "Message",
    "NatsConnection",
    "QueueConsumer",
    "QueueProducer",
    "ensure_streams",
]
