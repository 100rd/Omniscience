"""Webhook delivery ID tracker for replay protection.

Maintains an in-memory set of recently seen delivery IDs with TTL-based
expiry so that duplicate webhook deliveries are rejected.

This is a single-process in-memory implementation.  For multi-process
deployments a shared store (Redis, etc.) would be required.
"""

from __future__ import annotations

import asyncio
import time

import structlog

log = structlog.get_logger(__name__)

# Default window: 5 minutes
_DEFAULT_WINDOW_SECONDS: float = 300.0


class DeliveryTracker:
    """Thread-safe (asyncio) in-memory tracker for webhook delivery IDs.

    Each delivery ID is stored alongside its arrival timestamp.  IDs older
    than *window_seconds* are evicted lazily on every call to
    :meth:`is_duplicate` or :meth:`record`.

    Args:
        window_seconds: How long to remember a delivery ID (default 300 s).
    """

    def __init__(self, window_seconds: float = _DEFAULT_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        # delivery_id -> arrival timestamp (monotonic)
        self._seen: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_duplicate(self, delivery_id: str) -> bool:
        """Return ``True`` if *delivery_id* was already recorded within the window.

        Performs a lazy TTL purge before checking.

        Args:
            delivery_id: Opaque unique identifier for the delivery (e.g. GitHub
                ``X-GitHub-Delivery`` UUID, GitLab ``X-Gitlab-Event-UUID``).

        Returns:
            ``True`` if the ID is a duplicate; ``False`` otherwise.
        """
        async with self._lock:
            self._purge_expired()
            return delivery_id in self._seen

    async def record(self, delivery_id: str) -> None:
        """Record *delivery_id* as seen now.

        If the ID was already recorded the timestamp is refreshed.

        Args:
            delivery_id: Opaque delivery identifier to remember.
        """
        async with self._lock:
            self._purge_expired()
            self._seen[delivery_id] = time.monotonic()
            log.debug("delivery_tracked", delivery_id=delivery_id, tracked_count=len(self._seen))

    async def size(self) -> int:
        """Return the number of currently tracked (non-expired) IDs."""
        async with self._lock:
            self._purge_expired()
            return len(self._seen)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _purge_expired(self) -> None:
        """Remove IDs older than *window_seconds*.

        Must be called with ``self._lock`` held.
        """
        cutoff = time.monotonic() - self._window
        expired = [k for k, ts in self._seen.items() if ts < cutoff]
        for key in expired:
            del self._seen[key]
        if expired:
            log.debug("delivery_tracker_purged", count=len(expired))


__all__ = ["DeliveryTracker"]
