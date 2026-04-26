"""In-process TTL cache for stats rollups (issue #111).

Why not :func:`functools.lru_cache`?
------------------------------------
``lru_cache`` does not support per-entry TTL, does not work with async
callables (it would cache the coroutine, not its result), and has no
clean invalidation hook.  This module provides a small async-friendly
TTL cache with a hard size cap so memory stays bounded under workspace
churn.

Why not Redis?
--------------
The stats endpoints serve the admin dashboard, which is low-traffic
(operator-only) and tolerates a few seconds of staleness.  Adding a
Redis dependency for ~10s caching would dwarf the benefit.  An
in-process cache also keeps every response below the < 100ms latency
target on the 100k-chunk fixture without an extra network hop.

The cache is keyed by ``(workspace_id, method_name)`` — workspaces are
isolated, and a write to source X in workspace A must not invalidate
workspace B's cached overview.  TTL invalidation is sufficient for the
dashboard refresh model; explicit invalidation (e.g. on source create)
is intentionally out of scope.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Final

# Default cache TTL — short enough that the dashboard feels live, long
# enough to absorb a typical operator burst (multiple panels reload on
# the same dashboard tick).  Values <5s defeat the cache; values >30s
# make the dashboard look stale.
STATS_CACHE_TTL_SECONDS: Final[float] = 10.0

# Hard cap on simultaneously cached ``(workspace_id, method)`` keys.
# 256 supports a deployment with hundreds of workspaces and a handful
# of cached methods per workspace; eviction is LRU.
_CACHE_MAX_ENTRIES: Final[int] = 256


class TTLCache[T]:
    """Async-safe LRU+TTL cache for stats rollups.

    Each :meth:`get_or_compute` call serialises through an asyncio lock
    so concurrent dashboard refreshes do not stampede the underlying
    stores.  The factory is a coroutine so it can run database queries
    without blocking.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = STATS_CACHE_TTL_SECONDS,
        max_entries: int = _CACHE_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[uuid.UUID, str], tuple[float, T]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get_or_compute(
        self,
        *,
        workspace_id: uuid.UUID,
        method: str,
        factory: Callable[[], Awaitable[T]],
    ) -> T:
        """Return the cached value or run ``factory`` and cache its result."""
        key = (workspace_id, method)
        now = time.monotonic()
        async with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                expires_at, value = existing
                if expires_at > now:
                    self._entries.move_to_end(key)
                    return value
                # Stale — drop and recompute below.
                self._entries.pop(key, None)

        # Compute outside the lock so independent workspaces don't serialise
        # on each other.  A second concurrent caller for the same key will
        # recompute once; we accept that double work for simplicity.
        value = await factory()
        async with self._lock:
            self._entries[key] = (now + self._ttl_seconds, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return value

    def invalidate(self, *, workspace_id: uuid.UUID) -> None:
        """Drop all cached entries for ``workspace_id``.

        Synchronous because it never awaits — the cache state is plain
        Python dicts.  Useful for tests and explicit invalidation by
        write paths that have side effects on stats counts.
        """
        keys = [k for k in self._entries if k[0] == workspace_id]
        for key in keys:
            self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop the entire cache.  Test-only helper."""
        self._entries.clear()


__all__ = ["STATS_CACHE_TTL_SECONDS", "TTLCache"]
