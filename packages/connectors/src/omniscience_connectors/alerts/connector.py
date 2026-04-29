"""Alerts source connector — PagerDuty + Datadog webhook ingestion.

This connector is **push-only**.  ``discover()`` yields nothing and ``fetch()``
raises :class:`NotImplementedError`; alerts arrive exclusively through signed
HTTP webhooks delivered to ``POST /api/v1/ingest/webhook/{source_name}``.  The
generic webhook receiver (`apps/server/src/omniscience_server/rest/webhooks.py`)
handles signature verification, replay protection, rate limiting, and tenant
resolution; this module supplies the connector-side
:class:`~omniscience_connectors.base.WebhookHandler` and provider-agnostic
payload normalisation.

ACL invariant — workspace_id ALWAYS from ``Source.tenant_id`` (P0)
------------------------------------------------------------------
Webhook payloads are *tenant-writable upstream content*.  The receiver derives
workspace identity solely from the URL-path ``{source_name}`` resolving to a
:class:`~omniscience_core.db.models.Source` row whose ``tenant_id`` is the
authoritative workspace.  Anything inside the payload — ``tenant_id``,
``customer_id``, custom tags, ``incident.service.id``, Datadog ``tags``,
``routing_key``, ``account_id`` — MUST NOT influence tenancy.  The normalisers
below NEVER read those fields for tenancy purposes; they only carry forensic
context inside ``NormalizedAlert.raw``.

Webhook secret rotation
-----------------------
Operators may rotate the per-source ``webhook_secret`` without downtime by
configuring a newline-separated list of currently-valid secrets.  The receiver
splits on newlines and accepts a request if *any* secret yields a valid HMAC.
See :class:`~omniscience_connectors.alerts.webhook.AlertsWebhookHandler` for
details.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from omniscience_connectors.alerts.webhook import AlertsWebhookHandler
from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
)

__all__ = [
    "AlertsConfig",
    "AlertsConnector",
    "NormalizedAlert",
    "extract_cross_refs",
    "normalize_datadog",
    "normalize_pagerduty",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical name helpers
# ---------------------------------------------------------------------------

#: Canonical AWS ARN regex.  Mirrors the form used by the Slack/AWS connectors.
_ARN_RE = re.compile(r"arn:[a-z0-9-]+:[a-z0-9-]*:[a-z0-9-]*:[0-9]*:[A-Za-z0-9_/.:+=@\\-]+")

#: Canonical Kubernetes pod token: ``pod/<name>`` or ``<namespace>/<name>``.
_POD_RE = re.compile(r"\bpod/([a-z0-9][a-z0-9-]{0,252}[a-z0-9])\b")

#: Trace IDs from OTel: hex strings 16 or 32 chars, prefixed by ``trace://``.
_TRACE_RE = re.compile(r"\btrace://([a-f0-9]{16,32})\b")

#: Generic Kubernetes service token (Datadog tag form ``service:<name>``).
_K8S_NAMESPACED_RE = re.compile(
    r"\b([a-z0-9][a-z0-9-]{0,62})/([a-z0-9][a-z0-9-]{0,252}[a-z0-9])\b"
)


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------


class AlertsConfig(BaseModel):
    """Public configuration for the alerts connector (no secrets).

    The provider is fixed per source — one :class:`Source` row per
    ``(tenant, provider)`` pair so signature verification is unambiguous.
    """

    provider: Literal["pagerduty", "datadog"]
    """Upstream provider.  Determines which signature verifier and normaliser run."""

    signature_algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    """Signature algorithm.  Both providers use HMAC-SHA256 today."""

    replay_window_seconds: int = 300
    """Datadog ``X-Datadog-Signature-Timestamp`` skew tolerance, in seconds.

    Mirrors the Slack connector default.  PagerDuty does not transmit a
    timestamp; replay protection there relies on the receiver-side delivery
    tracker keyed on ``X-PagerDuty-Webhook-Subscription-Id``.
    """


# ---------------------------------------------------------------------------
# Normalised alert schema
# ---------------------------------------------------------------------------


class NormalizedAlert(BaseModel):
    """Provider-agnostic alert payload.

    Both PagerDuty and Datadog webhook bodies are normalised into this shape so
    downstream entity emission and ``resolve_incident`` (Wave 2) treat alerts
    uniformly.  ``raw`` retains the original payload for forensics; the
    normaliser MUST NOT read any field of ``raw`` for tenancy decisions.
    """

    provider: Literal["pagerduty", "datadog"]
    provider_alert_id: str
    severity: Literal["info", "warning", "error", "critical"]
    status: Literal["triggered", "acknowledged", "resolved"]
    fired_at: datetime
    summary: str
    target_arn: str | None = None
    target_pod: str | None = None
    target_service: str | None = None
    trace_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider normalisers
# ---------------------------------------------------------------------------


_PD_SEVERITY_MAP: dict[str, Literal["info", "warning", "error", "critical"]] = {
    "info": "info",
    "warning": "warning",
    "error": "error",
    "critical": "critical",
}

_PD_STATUS_MAP: dict[str, Literal["triggered", "acknowledged", "resolved"]] = {
    "triggered": "triggered",
    "acknowledged": "acknowledged",
    "resolved": "resolved",
}

_DD_TYPE_TO_STATUS: dict[str, Literal["triggered", "acknowledged", "resolved"]] = {
    "alert": "triggered",
    "alert_triggered": "triggered",
    "alert_recovery": "resolved",
    "recovery": "resolved",
    "alert_no_data": "triggered",
    "alert_acknowledged": "acknowledged",
}

_DD_PRIORITY_TO_SEVERITY: dict[str, Literal["info", "warning", "error", "critical"]] = {
    "P1": "critical",
    "P2": "error",
    "P3": "warning",
    "P4": "info",
    "P5": "info",
}


def _coerce_datetime(value: Any) -> datetime:
    """Best-effort coercion of an upstream timestamp into an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # PagerDuty sends epoch ms in some events; Datadog sends epoch seconds.
        seconds = float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(tz=UTC)


