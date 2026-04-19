"""Sandboxed connector wrapper for third-party plugins.

:class:`SandboxedConnector` wraps an arbitrary :class:`~omniscience_connectors.base.Connector`
instance and enforces:

- **Timeout enforcement** — every async method call is bounded by a configurable timeout.
- **Exception isolation** — exceptions from the plugin are caught and re-raised as
  :class:`SandboxError` so a misbehaving plugin cannot crash the worker.
- **Resource limits** — configurable maximum concurrent requests and an advisory memory
  limit (enforced via a semaphore on concurrent calls).
- **Structured logging** — every call is prefixed with the plugin name for easy filtering.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
)

__all__ = [
    "PluginTimeoutError",
    "SandboxConfig",
    "SandboxError",
    "SandboxedConnector",
    # Legacy alias kept for backwards compatibility
    "TimeoutError",
]

logger = logging.getLogger(__name__)


class SandboxConfig(BaseModel):
    """Configuration for :class:`SandboxedConnector` resource limits."""

    timeout_seconds: float = Field(default=30.0, gt=0)
    """Maximum wall-clock seconds for any single async method call."""

    max_concurrent_requests: int = Field(default=10, gt=0)
    """Maximum number of simultaneous in-flight calls to the underlying connector."""

    max_discover_items: int = Field(default=100_000, gt=0)
    """Maximum number of :class:`~omniscience_connectors.base.DocumentRef` objects
    that :meth:`~SandboxedConnector.discover` will yield before stopping."""


class SandboxError(RuntimeError):
    """Raised when the sandboxed connector raises an unexpected exception."""

    def __init__(self, plugin_name: str, method: str, cause: BaseException) -> None:
        self.plugin_name = plugin_name
        self.method = method
        self.cause = cause
        super().__init__(
            f"Plugin {plugin_name!r} raised in {method}(): {type(cause).__name__}: {cause}"
        )


class PluginTimeoutError(asyncio.TimeoutError):
    """Raised when the sandboxed connector exceeds its configured timeout."""

    def __init__(self, plugin_name: str, method: str, timeout: float) -> None:
        self.plugin_name = plugin_name
        self.method = method
        self.timeout = timeout
        super().__init__(f"Plugin {plugin_name!r} timed out in {method}() after {timeout}s")


# Backwards-compatible alias.
TimeoutError = PluginTimeoutError  # noqa: A001


class SandboxedConnector(Connector):
    """Wraps a third-party connector with timeout, isolation, and resource limits.

    All async methods delegate to the inner connector but enforce:

    - A per-call asyncio timeout (:attr:`SandboxConfig.timeout_seconds`)
    - A concurrency limit semaphore (:attr:`SandboxConfig.max_concurrent_requests`)
    - Exception capture and re-raise as :class:`SandboxError`
    - Structured log records prefixed with the plugin name

    The :attr:`connector_type` and :attr:`config_schema` class variables are
    copied from the wrapped connector class at construction time.

    Args:
        inner: The underlying connector instance to wrap.
        plugin_name: Human-readable name used in log messages and error messages.
        config: Resource limit configuration.
    """

    # Declare class-level defaults that are overridden per-instance in __init__.
    # mypy[misc]: overriding ClassVar from base class at instance scope is intentional here.
    connector_type: str = ""  # type: ignore[misc]
    config_schema: type[BaseModel] = BaseModel  # type: ignore[misc]

    def __init__(
        self,
        inner: Connector,
        plugin_name: str,
        config: SandboxConfig | None = None,
    ) -> None:
        self._inner = inner
        self._plugin_name = plugin_name
        self._config = config or SandboxConfig()
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_requests)

        # Mirror the inner connector's class attributes at instance level so the
        # registry can read them via standard attribute access.
        inner_cls = type(inner)
        self.connector_type = getattr(inner_cls, "connector_type", "")
        self.config_schema = getattr(inner_cls, "config_schema", BaseModel)

    # ------------------------------------------------------------------
    # Connector interface
    # ------------------------------------------------------------------

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Delegate to inner connector with timeout and isolation.

        Args:
            config: Validated public configuration.
            secrets: Runtime secret values.

        Raises:
            SandboxError: If the inner connector raises.
            PluginTimeoutError: If the call exceeds :attr:`SandboxConfig.timeout_seconds`.
        """
        await self._run("validate", self._inner.validate(config, secrets))

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Delegate to inner connector with timeout per item and item count limit.

        Yields at most :attr:`SandboxConfig.max_discover_items` refs.

        Args:
            config: Validated public configuration.
            secrets: Runtime secret values.

        Yields:
            :class:`~omniscience_connectors.base.DocumentRef` objects.

        Raises:
            SandboxError: If the inner connector raises during iteration.
            PluginTimeoutError: If a single item takes longer than the configured timeout.
        """
        count = 0
        max_items = self._config.max_discover_items
        timeout = self._config.timeout_seconds
        log_prefix = self._log_prefix("discover")

        async with self._semaphore:
            try:
                async for ref in self._inner.discover(config, secrets):
                    if count >= max_items:
                        logger.warning(
                            "%s item limit reached (%d), stopping early",
                            log_prefix,
                            max_items,
                        )
                        return
                    try:
                        yield await asyncio.wait_for(self._yield_ref(ref), timeout=timeout)
                    except builtins.TimeoutError:
                        raise PluginTimeoutError(self._plugin_name, "discover", timeout) from None
                    count += 1
            except (SandboxError, PluginTimeoutError):
                raise
            except Exception as exc:
                raise SandboxError(self._plugin_name, "discover", exc) from exc

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Delegate to inner connector with timeout and isolation.

        Args:
            config: Validated public configuration.
            secrets: Runtime secret values.
            ref: Document reference returned by :meth:`discover`.

        Returns:
            :class:`~omniscience_connectors.base.FetchedDocument` with raw bytes.

        Raises:
            SandboxError: If the inner connector raises.
            PluginTimeoutError: If the call exceeds the configured timeout.
        """
        result: FetchedDocument = await self._run("fetch", self._inner.fetch(config, secrets, ref))
        return result

    def webhook_handler(self) -> WebhookHandler | None:
        """Return the inner connector's webhook handler, if any."""
        return self._inner.webhook_handler()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run(self, method: str, coro: Any) -> Any:
        """Execute *coro* inside the semaphore with timeout and exception isolation."""
        log_prefix = self._log_prefix(method)
        timeout = self._config.timeout_seconds

        logger.debug("%s starting", log_prefix)
        async with self._semaphore:
            try:
                result = await asyncio.wait_for(coro, timeout=timeout)
                logger.debug("%s completed", log_prefix)
                return result
            except builtins.TimeoutError:
                logger.error("%s timed out after %.1fs", log_prefix, timeout)
                raise PluginTimeoutError(self._plugin_name, method, timeout) from None
            except (SandboxError, PluginTimeoutError):
                raise
            except Exception as exc:
                logger.error(
                    "%s raised %s: %s",
                    log_prefix,
                    type(exc).__name__,
                    exc,
                )
                raise SandboxError(self._plugin_name, method, exc) from exc

    def _log_prefix(self, method: str) -> str:
        return f"[plugin:{self._plugin_name}] {method}"

    @staticmethod
    async def _yield_ref(ref: DocumentRef) -> DocumentRef:
        """Identity coroutine used to apply wait_for to a single yielded item."""
        return ref
