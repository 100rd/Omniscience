"""Webhook ingestion endpoint.

POST /api/v1/ingest/webhook/{source_name}

Receives push events from source systems (GitHub, GitLab, Confluence).

Processing pipeline:

1. Look up the named source in the database (404 if not found).
2. Attempt to obtain a :class:`~omniscience_connectors.base.WebhookHandler`
   from the registered connector for this source type.  If no connector or no
   handler is available the legacy built-in signature helpers are used as
   fallback.
3. Verify the HMAC / token signature (400 if invalid).
4. Extract the delivery ID from request headers and reject duplicates within
   the configured replay-protection window (400 if duplicate or too old).
5. Check per-source rate limit (429 if exceeded).
6. Delegate payload parsing to the connector's handler to obtain the list of
   affected :class:`~omniscience_connectors.base.DocumentRef` objects.
7. Enqueue a :class:`~omniscience_server.ingestion.events.DocumentChangeEvent`
   for each affected document via NATS JetStream.
8. Return 202 Accepted with ``{"accepted": true, "events_queued": N}``.

No authentication token is required — webhook endpoints are authenticated via
HMAC signature (the shared secret configured per source).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from omniscience_core.db.models import Source
from omniscience_core.queue.producer import QueueProducer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.rest.delivery_tracker import DeliveryTracker

log = structlog.get_logger(__name__)

router = APIRouter(tags=["webhooks"])

# ---------------------------------------------------------------------------
# Module-level singletons (single-process; replace with Redis for multi-proc)
# ---------------------------------------------------------------------------

# Shared replay-protection tracker (configurable window; default 5 min)
_REPLAY_WINDOW_SECONDS: float = 300.0
_delivery_tracker = DeliveryTracker(window_seconds=_REPLAY_WINDOW_SECONDS)

# Per-source rate-limiting: source_id (str) -> (tokens: float, last_refill: float)
_source_buckets: dict[str, tuple[float, float]] = {}
_DEFAULT_SOURCE_RPM: int = 120


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WebhookAcceptedResponse(BaseModel):
    """Confirmation that the webhook was accepted and events enqueued."""

    accepted: bool
    events_queued: int


# ---------------------------------------------------------------------------
# Signature verification helpers (legacy / fallback)
# ---------------------------------------------------------------------------


def _verify_github_signature(payload: bytes, secret: str, signature_header: str | None) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature.

    GitHub sends the signature as ``X-Hub-Signature-256: sha256=<hex>``.
    """
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    received = signature_header[len("sha256=") :]
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def _verify_gitlab_signature(payload: bytes, secret: str, token_header: str | None) -> bool:
    """Verify a GitLab webhook token.

    GitLab sends the secret token as ``X-Gitlab-Token: <secret>``.
    This is a simple equality check, not HMAC.
    """
    if not token_header:
        return False
    return hmac.compare_digest(secret, token_header)


def _verify_confluence_signature(
    payload: bytes, secret: str, signature_header: str | None
) -> bool:
    """Verify a Confluence webhook HMAC-SHA256 signature.

    Confluence sends ``X-Hub-Signature: sha256=<hex>`` similar to GitHub.
    """
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    received = signature_header[len("sha256=") :]
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def verify_webhook_signature(
    source_type: str,
    payload: bytes,
    secret: str,
    request: Request,
) -> bool:
    """Dispatch to the appropriate signature verifier based on source type.

    This is the *fallback* path used when no connector-specific
    :class:`~omniscience_connectors.base.WebhookHandler` is registered for the
    given source type.

    Args:
        source_type: Source type string (e.g. ``"git"``, ``"gitlab"``, ``"confluence"``).
        payload:     Raw request body bytes.
        secret:      Shared secret configured on the source.
        request:     The FastAPI/Starlette request (for reading headers).

    Returns:
        ``True`` if the signature is valid; ``False`` otherwise.
    """
    if source_type in ("git", "github"):
        sig = request.headers.get("X-Hub-Signature-256")
        return _verify_github_signature(payload, secret, sig)

    if source_type == "gitlab":
        token = request.headers.get("X-Gitlab-Token")
        return _verify_gitlab_signature(payload, secret, token)

    if source_type == "confluence":
        sig = request.headers.get("X-Hub-Signature")
        return _verify_confluence_signature(payload, secret, sig)

    # Unknown source type — allow through (no signature to verify)
    log.warning("webhook_no_signature_check", source_type=source_type)
    return True


# ---------------------------------------------------------------------------
# Delivery ID extraction helpers
# ---------------------------------------------------------------------------


def _extract_delivery_id(request: Request) -> str | None:
    """Extract the source-specific delivery identifier from request headers.

    * GitHub: ``X-GitHub-Delivery`` header
    * GitLab: ``X-Gitlab-Event-UUID`` header

    Returns ``None`` if no recognised delivery header is present.
    """
    for header in ("x-github-delivery", "x-gitlab-event-uuid"):
        value = request.headers.get(header)
        if value:
            return value
    return None


