"""NATS JetStream connection management.

``NatsConnection`` wraps the nats-py client and provides a clean lifecycle
(connect / disconnect) with structured logging and auto-reconnect callbacks.
"""

from __future__ import annotations

from typing import Any

import nats
import nats.js
import structlog
from nats.aio.client import Client as NatsClient

from omniscience_core.config import Settings

log = structlog.get_logger(__name__)


def _make_error_cb(url: str) -> Any:
    """Return an async error callback that logs unexpected NATS errors."""

    async def _error_cb(exc: Exception) -> None:
        log.error("nats_error", url=url, error=str(exc))

    return _error_cb


def _make_reconnected_cb(url: str) -> Any:
    """Return an async callback that logs successful reconnection."""

    async def _reconnected_cb() -> None:
        log.info("nats_reconnected", url=url)

    return _reconnected_cb


def _make_disconnected_cb(url: str) -> Any:
    """Return an async callback that logs disconnection events."""

    async def _disconnected_cb() -> None:
        log.warning("nats_disconnected", url=url)

    return _disconnected_cb


def _make_closed_cb(url: str) -> Any:
    """Return an async callback that logs when the client connection closes."""

    async def _closed_cb() -> None:
        log.info("nats_closed", url=url)

    return _closed_cb


class NatsConnection:
    """Lifecycle wrapper for a nats-py client with JetStream enabled.

    Usage::

        conn = NatsConnection()
        await conn.connect(settings)
        js = conn.jetstream
        # ... use js ...
        await conn.disconnect()
    """

    def __init__(self) -> None:
        self._client: NatsClient | None = None
        self._js: nats.js.JetStreamContext | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client(self) -> NatsClient:
        """The underlying nats-py client.  Raises if not connected."""
        if self._client is None:
            raise RuntimeError("NatsConnection is not connected. Call connect() first.")
        return self._client

    @property
    def jetstream(self) -> nats.js.JetStreamContext:
        """The JetStream context.  Raises if not connected."""
        if self._js is None:
            raise RuntimeError("NatsConnection is not connected. Call connect() first.")
        return self._js

    @property
    def is_connected(self) -> bool:
        """Return True when the underlying client is currently connected."""
        return self._client is not None and self._client.is_connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, settings: Settings) -> None:
        """Open the NATS connection and enable JetStream.

        Idempotent — calling connect() on an already-connected instance is a no-op.
        """
        if self._client is not None and self._client.is_connected:
            log.debug("nats_already_connected", url=settings.nats_url)
            return

        url = settings.nats_url
        log.info("nats_connecting", url=url)

        self._client = await nats.connect(
            servers=url,
            error_cb=_make_error_cb(url),
            reconnected_cb=_make_reconnected_cb(url),
            disconnected_cb=_make_disconnected_cb(url),
            closed_cb=_make_closed_cb(url),
            # Retry indefinitely with back-off — the server may be starting up.
            max_reconnect_attempts=-1,
        )
        self._js = self._client.jetstream()
        log.info("nats_connected", url=url)

    async def disconnect(self) -> None:
        """Drain in-flight messages and close the NATS connection gracefully."""
        if self._client is None:
            return

        url = self._client.connected_url.netloc if self._client.connected_url else "unknown"
        log.info("nats_disconnecting", url=url)
        try:
            await self._client.drain()
        except Exception as exc:
            log.warning("nats_drain_error", url=url, error=str(exc))
        finally:
            self._client = None
            self._js = None
            log.info("nats_disconnected_clean", url=url)
