"""Shared admission backend interface (issue #350, AC-SCALE-1).

A narrow `Protocol` the REST admission call site admits requests through
instead of touching a process-local dict directly. Every replica that
constructs an `AdmissionBackend` from the same identity/config is expected
to enforce the same aggregate budget — for `ProcessLocalAdmissionBackend`
that aggregate is only "this process" (the pre-#350 MVP posture,
unchanged); a future shared implementation makes the aggregate span every
replica.

`try_admit` doubles as the atomic authority primitive: a granted decision
*is* the proof of authority to proceed (one atomic check-and-decrement), not
a heuristic layered on top of a separate read. `health` reports whether the
backend can currently make that atomic decision at all — `False` is what
drives the AC-SCALE-4 fail-closed path in `admission.state_machine`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AdmissionDecision:
    """Outcome of one atomic admission attempt."""

    allowed: bool
    retry_after_seconds: float = 0.0


@dataclass(frozen=True)
class BackendHealth:
    """Whether the backend can currently be trusted to make an atomic
    admission decision. `detail` is a short, non-secret diagnostic string —
    never a credential, connection string, or stack trace."""

    healthy: bool
    detail: str | None = None


class AdmissionBackendUnavailableError(RuntimeError):
    """Raised by `AdmissionBackend.try_admit` when the backend cannot make
    an atomic decision right now (unreachable, unimplemented, or otherwise
    untrustworthy). Callers MUST treat this as a fail-closed signal, never
    silently substitute a local guess — see `admission.state_machine`."""


class AdmissionBackend(Protocol):
    """Atomic admission authority. Implementations MUST make `try_admit`
    a single atomic check-and-decrement (or equivalent lease-acquire) —
    never a read followed by a separate, non-atomic write."""

    def try_admit(
        self,
        *,
        lane: str,
        key: str,
        capacity: int,
        refill_per_second: float,
    ) -> AdmissionDecision:
        """Attempt to admit one unit of work for `(lane, key)`.

        `capacity` and `refill_per_second` describe a token-bucket budget;
        implementations that use a different atomic primitive (e.g. a
        fixed-window lease) MUST still honor `capacity` as the hard
        ceiling for the window they use.

        Raises:
            AdmissionBackendUnavailableError: the backend cannot make an atomic
                decision right now (see class docstring).
        """
        ...

    def health(self) -> BackendHealth:
        """Report whether this backend can currently be trusted to make an
        atomic decision. MUST NOT raise — an internal check failure is
        itself reported as `BackendHealth(healthy=False, ...)`."""
        ...


__all__ = [
    "AdmissionBackend",
    "AdmissionBackendUnavailableError",
    "AdmissionDecision",
    "BackendHealth",
]