def normalize_pagerduty(payload: dict[str, Any]) -> NormalizedAlert:
    """Normalise a PagerDuty webhook body into :class:`NormalizedAlert`.

    PagerDuty wraps the event under ``event.data`` (Webhooks v3).  We treat
    ``event.data.id`` as ``provider_alert_id`` (stable across the lifecycle)
    and read severity/status from the same node.
    """
    event_obj = payload.get("event") or {}
    raw_data = event_obj.get("data") if isinstance(event_obj, dict) else None
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}

    raw_severity = str(data.get("severity") or "warning").lower()
    raw_status = str(data.get("status") or "triggered").lower()
    summary = str(data.get("title") or data.get("summary") or "PagerDuty incident")
    fired_at = _coerce_datetime(
        data.get("created_at")
        or (event_obj.get("occurred_at") if isinstance(event_obj, dict) else None)
    )

    service = data.get("service") or {}
    target_service = (
        str(service.get("summary"))
        if isinstance(service, dict) and service.get("summary")
        else None
    )

    haystack = _build_pd_haystack(data)
    target_arn = _first_match(_ARN_RE, haystack)
    target_pod = _first_pod_match(haystack)
    trace_id = _first_trace_id(haystack)

    return NormalizedAlert(
        provider="pagerduty",
        provider_alert_id=str(data.get("id") or payload.get("id") or ""),
        severity=_PD_SEVERITY_MAP.get(raw_severity, "warning"),
        status=_PD_STATUS_MAP.get(raw_status, "triggered"),
        fired_at=fired_at,
        summary=summary,
        target_arn=target_arn,
        target_pod=target_pod,
        target_service=target_service,
        trace_id=trace_id,
        raw=payload,
    )


