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

# nats-py's StreamConfig.max_age is expressed in SECONDS — as_dict() converts it
# to nanoseconds for the server. Passing nanoseconds here double-converts to a
# 24-digit value that the server rejects with err_code 10025 "invalid JSON".
_SEVEN_DAYS_SECONDS: float = 7 * 24 * 60 * 60

# JetStream API error code for "stream name already in use" — the only
# BadRequestError we treat as benign on re-create.
_STREAM_ALREADY_EXISTS_CODE = 10058

INGEST_CHANGES_STREAM = "INGEST_CHANGES"
INGEST_DLQ_STREAM = "INGEST_DLQ"
OUTBOX_STREAM = "OUTBOX"

_STREAM_CONFIGS: list[nats.js.api.StreamConfig] = [
    nats.js.api.StreamConfig(
        name=INGEST_CHANGES_STREAM,
        subjects=["ingest.changes.*"],
        retention=nats.js.api.RetentionPolicy.LIMITS,
        max_age=_SEVEN_DAYS_SECONDS,
        storage=nats.js.api.StorageType.FILE,
        num_replicas=1,
    ),
    nats.js.api.StreamConfig(
        name=INGEST_DLQ_STREAM,
        subjects=["ingest.dlq.*"],
        retention=nats.js.api.RetentionPolicy.LIMITS,
        max_age=_SEVEN_DAYS_SECONDS,
        storage=nats.js.api.StorageType.FILE,
        num_replicas=1,
    ),
    nats.js.api.StreamConfig(
        name=OUTBOX_STREAM,
        subjects=["outbox.*"],
        retention=nats.js.api.RetentionPolicy.LIMITS,
        max_age=_SEVEN_DAYS_SECONDS,
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
    """Create a single stream, or skip if it already exists.

    Only "stream name already in use" (err_code 10058) is treated as a benign
    no-op; any other BadRequestError (e.g. 10025 "invalid JSON" from a bad
    config) is re-raised so a misconfiguration fails loudly instead of being
    silently swallowed as "already exists".
    """
    try:
        await js.add_stream(config=cfg)
        log.info("nats_stream_created", stream=cfg.name, subjects=cfg.subjects)
    except BadRequestError as exc:
        if getattr(exc, "err_code", None) != _STREAM_ALREADY_EXISTS_CODE:
            log.error("nats_stream_create_failed", stream=cfg.name, error=str(exc))
            raise
        log.debug("nats_stream_exists", stream=cfg.name)
