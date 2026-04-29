"""Webhook handler for the alerts connector.

Verifies HMAC-SHA256 signatures sent by PagerDuty and Datadog using
``hmac.compare_digest`` for constant-time comparison (no timing oracle).
On verification success the handler parses the payload via the provider's
normaliser and emits one alert :class:`DocumentRef` plus one cross_ref target
per detected entity (ARN, pod, service, trace id).

ACL invariant — workspace_id is NEVER read from the payload (P0)
----------------------------------------------------------------
The handler does not look at, copy, or trust any of the following payload
fields for tenancy decisions: ``tenant_id``, ``customer_id``, ``org_id``,
``routing_key``, ``account_id``, ``team``, custom tags, or any other field.
Workspace identity is derived solely from the URL-path ``{source_name}`` by
the FastAPI receiver in
``apps/server/src/omniscience_server/rest/webhooks.py``.

Webhook secret rotation
-----------------------
Operators may pre-stage a rotated secret by configuring ``webhook_secret`` as
a newline-separated list of currently-valid secrets.  The verifier returns
true if *any* listed secret yields a matching signature, allowing zero-downtime
rotation.  Whitespace and empty lines are ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from omniscience_connectors.base import DocumentRef, WebhookHandler, WebhookPayload

if TYPE_CHECKING:
    from omniscience_connectors.alerts.connector import NormalizedAlert

__all__ = [
    "AlertsWebhookHandler",
    "verify_datadog",
    "verify_pagerduty",
]

logger = logging.getLogger(__name__)

# Header names — lower-cased for dict-of-lower-keys lookup.
_PD_SIG_HEADER = "x-pagerduty-signature"
_DD_SIG_HEADER = "x-datadog-signature"
_DD_TS_HEADER = "x-datadog-signature-timestamp"

# Datadog replay window default (seconds).  Keep in sync with
# ``AlertsConfig.replay_window_seconds`` and the Slack webhook handler.
_DEFAULT_REPLAY_WINDOW_SECONDS = 300


# ---------------------------------------------------------------------------
# Signature verification — pure functions (testable in isolation)
# ---------------------------------------------------------------------------


def _split_secrets(secret: str) -> list[str]:
    """Split a multi-line secret string into individual non-empty values.

    Operators may pre-stage a rotated secret by configuring
    ``webhook_secret`` as multiple newline-separated values; verification
    accepts a request as authentic if *any* of the listed secrets matches.
    """
    return [line.strip() for line in secret.splitlines() if line.strip()]


def verify_pagerduty(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify a PagerDuty webhook HMAC-SHA256 signature.

    PagerDuty sends the header ``X-PagerDuty-Signature`` as a comma-separated
    list of one or more ``v1=<hex>`` values (one per active signing secret on
    the upstream side).  The verifier returns ``True`` if any of the upstream
    signatures matches the HMAC of any of our currently-valid local secrets,
    using :func:`hmac.compare_digest` for constant-time comparison.

    Args:
        payload: Raw request body bytes.
        signature_header: Value of ``X-PagerDuty-Signature``.
        secret: One or more newline-separated secrets (rotation support).

    Returns:
        ``True`` iff the signature is valid.  Returns ``False`` (does not
        raise) on malformed inputs.
    """
    if not signature_header:
        return False
    secrets = _split_secrets(secret)
    if not secrets:
        return False

    candidates = _parse_pd_signatures(signature_header)
    if not candidates:
        return False

    for s in secrets:
        expected = hmac.new(s.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        for received in candidates:
            if hmac.compare_digest(expected, received):
                return True
    return False


def _parse_pd_signatures(header: str) -> list[str]:
    """Extract the hex-encoded ``v1`` HMACs from a PagerDuty signature header."""
    sigs: list[str] = []
    for raw_part in header.split(","):
        part = raw_part.strip()
        if part.startswith("v1="):
            sigs.append(part[len("v1=") :])
    return sigs


def verify_datadog(
    payload: bytes,
    signature_header: str,
    timestamp_header: str,
    secret: str,
    *,
    replay_window_seconds: int = _DEFAULT_REPLAY_WINDOW_SECONDS,
    now: float | None = None,
) -> bool:
    """Verify a Datadog webhook HMAC-SHA256 signature.

    Datadog signs ``"<timestamp>:<raw_body>"`` with HMAC-SHA256.  The hex
    digest arrives in ``X-Datadog-Signature``; the timestamp arrives in
    ``X-Datadog-Signature-Timestamp`` (Unix epoch seconds).  Requests older
    than ``replay_window_seconds`` are rejected to neutralise replay attacks.

    Args:
        payload: Raw request body bytes.
        signature_header: ``X-Datadog-Signature`` value (hex digest, optionally
            prefixed with ``sha256=``).
        timestamp_header: ``X-Datadog-Signature-Timestamp`` value.
        secret: One or more newline-separated secrets (rotation support).
        replay_window_seconds: Maximum allowed clock skew.
        now: Wall-clock time injection (test only).

    Returns:
        ``True`` iff the signature is valid AND the timestamp is fresh.
    """
    if not signature_header or not timestamp_header:
        return False

    try:
        ts = int(timestamp_header)
    except ValueError:
        return False

    wall = time.time() if now is None else now
    if abs(wall - ts) > replay_window_seconds:
        return False

    secrets = _split_secrets(secret)
    if not secrets:
        return False

    received = _strip_sha256_prefix(signature_header.strip())
    base = f"{ts}:".encode() + payload

    for s in secrets:
        expected = hmac.new(s.encode("utf-8"), base, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, received):
            return True
    return False


def _strip_sha256_prefix(value: str) -> str:
    """Strip a ``sha256=`` prefix if present (Datadog payload format variant)."""
    if value.startswith("sha256="):
        return value[len("sha256=") :]
    return value


# ---------------------------------------------------------------------------
# WebhookHandler implementation
# ---------------------------------------------------------------------------


class AlertsWebhookHandler(WebhookHandler):
    """Verifies and parses PagerDuty / Datadog alert webhooks.

    The handler dispatches on the presence of provider-specific signature
    headers — ``X-PagerDuty-Signature`` for PagerDuty, ``X-Datadog-Signature``
    for Datadog.  A request lacking *any* recognised signature header is
    rejected (returns ``False`` from :meth:`verify_signature`).  The receiver
    translates that into a 400 response.
    """

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        """Return ``True`` iff the request bears a valid HMAC signature."""
        lower = {k.lower(): v for k, v in headers.items()}
        pd_sig = lower.get(_PD_SIG_HEADER, "")
        dd_sig = lower.get(_DD_SIG_HEADER, "")
        dd_ts = lower.get(_DD_TS_HEADER, "")

        if pd_sig:
            ok = verify_pagerduty(payload, pd_sig, secret)
            if not ok:
                logger.warning("alerts.webhook.signature_invalid", extra={"provider": "pagerduty"})
            return ok

        if dd_sig:
            ok = verify_datadog(payload, dd_sig, dd_ts, secret)
            if not ok:
                logger.warning("alerts.webhook.signature_invalid", extra={"provider": "datadog"})
            return ok

        logger.warning("alerts.webhook.signature_missing")
        return False

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        """Parse a verified payload into a :class:`WebhookPayload`.

        Dispatches on the same signature-header presence used by
        :meth:`verify_signature`; the receiver guarantees this is only invoked
        after a successful verification.
        """
        lower_headers = {k.lower(): v for k, v in headers.items()}

        try:
            data: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Alerts webhook: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Alerts webhook: top-level payload must be a JSON object")

        provider = self._detect_provider(lower_headers)
        alert = self._normalize(provider, data)

        from omniscience_connectors.alerts.connector import extract_cross_refs

        refs: list[DocumentRef] = extract_cross_refs(alert)
        return WebhookPayload(
            source_name=f"alerts:{provider}",
            affected_refs=refs,
            raw_headers=lower_headers,
        )

    @staticmethod
    def _detect_provider(lower_headers: dict[str, str]) -> str:
        if _PD_SIG_HEADER in lower_headers:
            return "pagerduty"
        if _DD_SIG_HEADER in lower_headers:
            return "datadog"
        # Should never happen in production — verify_signature would have
        # rejected the request first — but be defensive in case of misuse.
        raise ValueError("Alerts webhook: unrecognised provider (no signature header).")

    @staticmethod
    def _normalize(provider: str, data: dict[str, Any]) -> NormalizedAlert:
        from omniscience_connectors.alerts.connector import (
            normalize_datadog,
            normalize_pagerduty,
        )

        if provider == "pagerduty":
            return normalize_pagerduty(data)
        if provider == "datadog":
            return normalize_datadog(data)
        raise ValueError(f"Alerts webhook: unsupported provider {provider!r}")