def normalize_datadog(payload: dict[str, Any]) -> NormalizedAlert:
    """Normalise a Datadog alert webhook body into :class:`NormalizedAlert`.

    Datadog payloads vary by template, but commonly carry ``id``,
    ``alert_type``/``event_type``, ``priority``, ``title``, ``date_happened``,
    and a ``tags`` list (e.g. ``["service:checkout", "env:prod"]``).
    """
    alert_id = str(payload.get("id") or payload.get("alert_id") or "")
    raw_priority = str(payload.get("priority") or "P3").upper()
    severity = _DD_PRIORITY_TO_SEVERITY.get(raw_priority, "warning")

    raw_type = str(payload.get("alert_type") or payload.get("event_type") or "alert").lower()
    status = _DD_TYPE_TO_STATUS.get(raw_type, "triggered")

    summary = str(payload.get("title") or payload.get("alert_title") or "Datadog alert")
    fired_at = _coerce_datetime(
        payload.get("date_happened") or payload.get("last_updated") or payload.get("timestamp")
    )

    tags = payload.get("tags")
    tag_list: list[str] = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
    target_service = _tag_value(tag_list, "service")

    haystack = _build_dd_haystack(payload, tag_list)
    target_arn = _first_match(_ARN_RE, haystack)
    target_pod = _tag_value(tag_list, "pod_name") or _first_pod_match(haystack)
    trace_id = _first_trace_id(haystack) or _tag_value(tag_list, "trace_id")

    return NormalizedAlert(
        provider="datadog",
        provider_alert_id=alert_id,
        severity=severity,
        status=status,
        fired_at=fired_at,
        summary=summary,
        target_arn=target_arn,
        target_pod=target_pod,
        target_service=target_service,
        trace_id=trace_id,
        raw=payload,
    )


def _build_pd_haystack(data: dict[str, Any]) -> str:
    """Concatenate PagerDuty payload fields where named-entity refs may live."""
    custom_details = data.get("custom_details") or {}
    pieces: list[str] = [
        str(data.get("title") or ""),
        str(data.get("summary") or ""),
        str(data.get("description") or ""),
    ]
    if isinstance(custom_details, dict):
        pieces.extend(str(v) for v in custom_details.values())
    return " \n ".join(p for p in pieces if p)


def _build_dd_haystack(payload: dict[str, Any], tags: list[str]) -> str:
    """Concatenate Datadog payload fields plus tags into a search haystack."""
    pieces: list[str] = [
        str(payload.get("title") or ""),
        str(payload.get("body") or payload.get("alert_message") or ""),
        " ".join(tags),
    ]
    return " \n ".join(p for p in pieces if p)


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    if not text:
        return None
    match = pattern.search(text)
    return match.group(0) if match else None


def _first_trace_id(text: str) -> str | None:
    """Return the bare trace-id hex string from a ``trace://<hex>`` token.

    The cross_ref emitter re-prefixes with ``trace://`` so the canonical name
    used for linking matches the form emitted by the OTel receiver (#152).
    """
    if not text:
        return None
    match = _TRACE_RE.search(text)
    return match.group(1) if match else None


def _first_pod_match(text: str) -> str | None:
    """Return ``pod/<name>`` if found, else the first ``<namespace>/<name>`` token."""
    if not text:
        return None
    pod_match = _POD_RE.search(text)
    if pod_match:
        return pod_match.group(0)
    ns_match = _K8S_NAMESPACED_RE.search(text)
    return ns_match.group(0) if ns_match else None


def _tag_value(tags: list[str], key: str) -> str | None:
    """Return the value of a Datadog tag like ``key:value`` (first match only)."""
    prefix = f"{key}:"
    for tag in tags:
        if tag.startswith(prefix):
            value = tag[len(prefix) :].strip()
            if value:
                return value
    return None


# ---------------------------------------------------------------------------
# Cross-ref extraction
# ---------------------------------------------------------------------------