def _extract_payload_timestamp(payload_data: Any) -> float | None:
    """Try to read a UNIX timestamp from a webhook JSON payload.

    GitHub includes a ``timestamp`` field in some events; GitLab uses
    ``object_attributes.created_at``.  We do a best-effort extraction.
    Returns the timestamp as a POSIX float, or ``None`` if not found.
    """
    if not isinstance(payload_data, dict):
        return None

    # Direct timestamp field (ISO 8601 or UNIX epoch integer)
    for key in ("timestamp", "created_at", "pushed_at"):
        value = payload_data.get(key)
        if value is not None and isinstance(value, (int, float)):
            return float(value)
        # ISO 8601 strings are not parsed here to keep dependencies minimal;
        # callers that need strict timestamp enforcement should extend this.

    return None


# ---------------------------------------------------------------------------
# Per-source rate limiting
# ---------------------------------------------------------------------------


def _check_source_rate_limit(source_id: str, rpm: int) -> tuple[bool, float]:
    """Token-bucket rate limit check for a given source.

    Args:
        source_id: String form of the source UUID.
        rpm:       Requests-per-minute limit.

    Returns:
        ``(allowed, retry_after_seconds)`` — when *allowed* is ``False``,
        *retry_after_seconds* is approximate seconds until one token refills.
    """
    now = time.monotonic()
    capacity = float(rpm)
    refill_rate = capacity / 60.0  # tokens per second

    if source_id not in _source_buckets:
        _source_buckets[source_id] = (capacity - 1.0, now)
        return True, 0.0

    tokens, last_refill = _source_buckets[source_id]
    elapsed = now - last_refill
    tokens = min(capacity, tokens + elapsed * refill_rate)

    if tokens >= 1.0:
        _source_buckets[source_id] = (tokens - 1.0, now)
        return True, 0.0

    retry_after = (1.0 - tokens) / refill_rate
    _source_buckets[source_id] = (tokens, now)
    return False, retry_after


def reset_source_rate_limit(source_id: str) -> None:
    """Reset the rate-limit bucket for *source_id* (test helper)."""
    _source_buckets.pop(source_id, None)


