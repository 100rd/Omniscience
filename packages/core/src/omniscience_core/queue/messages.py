"""Message wrappers for NATS JetStream messages.

``Message[T]`` is the standard typed wrapper for consumed messages.
``DLQMessage`` carries the context of a message forwarded to the Dead Letter Queue.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Ack callbacks
# ---------------------------------------------------------------------------

AckCallback = Callable[[], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Generic message wrapper
# ---------------------------------------------------------------------------


class Message[T: BaseModel]:
    """Typed wrapper around a raw NATS JetStream message.

    Callers should use :meth:`ack`, :meth:`nak`, or :meth:`term` to signal
    the broker rather than calling the underlying nats-py message directly.
    """

    __slots__ = ("_ack", "_nak", "_term", "metadata", "payload", "subject")

    def __init__(
        self,
        *,
        payload: T,
        subject: str,
        metadata: dict[str, str],
        ack: AckCallback,
        nak: AckCallback,
        term: AckCallback,
    ) -> None:
        self.payload = payload
        self.subject = subject
        self.metadata = metadata
        self._ack = ack
        self._nak = nak
        self._term = term

    async def ack(self) -> None:
        """Acknowledge successful processing."""
        await self._ack()

    async def nak(self) -> None:
        """Negative-acknowledge — triggers redelivery according to consumer config."""
        await self._nak()

    async def term(self) -> None:
        """Terminate — drop the message without redelivery (no DLQ routing from broker side)."""
        await self._term()

    def __repr__(self) -> str:
        return f"Message(subject={self.subject!r}, payload={self.payload!r})"


# ---------------------------------------------------------------------------
# Dead Letter Queue message
# ---------------------------------------------------------------------------


class DLQMessage(BaseModel):
    """Schema for a message published to the Dead Letter Queue stream."""

    original_subject: str = Field(description="Subject the failed message was consumed from.")
    original_payload: str = Field(description="JSON-encoded original message payload.")
    error: str = Field(description="Human-readable description of the terminal failure.")
    attempt_count: int = Field(description="Number of delivery attempts before DLQ routing.")
    failed_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC),
        description="UTC timestamp when the message was forwarded to the DLQ.",
    )
