"""Shared ``as_of`` parameter validation + telemetry (issue #133).

ADR-0008 §1 fixes the bitemporal type as Cypher ``datetime`` in UTC at
microsecond resolution.  The MCP and REST surfaces accept ``as_of`` as an
ISO-8601 timezone-aware datetime string; non-UTC values and naive
datetimes are rejected at the validation boundary so the bitemporal
predicate (ADR-0008 §5) never sees ambiguous time.

This module is the single source of truth for both surfaces:

- :func:`enforce_utc` is the Pydantic validator function plumbed into
  every request body / query model.  Naive datetimes raise ``ValueError``
  with the structured ``INVALID_TIMEZONE`` code; non-UTC datetimes are
  normalised to UTC (callers that send ``+00:00`` and ``Z`` are
  byte-equivalent on the wire).
- :func:`classify_as_of` returns the histogram label
  ``("none", "past", "future", "pre_history")`` consumed by
  :data:`REQUEST_DURATION_SECONDS`.  ``pre_history`` is reserved for a
  caller that supplied ``as_of`` AND received an empty/None response —
  the handler patches the label after the call.
- :data:`REQUEST_DURATION_SECONDS` is the single Prometheus histogram
  used by the MCP and REST surfaces to record per-request wall-clock
  with ``surface``, ``tool`` and ``as_of_kind`` labels.  Ops query it
  to see how often historical reads happen and how they perform.

ACL invariant
-------------

``as_of`` is a query-time predicate, NOT an authorisation surface.  The
token-derived ``workspace_id`` continues to gate every read.  Validation
here only normalises the time value; it never widens or narrows the
workspace clause (issue #133 §H).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Final, Literal

from prometheus_client import Histogram

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

#: Error code surfaced to callers when ``as_of`` is naive or non-UTC.
#: Matches the issue #133 contract; do not change without a doc update.
INVALID_TIMEZONE_CODE: Final[str] = "invalid_timezone"

#: Human-readable error message paired with :data:`INVALID_TIMEZONE_CODE`.
INVALID_TIMEZONE_MESSAGE: Final[str] = (
    "as_of must be a timezone-aware UTC datetime (ISO-8601 with 'Z' suffix or '+00:00')"
)


def enforce_utc(value: datetime | None) -> datetime | None:
    """Reject naive datetimes; normalise non-UTC offsets to UTC.

    Pydantic invokes this as a ``field_validator`` on every ``as_of``
    field.  ``None`` passes through (still-current read).  Aware
    datetimes carrying a non-UTC offset are converted to UTC — they are
    unambiguous and round-tripping is lossless.  Naive datetimes raise
    ``ValueError(INVALID_TIMEZONE_CODE)``; the FastAPI exception
    handler maps that to ``HTTP 400`` with the structured code.

    The MCP layer catches the same ``ValueError`` and re-raises it as
    an MCP error envelope ``code: invalid_timezone``.
    """
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(INVALID_TIMEZONE_CODE)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

AsOfKind = Literal["none", "past", "future", "pre_history"]

#: Histogram bucket boundaries in seconds — same shape as the FastAPI
#: default request-duration histogram so the resulting time series is
#: easy to compare with the existing surface metrics.
_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: Per-request duration histogram.  Labels:
#:   ``surface`` — ``"mcp"`` or ``"rest"`` — distinguishes the transport.
#:   ``tool``    — tool/route name (``"search"``, ``"get_entity"``, ...).
#:   ``as_of_kind`` — bucket of the ``as_of`` argument; see
#:                    :func:`classify_as_of`.
REQUEST_DURATION_SECONDS: Histogram = Histogram(
    name="omniscience_request_duration_seconds",
    documentation=(
        "Wall-clock duration of MCP/REST requests with bitemporal "
        "predicate awareness.  The 'as_of_kind' label distinguishes "
        "current-state reads from historical reads (issue #133)."
    ),
    labelnames=["surface", "tool", "as_of_kind"],
    buckets=_DURATION_BUCKETS,
)


def classify_as_of(as_of: datetime | None, *, now: datetime | None = None) -> AsOfKind:
    """Bucket an ``as_of`` value into the histogram label space.

    ``None`` -> ``"none"`` (caller asked for current state).  An aware
    datetime in the past relative to ``now`` (default ``datetime.now(UTC)``)
    maps to ``"past"``; future values map to ``"future"``.  The
    ``"pre_history"`` bucket is **never** emitted by this helper — it is
    set by the caller after observing an empty result for an ``as_of``
    in the past, and patched into the histogram label via
    :func:`record_request_duration`.
    """
    if as_of is None:
        return "none"
    reference = now if now is not None else datetime.now(UTC)
    return "past" if as_of < reference else "future"


@contextmanager
def record_request_duration(
    *,
    surface: Literal["mcp", "rest"],
    tool: str,
    as_of: datetime | None,
) -> Iterator[DurationLabelPatcher]:
    """Context manager that observes a single request duration.

    Emits one observation on :data:`REQUEST_DURATION_SECONDS` with the
    ``as_of_kind`` derived by :func:`classify_as_of`.  Handlers that
    discover the request was a pre-history read (empty result, ``as_of``
    in the past) call :meth:`DurationLabelPatcher.mark_pre_history`
    inside the ``with`` block; the patcher swaps the label before the
    observation is recorded.
    """
    start = time.monotonic()
    patcher = DurationLabelPatcher(initial=classify_as_of(as_of))
    try:
        yield patcher
    finally:
        duration = time.monotonic() - start
        REQUEST_DURATION_SECONDS.labels(
            surface=surface,
            tool=tool,
            as_of_kind=patcher.kind,
        ).observe(duration)


class DurationLabelPatcher:
    """Mutable wrapper letting handlers refine the ``as_of_kind`` label.

    A handler that discovers it has produced an empty response for an
    ``as_of`` in the past calls :meth:`mark_pre_history`; the new label
    is used when the surrounding :func:`record_request_duration` block
    exits.
    """

    __slots__ = ("kind",)

    def __init__(self, *, initial: AsOfKind) -> None:
        self.kind: AsOfKind = initial

    def mark_pre_history(self) -> None:
        """Promote a ``"past"`` request to ``"pre_history"``.

        No-op when the original label was ``"none"`` or ``"future"``;
        a future ``as_of`` returning empty is not a pre-history event,
        and a current-state read is by definition not historical.
        """
        if self.kind == "past":
            self.kind = "pre_history"


# ---------------------------------------------------------------------------
# Response-envelope helpers
# ---------------------------------------------------------------------------


def resolve_effective_as_of(as_of: datetime | None) -> datetime:
    """Return the timestamp the response reflects.

    Per issue #133: an explicit ``as_of`` is echoed back so callers can
    pin retries; ``None`` is replaced with response generation time
    (``datetime.now(UTC)``).  Always returns a timezone-aware UTC
    datetime so JSON serialisation is deterministic.
    """
    if as_of is None:
        return datetime.now(UTC)
    # ``enforce_utc`` already normalised aware -> UTC; defensive convert.
    return as_of.astimezone(UTC) if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)


#: ``meta.degraded_response`` value emitted when ``as_of`` precedes any
#: recorded history for the workspace.  Surfaces an explicit hint to
#: callers so an empty response is not interpreted as "deleted".
DEGRADED_PRE_HISTORY: Final[str] = "as_of_before_recorded_history"


__all__ = [
    "DEGRADED_PRE_HISTORY",
    "INVALID_TIMEZONE_CODE",
    "INVALID_TIMEZONE_MESSAGE",
    "REQUEST_DURATION_SECONDS",
    "AsOfKind",
    "DurationLabelPatcher",
    "classify_as_of",
    "enforce_utc",
    "record_request_duration",
    "resolve_effective_as_of",
]
