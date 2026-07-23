"""Sealed QuarantineStore (ADR-0020) -- disposable fixtures only, isolated from
ordinary knowledge stores. No live backend, access path, or key lives here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from omniscience_core.privacy.envelope import DataEnvelope


class QuarantineError(Exception):
    """Base error for the sealed quarantine boundary."""


class QuarantineUnavailableError(QuarantineError):
    """Raised when the quarantine backend itself cannot accept a hold.

    The gate must treat this as fail-closed (park), never as license to admit the
    envelope into an ordinary sink instead (ADR-0020, SPEC-PII REQ-PII-3 fallback).
    """


@runtime_checkable
class QuarantineStore(Protocol):
    def put(self, envelope: DataEnvelope) -> str: ...
    def health(self) -> bool: ...


@dataclass
class InMemoryQuarantineStore:
    """Disposable, process-local, sealed quarantine fixture for development and
    tests. Not a production quarantine backend -- ADR-0020 requires a live backend,
    encryption, and retention profile as a separate, human-approved activation."""

    _items: dict[str, DataEnvelope] = field(default_factory=dict)

    def put(self, envelope: DataEnvelope) -> str:
        quarantine_id = f"quarantine-{envelope.envelope_id}"
        self._items[quarantine_id] = envelope
        return quarantine_id

    def health(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(self._items)


class FailingQuarantineStore:
    """Test double that always reports the quarantine backend unavailable -- used to
    prove the PW0 gate parks instead of falling back to an unsafe admit when
    quarantine itself cannot be reached (AC-SP61-2)."""

    def put(self, envelope: DataEnvelope) -> str:
        raise QuarantineUnavailableError("fixture-injected quarantine outage")

    def health(self) -> bool:
        return False
