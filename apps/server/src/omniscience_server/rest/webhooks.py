"""Webhook ingestion endpoint.

POST /api/v1/ingest/webhook/{source_name}

Receives push events from source systems (GitHub, GitLab, Confluence).
Validates the payload signature per source type, then enqueues a sync task.

No authentication token required — webhook endpoints are authenticated via
HMAC signature (the shared secret configured per source).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from omniscience_core.db.models import IngestionRun, IngestionRunStatus, Source
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

router = APIRouter(tags=["webhooks"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WebhookAcceptedResponse(BaseModel):
    """Confirmation that the webhook was accepted and a sync enqueued."""

    accepted: bool
    run_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# Signature verification helpers
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

    Args:
        source_type: Source type string (e.g. "git", "gitlab", "confluence").
        payload:     Raw request body bytes.
        secret:      Shared secret configured on the source.
        request:     The FastAPI/Starlette request (for reading headers).

    Returns:
        True if the signature is valid; False otherwise.
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

    1. Looks up the named source in the database.
    2. Validates the HMAC/token signature using the secret stored in the source config.
    3. Creates an ingestion run record.
    4. Enqueues a sync task (placeholder — real NATS publish in issue #6).
    5. Returns 202 Accepted with the run_id.

    Returns 404 if the source name is not found.
    Returns 401 if signature verification fails.
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        # No DB: accept and drop (degraded mode)
        log.warning("webhook_no_db", source_name=source_name)
        return WebhookAcceptedResponse(accepted=True, run_id=None)

    # Read the raw body before anything else — needed for signature verification
    payload_bytes = await request.body()

    db: AsyncSession
    async with factory() as db:
        result = await db.execute(select(Source).where(Source.name == source_name))
        source = result.scalars().first()

        if source is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "source_not_found",
                    "message": f"Source '{source_name}' not found",
                },
            )

        # Signature verification — only if a webhook_secret is configured
        webhook_secret: str | None = source.config.get("webhook_secret") if source.config else None
        if webhook_secret:
            valid = verify_webhook_signature(
                source_type=str(source.type),
                payload=payload_bytes,
                secret=webhook_secret,
                request=request,
            )
            if not valid:
                log.warning(
                    "webhook_signature_invalid",
                    source_name=source_name,
                    source_id=str(source.id),
                )
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "unauthorized",
                        "message": "Webhook signature verification failed",
                    },
                )

        # Parse payload for logging (best-effort)
        try:
            _payload_data: Any = json.loads(payload_bytes)
        except (json.JSONDecodeError, ValueError):
            _payload_data = None

        # Create ingestion run
        run = IngestionRun(
            source_id=source.id,
            status=IngestionRunStatus.running,
        )
        db.add(run)
        await db.flush()
        await db.refresh(run)
        await db.commit()

        run_id: uuid.UUID = run.id

    # TODO(issue-6): Publish sync task to NATS JetStream
    # nats = getattr(request.app.state, "nats", None)
    # if nats is not None:
    #     msg = json.dumps(
    #         {"source_id": str(source.id), "run_id": str(run_id), "trigger": "webhook"}
    #     )
    #     await nats.jetstream.publish("sync.trigger", msg.encode())

    log.info(
        "webhook_accepted",
        source_name=source_name,
        source_id=str(source.id),
        run_id=str(run_id),
    )
    return WebhookAcceptedResponse(accepted=True, run_id=run_id)


__all__ = ["router", "verify_webhook_signature"]
