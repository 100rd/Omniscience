"""Configuration for the Qdrant vector-store adapter.

Kept in its own module so the settings layer, tests, and the adapter
all depend on a single source of truth for config shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from omniscience_index.stores.qdrant_constants import (
    DEFAULT_QDRANT_GRPC_PORT,
    DEFAULT_QDRANT_HOST,
    DEFAULT_QDRANT_HTTP_PORT,
)


@dataclass(frozen=True, slots=True)
class QdrantConfig:
    """Connection and TLS settings for the Qdrant adapter.

    ``api_key`` is read from the environment at the application edge
    (``omniscience_core.config.Settings``) and passed in.  It is
    **never** read directly by the adapter — the adapter consumes
    already-resolved config so secrets stay centralised.

    ADR-0006 §Deployment posture mandates an API key in every
    non-dev environment.  The dev profile passes ``api_key=None``.

    ADR-0006 §Transport: gRPC on ``grpc_port`` (default 6334) is
    tried first; HTTP on ``http_port`` (default 6333) is the fallback
    transport used when gRPC is unreachable.
    """

    host: str = DEFAULT_QDRANT_HOST
    grpc_port: int = DEFAULT_QDRANT_GRPC_PORT
    http_port: int = DEFAULT_QDRANT_HTTP_PORT
    api_key: str | None = None
    https: bool = False
    prefer_grpc: bool = True
    timeout_seconds: float = 10.0


__all__ = ["QdrantConfig"]
