"""Connected-clients telemetry registry (issue #113, epic #109).

Why a dedicated module?
-----------------------
Issue #113 needs *two* read shapes for the admin dashboard's "connected
clients" view:

1. **Fresh, low-latency rollups** — "requests in the last 15 minutes"
   per token, "active MCP sessions right now", "top tools in the last
   hour".  These are not natural Prometheus queries because we have no
   external Prometheus / TSDB; they are read off the live process by an
   operator hitting ``/api/v1/stats/clients`` directly.
2. **Long-tail counters** — Prometheus families (``Counter``, ``Gauge``)
   that downstream scrapers can aggregate however they wish.

The dashboard can't query Prometheus (we don't run one).  So this
module keeps a small, bounded, in-memory rolling-bucket structure for
the dashboard reads, *and* updates the Prometheus families so external
scrapers stay first-class.

Storage shape
-------------
* Per token / per session, we keep a rolling bucket array.  Each bucket
  covers ``_BUCKET_SECONDS`` (60s) of wall-clock; we keep enough buckets
  to cover 24h.  Reads pick the suffix that lies inside the desired
  window.  Writes touch the current bucket only — O(1).
* Eviction is **strict LRU** on the per-token map at
  :data:`_MAX_TRACKED_TOKENS`.  Past that bound we drop the oldest
  token's history; the Prometheus counters still keep their cumulative
  values because they live on the registry, not in this map.

ACL invariant
-------------
This module does **not** know about workspaces.  The
:class:`ClientsStatsService` (see ``omniscience_core.stats.clients``)
is the boundary that enforces the per-request workspace filter: it
calls :meth:`ClientTelemetryRegistry.snapshot` for the *list of token
ids* the caller is allowed to see, then assembles the response.  Token
ids never leak outside their workspace because the *list* never
includes another workspace's tokens.

No databases
------------
No new SQL tables — issue #113 is explicit on this point.  Memory is
bounded by ``_MAX_TRACKED_TOKENS`` x bucket array size; under sustained
traffic the eviction policy keeps total bytes << 10 MiB at the default
bound.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from prometheus_client import Counter, Gauge

# ---------------------------------------------------------------------------
# Bounds (named constants — no magic numbers per the project standards)
# ---------------------------------------------------------------------------

#: Bucket width in seconds.  60s gives us minute-granularity over a
#: 24-hour window with 1440 buckets per token; cheap memory and good
#: enough resolution for an operator dashboard.
_BUCKET_SECONDS: Final[int] = 60

#: How many buckets to keep per token / session.  1440 = 24h x 60 min.
#: Exposed so tests can shrink the array without touching code.
_BUCKET_COUNT: Final[int] = 24 * 60

#: 15-minute sliding window in seconds — the "requests_last_15m" figure.
_WINDOW_15M_SECONDS: Final[int] = 15 * 60

#: 1-hour sliding window in seconds — the "tools last hour" figure.
_WINDOW_1H_SECONDS: Final[int] = 60 * 60

#: 24-hour window in seconds — the "requests_last_24h" approximation.
_WINDOW_24H_SECONDS: Final[int] = 24 * 60 * 60

#: Hard cap on tracked tokens.  At 1440 buckets x 16 bytes = 23 KiB per
#: token, 10_000 tokens = ~225 MiB worst case.  In practice a deployment
#: has tens of tokens; this bound is for a runaway / abusive caller
#: scenario where many short-lived tokens churn through.
_MAX_TRACKED_TOKENS: Final[int] = 10_000

#: Hard cap on tracked MCP sessions in the in-memory rolling state.
_MAX_TRACKED_SESSIONS: Final[int] = 10_000

#: Sentinel used as token-id label when the request was unauthenticated.
ANONYMOUS_TOKEN_ID: Final[str] = "anonymous"  # noqa: S105 — label sentinel, not a credential


# ---------------------------------------------------------------------------
# Prometheus families (registered on the default REGISTRY)
# ---------------------------------------------------------------------------

#: Total REST requests, labelled by token UUID and a comma-joined scope set.
#: ``scope_set`` is the token's *granted* scope set, sorted+joined; for
#: anonymous callers it is the literal string ``""``.
REQUESTS_TOTAL: Counter = Counter(
    name="omniscience_requests_total",
    documentation="Total REST requests served, labelled by token id + scope set.",
    labelnames=["token_id", "scope_set"],
)

#: Total response bytes (Content-Length) sent, labelled by token id.
RESPONSE_BYTES_TOTAL: Counter = Counter(
    name="omniscience_response_bytes_total",
    documentation="Total response bytes shipped to clients, labelled by token id.",
    labelnames=["token_id"],
)

#: Total MCP sessions opened / closed, with streaming flag and outcome.
MCP_SESSIONS_TOTAL: Counter = Counter(
    name="omniscience_mcp_sessions_total",
    documentation="MCP session lifecycle events: connected / disconnected.",
    labelnames=["outcome", "streaming"],
)

#: Total MCP tool invocations, by tool name + token id.
MCP_TOOL_INVOCATIONS_TOTAL: Counter = Counter(
    name="omniscience_mcp_tool_invocations_total",
    documentation="MCP tools/call invocation counter.",
    labelnames=["tool_name", "token_id"],
)

#: Currently active MCP sessions.
MCP_SESSIONS_ACTIVE: Gauge = Gauge(
    name="omniscience_mcp_sessions_active",
    documentation="Number of MCP sessions currently connected.",
)


# ---------------------------------------------------------------------------
# Rolling-bucket counter
# ---------------------------------------------------------------------------


@dataclass
class _RollingCounter:
    """Wall-clock bucketed counter.

    Each bucket holds ``count`` and ``bytes_out`` for one ``_BUCKET_SECONDS``
    slice.  The buckets are stored as a plain list indexed by
    ``bucket_index = floor(monotonic_time / _BUCKET_SECONDS) % _BUCKET_COUNT``.
    To distinguish a fresh bucket from a stale one we stamp every slot with
    its absolute ``epoch`` (the bucket index unmodulated); on read we discard
    slots whose epoch is older than the window cutoff.
    """

    counts: list[int] = field(default_factory=lambda: [0] * _BUCKET_COUNT)
    bytes_out: list[int] = field(default_factory=lambda: [0] * _BUCKET_COUNT)
    epochs: list[int] = field(default_factory=lambda: [-1] * _BUCKET_COUNT)
    last_seen_monotonic: float = 0.0
    last_seen_wall: float = 0.0

    def record(self, *, now_monotonic: float, now_wall: float, bytes_out: int) -> None:
        epoch = int(now_monotonic // _BUCKET_SECONDS)
        slot = epoch % _BUCKET_COUNT
        if self.epochs[slot] != epoch:
            self.counts[slot] = 0
            self.bytes_out[slot] = 0
            self.epochs[slot] = epoch
        self.counts[slot] += 1
        self.bytes_out[slot] += max(0, bytes_out)
        self.last_seen_monotonic = now_monotonic
        self.last_seen_wall = now_wall

    def sum_window(self, *, now_monotonic: float, window_seconds: int) -> int:
        """Sum counts in the last ``window_seconds``."""
        epoch = int(now_monotonic // _BUCKET_SECONDS)
        cutoff_epoch = epoch - (window_seconds // _BUCKET_SECONDS)
        total = 0
        for stored_epoch, count in zip(self.epochs, self.counts, strict=False):
            if stored_epoch >= cutoff_epoch:
                total += count
        return total


# ---------------------------------------------------------------------------
# Public snapshot dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenSnapshot:
    """Per-token counters for the dashboard."""

    token_id: uuid.UUID
    last_seen_at_unix: float | None
    requests_last_15m: int
    requests_last_24h: int


@dataclass(frozen=True)
class ToolSnapshot:
    """Per-tool invocation count over the last hour."""

    tool_name: str
    invocations_last_hour: int


@dataclass(frozen=True)
class McpSessionStats:
    """Aggregated MCP session counts."""

    active: int
    last_hour: int


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ClientTelemetryRegistry:
    """In-memory rolling state for connected-clients telemetry.

    Thread-safe (a plain :class:`threading.Lock` protects the hot path —
    we do not need ``asyncio.Lock`` because all updates are O(1) and
    happen synchronously inside ASGI middleware / MCP hooks).
    """

    def __init__(
        self,
        *,
        max_tracked_tokens: int = _MAX_TRACKED_TOKENS,
        max_tracked_sessions: int = _MAX_TRACKED_SESSIONS,
    ) -> None:
        self._max_tracked_tokens = max_tracked_tokens
        self._max_tracked_sessions = max_tracked_sessions
        self._tokens: OrderedDict[str, _RollingCounter] = OrderedDict()
        self._tools: OrderedDict[str, _RollingCounter] = OrderedDict()
        self._sessions: OrderedDict[str, _RollingCounter] = OrderedDict()
        self._active_sessions: int = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_request(
        self,
        *,
        token_id: str,
        bytes_out: int,
        scope_set: str,
    ) -> None:
        """Record a single REST request for ``token_id``.

        ``token_id`` is the canonical UUID stringified, or
        :data:`ANONYMOUS_TOKEN_ID`.
        """
        now_m = time.monotonic()
        now_w = time.time()
        with self._lock:
            counter = self._touch_token(token_id)
            counter.record(now_monotonic=now_m, now_wall=now_w, bytes_out=bytes_out)

        # Prometheus families live on the global registry; bypass the
        # per-token bound (counters are cumulative + cardinality is
        # bounded by deployed tokens, not by churn through the LRU).
        REQUESTS_TOTAL.labels(token_id=token_id, scope_set=scope_set).inc()
        if bytes_out > 0:
            RESPONSE_BYTES_TOTAL.labels(token_id=token_id).inc(bytes_out)

    def record_session_connect(self, *, session_id: str, streaming: bool) -> None:
        now_m = time.monotonic()
        now_w = time.time()
        with self._lock:
            self._active_sessions += 1
            counter = self._touch_session(session_id)
            counter.record(now_monotonic=now_m, now_wall=now_w, bytes_out=0)
        MCP_SESSIONS_TOTAL.labels(outcome="connected", streaming=str(streaming).lower()).inc()
        MCP_SESSIONS_ACTIVE.inc()

    def record_session_disconnect(self, *, session_id: str, streaming: bool) -> None:
        with self._lock:
            self._active_sessions = max(0, self._active_sessions - 1)
        MCP_SESSIONS_TOTAL.labels(outcome="disconnected", streaming=str(streaming).lower()).inc()
        MCP_SESSIONS_ACTIVE.dec()

    def record_tool_invocation(self, *, tool_name: str, token_id: str) -> None:
        now_m = time.monotonic()
        now_w = time.time()
        with self._lock:
            counter = self._touch_tool(tool_name)
            counter.record(now_monotonic=now_m, now_wall=now_w, bytes_out=0)
        MCP_TOOL_INVOCATIONS_TOTAL.labels(tool_name=tool_name, token_id=token_id).inc()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def snapshot_tokens(self, *, token_ids: Iterable[str]) -> list[TokenSnapshot]:
        """Return per-token snapshots for ``token_ids`` (workspace-filtered)."""
        now_m = time.monotonic()
        results: list[TokenSnapshot] = []
        with self._lock:
            for raw_id in token_ids:
                counter = self._tokens.get(raw_id)
                if counter is None:
                    results.append(
                        TokenSnapshot(
                            token_id=uuid.UUID(raw_id),
                            last_seen_at_unix=None,
                            requests_last_15m=0,
                            requests_last_24h=0,
                        )
                    )
                    continue
                results.append(
                    TokenSnapshot(
                        token_id=uuid.UUID(raw_id),
                        last_seen_at_unix=counter.last_seen_wall or None,
                        requests_last_15m=counter.sum_window(
                            now_monotonic=now_m, window_seconds=_WINDOW_15M_SECONDS
                        ),
                        requests_last_24h=counter.sum_window(
                            now_monotonic=now_m, window_seconds=_WINDOW_24H_SECONDS
                        ),
                    )
                )
        return results

    def snapshot_top_tools(self, *, limit: int) -> list[ToolSnapshot]:
        """Return top tool invocation counts in the last hour."""
        now_m = time.monotonic()
        with self._lock:
            entries = [
                ToolSnapshot(
                    tool_name=name,
                    invocations_last_hour=counter.sum_window(
                        now_monotonic=now_m, window_seconds=_WINDOW_1H_SECONDS
                    ),
                )
                for name, counter in self._tools.items()
            ]
        entries = [e for e in entries if e.invocations_last_hour > 0]
        entries.sort(key=lambda e: e.invocations_last_hour, reverse=True)
        return entries[:limit]

    def snapshot_sessions(self) -> McpSessionStats:
        """Return the active session count and a 1-hour cumulative count."""
        now_m = time.monotonic()
        with self._lock:
            last_hour = sum(
                counter.sum_window(now_monotonic=now_m, window_seconds=_WINDOW_1H_SECONDS)
                for counter in self._sessions.values()
            )
            return McpSessionStats(active=self._active_sessions, last_hour=last_hour)

    def tracked_token_count(self) -> int:
        """Test/observability helper: how many tokens are in the LRU map."""
        with self._lock:
            return len(self._tokens)

    def reset(self) -> None:
        """Test-only: drop all in-memory state.  Does not touch Prometheus."""
        with self._lock:
            self._tokens.clear()
            self._tools.clear()
            self._sessions.clear()
            self._active_sessions = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _touch_token(self, token_id: str) -> _RollingCounter:
        """Get-or-create the token counter, evicting LRU on overflow."""
        existing = self._tokens.get(token_id)
        if existing is not None:
            self._tokens.move_to_end(token_id)
            return existing
        counter = _RollingCounter()
        self._tokens[token_id] = counter
        while len(self._tokens) > self._max_tracked_tokens:
            self._tokens.popitem(last=False)
        return counter

    def _touch_session(self, session_id: str) -> _RollingCounter:
        existing = self._sessions.get(session_id)
        if existing is not None:
            self._sessions.move_to_end(session_id)
            return existing
        counter = _RollingCounter()
        self._sessions[session_id] = counter
        while len(self._sessions) > self._max_tracked_sessions:
            self._sessions.popitem(last=False)
        return counter

    def _touch_tool(self, tool_name: str) -> _RollingCounter:
        existing = self._tools.get(tool_name)
        if existing is not None:
            self._tools.move_to_end(tool_name)
            return existing
        counter = _RollingCounter()
        self._tools[tool_name] = counter
        # Tool names are bounded by the registered MCP tool set — we do
        # not enforce a separate cap here; the OrderedDict keeps the
        # most-recent at the tail for any future LRU policy.
        return counter


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_REGISTRY: ClientTelemetryRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_client_registry() -> ClientTelemetryRegistry:
    """Return the process-wide :class:`ClientTelemetryRegistry`.

    Lazily constructed.  All ASGI middleware and MCP hooks read the same
    instance via this accessor; tests reset state via
    :meth:`ClientTelemetryRegistry.reset`.
    """
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = ClientTelemetryRegistry()
    return _REGISTRY


__all__ = [
    "ANONYMOUS_TOKEN_ID",
    "MCP_SESSIONS_ACTIVE",
    "MCP_SESSIONS_TOTAL",
    "MCP_TOOL_INVOCATIONS_TOTAL",
    "REQUESTS_TOTAL",
    "RESPONSE_BYTES_TOTAL",
    "ClientTelemetryRegistry",
    "McpSessionStats",
    "TokenSnapshot",
    "ToolSnapshot",
    "get_client_registry",
]
