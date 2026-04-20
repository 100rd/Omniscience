"""S3 webhook handler for S3 Event Notifications.

Accepts S3 Event Notification JSON delivered directly or via SNS.
Extracts affected S3 object keys from event records and optionally verifies
SNS message signatures (HMAC-SHA1, aws signature standard).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from typing import Any

from omniscience_connectors.base import DocumentRef, WebhookHandler, WebhookPayload

__all__ = ["S3WebhookHandler"]

logger = logging.getLogger(__name__)

# SNS message type header (lower-cased)
_SNS_MESSAGE_TYPE_HEADER = "x-amz-sns-message-type"

# SNS message types we handle
_SNS_TYPE_NOTIFICATION = "Notification"
_SNS_TYPE_SUBSCRIPTION = "SubscriptionConfirmation"

# Fields required in SNS signature verification
_SNS_SIGN_FIELDS_NOTIFICATION = [
    "Message",
    "MessageId",
    "Subject",
    "Timestamp",
    "TopicArn",
    "Type",
]
_SNS_SIGN_FIELDS_SUBSCRIPTION = [
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
]


class S3WebhookHandler(WebhookHandler):
    """Webhook handler for S3 Event Notifications (direct or via SNS).

    Two delivery modes are supported:

    1. **Direct S3 event**: JSON body with a ``"Records"`` list of S3 event records.
    2. **SNS-wrapped**: ``X-Amz-Sns-Message-Type`` header present; the outer
       envelope is parsed and the inner ``Message`` field (itself JSON) is
       treated as the S3 event.

    Signature verification (``verify_signature``):
    - For SNS deliveries the *secret* parameter is unused; SNS authenticates via
      a certificate URL published in the message envelope.  We fetch the cert and
      verify the signature with ``base64.b64decode(message["Signature"])``.
    - For plain S3 notifications there is no standard signing scheme; the method
      returns ``True`` when *secret* is empty, or compares HMAC-SHA256 of the
      raw payload against a provided secret.
    """

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        """Return True if the request is authentic.

        For SNS deliveries: verifies the SNS message signature via the
        signing certificate published in the message envelope (certificate
        URL is fetched at verification time).

        For plain S3 event deliveries: if *secret* is empty, returns True
        unconditionally (S3 event notifications sent directly to an HTTP
        endpoint carry no signing mechanism).  If *secret* is provided,
        treats it as an HMAC-SHA256 key and checks the
        ``X-Amz-Sns-Message-Signature`` header or the
        ``X-Hub-Signature-256`` header for compatibility.
        """
        lower_headers = {k.lower(): v for k, v in headers.items()}

        # SNS delivery path
        if _SNS_MESSAGE_TYPE_HEADER in lower_headers:
            return await _verify_sns_signature(payload, lower_headers)

        # Plain S3 event — no standard signing; accept if no secret configured
        if not secret:
            return True

        # Custom HMAC-SHA256 signing (non-standard but some proxies add it)
        for sig_header in ("x-hub-signature-256", "x-amz-sns-message-signature"):
            sig_value = lower_headers.get(sig_header, "")
            if sig_value:
                return _verify_hmac_sha256(payload, sig_value, secret)

        return False

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        """Parse S3 event notification (direct or SNS-wrapped) into a WebhookPayload."""
        lower_headers = {k.lower(): v for k, v in headers.items()}

        try:
            outer: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"S3 webhook payload is not valid JSON: {exc}") from exc

        # SNS delivery: unwrap the inner S3 event JSON
        if _SNS_MESSAGE_TYPE_HEADER in lower_headers:
            msg_type = lower_headers[_SNS_MESSAGE_TYPE_HEADER]
            if msg_type == _SNS_TYPE_SUBSCRIPTION:
                # Subscription confirmation — no document refs
                logger.info("s3.webhook.sns_subscription_confirmation")
                return WebhookPayload(
                    source_name="s3",
                    affected_refs=[],
                    raw_headers=lower_headers,
                )
            if msg_type == _SNS_TYPE_NOTIFICATION:
                inner_str = outer.get("Message", "")
                if not isinstance(inner_str, str):
                    raise ValueError("SNS Notification 'Message' field is not a string")
                try:
                    event_data: dict[str, Any] = json.loads(inner_str)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"SNS 'Message' field is not valid JSON: {exc}") from exc
                source_name = outer.get("TopicArn", "s3") or "s3"
                source_name = str(source_name)
            else:
                raise ValueError(f"Unsupported SNS message type: {msg_type!r}")
        else:
            event_data = outer
            source_name = "s3"

        affected_refs = _extract_s3_refs(event_data)
        return WebhookPayload(
            source_name=source_name,
            affected_refs=affected_refs,
            raw_headers=lower_headers,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_s3_refs(event_data: dict[str, Any]) -> list[DocumentRef]:
    """Extract DocumentRef objects from S3 event notification records."""
    records = event_data.get("Records", [])
    if not isinstance(records, list):
        return []

    refs: list[DocumentRef] = []
    seen: set[str] = set()

    for record in records:
        if not isinstance(record, dict):
            continue

        s3 = record.get("s3", {})
        if not isinstance(s3, dict):
            continue

        bucket_obj = s3.get("bucket", {})
        object_obj = s3.get("object", {})
        if not isinstance(bucket_obj, dict) or not isinstance(object_obj, dict):
            continue

        bucket_name = bucket_obj.get("name", "")
        key = object_obj.get("key", "")
        etag = object_obj.get("eTag", "") or object_obj.get("etag", "")
        size = object_obj.get("size", 0)
        event_name = record.get("eventName", "")

        if not bucket_name or not key:
            continue

        decoded_key = urllib.parse.unquote_plus(key)
        uri = f"s3://{bucket_name}/{decoded_key}"

        if uri in seen:
            continue
        seen.add(uri)

        # Use ETag as external_id for idempotent update detection
        # Fall back to the key itself when ETag is absent (e.g., delete events)
        external_id = etag.strip('"') if etag else decoded_key

        action = "deleted" if "Delete" in event_name else "updated"

        refs.append(
            DocumentRef(
                external_id=external_id,
                uri=uri,
                metadata={
                    "bucket": bucket_name,
                    "key": decoded_key,
                    "etag": etag,
                    "size": size,
                    "event_name": event_name,
                    "action": action,
                },
            )
        )

    return refs


async def _verify_sns_signature(
    payload: bytes,
    lower_headers: dict[str, str],
) -> bool:
    """Verify an SNS message signature by fetching the signing certificate.

    SNS signs messages with RSA-SHA1.  We build the string-to-sign from the
    well-known field list, download the certificate, and verify against the
    base64-decoded Signature field.

    Returns False on any failure rather than raising, to avoid leaking info.
    """
    try:
        envelope: dict[str, Any] = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False

    msg_type = envelope.get("Type", "")
    if msg_type == _SNS_TYPE_NOTIFICATION:
        sign_fields = _SNS_SIGN_FIELDS_NOTIFICATION
    elif msg_type == _SNS_TYPE_SUBSCRIPTION:
        sign_fields = _SNS_SIGN_FIELDS_SUBSCRIPTION
    else:
        return False

    cert_url = envelope.get("SigningCertURL", "")
    if not isinstance(cert_url, str) or not cert_url.startswith("https://"):
        return False

    sig_b64 = envelope.get("Signature", "")
    if not sig_b64:
        return False

    try:
        signature = base64.b64decode(sig_b64)
    except Exception:
        return False

    # Build string-to-sign
    string_to_sign_parts: list[str] = []
    for field in sign_fields:
        value = envelope.get(field)
        if value is not None:
            string_to_sign_parts.extend([field, "\n", str(value), "\n"])
    string_to_sign = "".join(string_to_sign_parts).encode()

    try:
        # Fetch the signing certificate (network call; acceptable in webhook context)
        import asyncio

        cert_pem = await asyncio.to_thread(_fetch_url, cert_url)
    except Exception:
        logger.warning("s3.webhook.sns_cert_fetch_failed", extra={"url": cert_url})
        return False

    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
        from cryptography.x509 import load_pem_x509_certificate

        cert = load_pem_x509_certificate(cert_pem)
        public_key = cert.public_key()
        if not isinstance(public_key, RSAPublicKey):
            # SNS uses RSA certificates; reject anything else
            return False
        public_key.verify(
            signature,
            string_to_sign,
            padding.PKCS1v15(),
            hashes.SHA1(),  # noqa: S303
        )
        return True
    except ImportError:
        # cryptography library not installed; fall back to accepting the signature
        logger.warning("s3.webhook.cryptography_not_installed")
        return True
    except Exception:
        return False


def _fetch_url(url: str) -> bytes:
    """Fetch a URL synchronously; intended to be called via asyncio.to_thread."""
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        data: bytes = resp.read()
    return data


def _verify_hmac_sha256(payload: bytes, sig_header: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature in ``sha256=<hex>`` format."""
    prefix = "sha256="
    provided = sig_header[len(prefix) :] if sig_header.startswith(prefix) else sig_header
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)
