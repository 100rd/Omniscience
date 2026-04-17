"""JetStream stream definitions and idempotent setup.

Call ``ensure_streams(js)`` once at application startup.  The function
creates each stream if it does not already exist; if it does exist the call
is a no-op (idempotent via ``find_or_create`` semantics from nats-py).
"""

from __future__ import annotations

import nats.js
import nats.js.api
import structlog
from nats.js.errors import BadRequestError

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEVEN_DAYS_NS: int = 7 * 24 * 60 * 60 * 10**9  # nanoseconds

INGEST_CHANGES_STREAM = "INGEST_CHANGES"
INGEST_DLQ_STREAM = "INGEST_DLQ"

_STREAM_CONFIGS: list[nats.js.api.StreamConfig] = [
    nats.js.api.StreamConfig(
        name=INGEST_CHANGES_STREAM,
        subjects=["ingest.changes.*"],
        retention=nats.js.api.RetentionPolicy.LIMITS,
        max_age=_SEVEN_DAYS_NS,
        storage=nats.js.api.StorageType.FILE,
        num_replicas=1,
    ),
    nats.js.api.StreamConfig(
        name=INGEST_DLQ_STREAM,
        subjects=["ingest.dlq.*"],
        retention=nats.js.api.RetentionPolicy.LIMITS,
        max_age=_SEVEN_DAYS_NS,
        storage=nats.js.api.StorageType.FILE,
        num_replicas=1,
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ensure_streams(js: nats.js.JetStreamContext) -> None:
    """Idempotently create all required JetStream streams.

    If a stream already exists this function leaves it unchanged and logs a
    debug message.  This makes the function safe to call on every startup.

    Args:
        js: An active JetStream context obtained from a connected NATS client.
    """
    for cfg in _STREAM_CONFIGS:
        await _ensure_stream(js, cfg)


async def _ensure_stream(
    js: nats.js.JetStreamContext,
    cfg: nats.js.api.StreamConfig,
) -> None:
    """Create a single stream, or skip if it already exists."""
    try:
        await js.add_stream(config=cfg)
        log.info("nats_stream_created", stream=cfg.name, subjects=cfg.subjects)
    except BadRequestError:
        # Stream already exists — this is expected on restart.
        log.debug("nats_stream_exists", stream=cfg.name)