def clear_all_source_buckets() -> None:
    """Clear all per-source rate-limit buckets (test helper)."""
    _source_buckets.clear()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/ingest/webhook/{source_name}",
    response_model=WebhookAcceptedResponse,
    status_code=202,
    summary="Receive a webhook push event",
)
async def receive_webhook(
    source_name: str,
    request: Request,
) -> WebhookAcceptedResponse:
    """Receive a webhook payload from a push-event source.

    Processing steps:

    1. Look up the named source in the database.
    2. Check the per-source rate limit.
    3. Verify the signature via the connector's :class:`WebhookHandler` or the
       built-in fallback verifiers.
    4. Check for duplicate or expired delivery IDs.
    5. Parse the payload via the connector handler (or produce a generic event).
    6. Enqueue :class:`~omniscience_server.ingestion.events.DocumentChangeEvent`
       messages for each affected document.
    7. Return 202 Accepted.

    Returns:
        :class:`WebhookAcceptedResponse` with ``accepted=True`` and
        ``events_queued`` equal to the number of events published.

    Raises:
        HTTPException 404: Source name not found.
        HTTPException 400: Signature invalid or duplicate/expired delivery.
        HTTPException 429: Per-source rate limit exceeded.
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        log.warning("webhook_no_db", source_name=source_name)
        return WebhookAcceptedResponse(accepted=True, events_queued=0)

    # Read the raw body before anything else — needed for signature verification.
    payload_bytes = await request.body()

    db: AsyncSession
    async with factory() as db:
        result = await db.execute(select(Source).where(Source.name == source_name))
        source: Source | None = result.scalars().first()

    if source is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "source_not_found",
                "message": f"Source '{source_name}' not found",
            },
        )

    source_id_str = str(source.id)
    source_type = str(source.type)

    # --- Per-source rate limit ---
    settings = getattr(request.app.state, "settings", None)
    rpm: int = (
        int(getattr(settings, "webhook_rpm", _DEFAULT_SOURCE_RPM))
        if settings
        else _DEFAULT_SOURCE_RPM
    )
    allowed, retry_after = _check_source_rate_limit(source_id_str, rpm)
    if not allowed:
        retry_int = int(retry_after) + 1
        log.warning("webhook_rate_limited", source_name=source_name, source_id=source_id_str)
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "Webhook rate limit exceeded for this source",
                "retry_after": str(retry_int),
            },
            headers={"Retry-After": str(retry_int)},
        )

    # --- Signature verification ---
    webhook_secret: str | None = source.config.get("webhook_secret") if source.config else None

    # Try connector-based handler first; fall back to built-in helpers.
    connector_handler = _get_connector_handler(source_type)
    headers_dict = dict(request.headers)

    if webhook_secret:
        if connector_handler is not None:
            try:
                valid = await connector_handler.verify_signature(
                    payload=payload_bytes,
                    headers=headers_dict,
                    secret=webhook_secret,
                )
            except Exception as exc:  # pragma: no cover
                log.warning(
                    "webhook_handler_verify_error",
                    source_name=source_name,
                    error=str(exc),
                )
                valid = False
        else:
            valid = verify_webhook_signature(
                source_type=source_type,
                payload=payload_bytes,
                secret=webhook_secret,
                request=request,
            )

        if not valid:
            log.warning(
                "webhook_signature_invalid",
                source_name=source_name,
                source_id=source_id_str,
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "bad_request",
                    "message": "Webhook signature verification failed",
                },
            )

    # --- Replay protection ---
    delivery_id = _extract_delivery_id(request)

    # Reject duplicate delivery IDs within the replay window.
    if delivery_id is not None and await _delivery_tracker.is_duplicate(delivery_id):
        log.warning(
            "webhook_duplicate_delivery",
            source_name=source_name,
            delivery_id=delivery_id,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_request",
                "message": "Duplicate webhook delivery rejected",
            },
        )

    # Best-effort timestamp check: reject payloads claiming to be older than window.
    try:
        payload_data: Any = json.loads(payload_bytes)
    except (json.JSONDecodeError, ValueError):
        payload_data = None

    ts = _extract_payload_timestamp(payload_data)
    if ts is not None:
        age = time.time() - ts
        if age > _REPLAY_WINDOW_SECONDS:
            log.warning(
                "webhook_expired_timestamp",
                source_name=source_name,
                age_seconds=age,
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "bad_request",
                    "message": "Webhook payload timestamp is too old",
                },
            )

    # Record delivery ID now that all checks have passed.
    if delivery_id is not None:
        await _delivery_tracker.record(delivery_id)

    # --- Parse payload and build change events ---
    affected_refs = await _parse_payload(
        source_type=source_type,
        source_name=source_name,
        payload_bytes=payload_bytes,
        headers_dict=headers_dict,
        connector_handler=connector_handler,
    )

    events: list[DocumentChangeEvent] = [
        DocumentChangeEvent(
            source_id=source.id,
            source_type=source_type,
            external_id=ref.external_id,
            uri=ref.uri,
            action="updated",
        )
        for ref in affected_refs
    ]

    # If no refs were parsed from the connector, emit a single generic event
    # so that the ingestion worker still receives a signal to re-sync the source.
    if not events:
        events = [
            DocumentChangeEvent(
                source_id=source.id,
                source_type=source_type,
                external_id="*",
                uri=f"webhook://{source_name}",
                action="updated",
            )
        ]

    # --- Enqueue to NATS JetStream ---
    events_queued = await _enqueue_events(request, events, source_type)

    log.info(
        "webhook_accepted",
        source_name=source_name,
        source_id=source_id_str,
        events_queued=events_queued,
    )
    return WebhookAcceptedResponse(accepted=True, events_queued=events_queued)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_connector_handler(source_type: str) -> Any:
    """Return the :class:`~omniscience_connectors.base.WebhookHandler` for
    *source_type*, or ``None`` if not available.

    Gracefully handles the case where the connector package is not registered.
    """
    try:
        from omniscience_connectors.registry import get_connector

        connector = get_connector(source_type)
        return connector.webhook_handler()
    except Exception:  # NotFoundError or any import error
        return None


async def _parse_payload(
    source_type: str,
    source_name: str,
    payload_bytes: bytes,
    headers_dict: dict[str, str],
    connector_handler: Any,
) -> list[Any]:
    """Use the connector handler to parse the payload and return affected refs.

    Falls back to an empty list when parsing fails or no handler is available.
    """

    if connector_handler is None:
        return []

    try:
        webhook_payload = await connector_handler.parse_payload(
            payload=payload_bytes,
            headers=headers_dict,
        )
        return list(webhook_payload.affected_refs)
    except Exception as exc:
        log.warning(
            "webhook_handler_parse_error",
            source_name=source_name,
            source_type=source_type,
            error=str(exc),
        )
        return []


async def _enqueue_events(
    request: Request,
    events: list[DocumentChangeEvent],
    source_type: str,
) -> int:
    """Publish *events* to NATS JetStream.

    Returns the number of successfully enqueued events.  Failures are logged
    but do not cause the request to fail — the webhook is still acknowledged
    so the source system does not retry unnecessarily.
    """
    nats_conn = getattr(request.app.state, "nats", None)
    if nats_conn is None:
        log.debug("webhook_nats_unavailable_skipping_enqueue", count=len(events))
        return 0

    js = getattr(nats_conn, "jetstream", None)
    if js is None:
        log.debug("webhook_nats_no_jetstream", count=len(events))
        return 0

    producer = QueueProducer(js)
    subject = f"ingest.changes.{source_type}"
    enqueued = 0

    for event in events:
        try:
            await producer.publish(subject=subject, payload=event)
            enqueued += 1
        except Exception as exc:
            log.error(
                "webhook_enqueue_error",
                subject=subject,
                external_id=event.external_id,
                error=str(exc),
            )

    return enqueued


__all__ = [
    "WebhookAcceptedResponse",
    "clear_all_source_buckets",
    "reset_source_rate_limit",
    "router",
    "verify_webhook_signature",
]