def extract_cross_refs(alert: NormalizedAlert) -> list[DocumentRef]:
    """Build the list of named-entity reference :class:`DocumentRef`s.

    The first ref is always the alert entity itself
    (``alert://{provider}/{provider_alert_id}``).  Each subsequent ref carries
    a canonical name in its ``uri`` field that the
    :class:`~omniscience_index.linker.EntityLinker` resolves via ``exact_name``
    or ``arn_match`` strategies, producing the ``[:FIRES_AGAINST]`` cross_ref
    edges described in the architect memo.

    Importantly, this function only inspects ``alert`` (the normalised model);
    raw payload tenant fields are never consulted.
    """
    alert_name = f"alert://{alert.provider}/{alert.provider_alert_id}"
    refs: list[DocumentRef] = [
        DocumentRef(
            external_id=alert_name,
            uri=alert_name,
            updated_at=alert.fired_at,
            metadata={
                "kind": "alert",
                "name": alert_name,
                "provider": alert.provider,
                "provider_alert_id": alert.provider_alert_id,
                "severity": alert.severity,
                "status": alert.status,
                "fired_at": alert.fired_at.isoformat(),
                "summary": alert.summary,
                "target_arn": alert.target_arn,
                "target_pod": alert.target_pod,
                "target_service": alert.target_service,
                "trace_id": alert.trace_id,
            },
        )
    ]

    if alert.target_arn:
        refs.append(
            DocumentRef(
                external_id=alert.target_arn,
                uri=alert.target_arn,
                metadata={
                    "kind": "cross_ref_target",
                    "name": alert.target_arn,
                    "edge_type": "FIRES_AGAINST",
                    "from_alert": alert_name,
                },
            )
        )

    if alert.target_pod:
        refs.append(
            DocumentRef(
                external_id=alert.target_pod,
                uri=alert.target_pod,
                metadata={
                    "kind": "cross_ref_target",
                    "name": alert.target_pod,
                    "edge_type": "FIRES_AGAINST",
                    "from_alert": alert_name,
                },
            )
        )

    if alert.target_service:
        service_name = f"service://{alert.target_service}"
        refs.append(
            DocumentRef(
                external_id=service_name,
                uri=service_name,
                metadata={
                    "kind": "cross_ref_target",
                    "name": service_name,
                    "edge_type": "FIRES_AGAINST",
                    "from_alert": alert_name,
                },
            )
        )

    if alert.trace_id:
        trace_name = f"trace://{alert.trace_id}"
        refs.append(
            DocumentRef(
                external_id=trace_name,
                uri=trace_name,
                metadata={
                    "kind": "cross_ref_target",
                    "name": trace_name,
                    "edge_type": "FIRES_AGAINST",
                    "from_alert": alert_name,
                },
            )
        )

    return refs


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class AlertsConnector(Connector):
    """Push-only connector for SRE alerting webhooks (PagerDuty, Datadog).

    ``discover()`` is a no-op — alerts are not enumerable.  ``fetch()`` raises
    :class:`NotImplementedError` because the alert content arrives in full with
    the webhook delivery and is processed inline by the receiver.
    """

    connector_type: ClassVar[str] = "alerts"
    config_schema: ClassVar[type[BaseModel]] = AlertsConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Validate that *config* is well-formed and *secrets* contains a webhook secret."""
        cfg: AlertsConfig = config  # type: ignore[assignment]
        if cfg.provider not in ("pagerduty", "datadog"):
            raise ValueError(f"Unsupported alerts provider: {cfg.provider!r}")
        if not (secrets.get("webhook_secret") or "").strip():
            raise ValueError("Alerts connector requires a 'webhook_secret' in secrets.")

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """No-op — alerts are exclusively push-based.

        Returns immediately without yielding any refs.  Implemented as an
        async generator (with an unreachable ``yield``) to satisfy the
        :class:`~omniscience_connectors.base.Connector` protocol.
        """
        return
        yield  # pragma: no cover  # type: ignore[unreachable]

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Not supported — alert content arrives in full via the webhook payload."""
        raise NotImplementedError(
            "AlertsConnector is push-only; fetch() is not supported. "
            "Alert payloads are delivered in full by the upstream provider's webhook."
        )

    def webhook_handler(self) -> WebhookHandler | None:
        return AlertsWebhookHandler()
