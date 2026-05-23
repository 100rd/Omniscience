"""Datadog source connector — monitors, dashboards, service catalog, SLOs, events.

This connector turns the four most useful Datadog configuration surfaces
(monitors, dashboards, service catalog, SLOs) plus the audit/alert event stream
into first-class bitemporal entities, completing the alert -> service ->
host/pod causal chain for the most common observability source in our ICP.

Authentication
--------------
Two secrets are required (resolved at call time from the per-source secrets
dict, never logged):

* ``datadog_api_key`` — Datadog API key (rotates freely).
* ``datadog_app_key`` — Datadog Application key with read scopes for monitors,
  dashboards, service catalog, SLOs, and events.

The required token scopes are documented in ``docs/connectors/datadog.md``.

Workspace scoping
-----------------
Datadog API tokens are organisation-scoped.  An Omniscience workspace pins
exactly one ``(org_uid, site)`` pair via :class:`DatadogConfig`; multi-org
ingestion requires multiple Source rows.  This is identical to the GitHub PR
connector's repo-list pattern.

Rate limiting
-------------
Datadog's default tier allows ~600 requests/minute, with per-endpoint family
quotas exposed via ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``.  The
connector tracks the headers per response and sleeps until reset whenever the
remaining budget is exhausted, capped at one hour as a defence against bogus
reset values.  No silent retries on other 4xx — those bubble up to the caller.

Canonical names
---------------
Every emitted entity has a deterministic ``datadog://`` URN as its ``name`` so
``EntityLinker.exact_name`` can attach ``cross_ref`` edges from Slack mentions
of monitor URLs (the existing Datadog Slack mention extractor canonicalises
``https://app.datadoghq.com/monitors/{id}`` -> ``datadog://monitor/{org}/{id}``).
This contract is asserted by the regex tests in ``test_datadog_connector.py``.

Bitemporality
-------------
``DocumentRef.updated_at`` is set from the Datadog ``modified`` field on
config objects (monitors, dashboards, SLOs).  Events carry ``date_happened``
(epoch seconds) coerced to UTC.  Discovery is incremental: ``fetch()`` syncs
the events window ``[now - lookback_minutes, now]`` so re-runs catch only the
new state-change events without re-ingesting the entire history.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
)

__all__ = [
    "CANONICAL_DASHBOARD_NAME_REGEX",
    "CANONICAL_EVENT_NAME_REGEX",
    "CANONICAL_MONITOR_NAME_REGEX",
    "CANONICAL_SERVICE_NAME_REGEX",
    "CANONICAL_SLO_NAME_REGEX",
    "DatadogConfig",
    "DatadogConnector",
    "canonical_dashboard_name",
    "canonical_event_name",
    "canonical_host_name",
    "canonical_monitor_name",
    "canonical_pod_name",
    "canonical_service_name",
    "canonical_slo_name",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (named with rationale)
# ---------------------------------------------------------------------------

# Default Datadog API base — US1 region.  Override via ``DatadogConfig.site``;
# EU customers will use ``datadoghq.eu``.
_DEFAULT_SITE = "datadoghq.com"

# Page size for list endpoints that support it.  Datadog caps most list
# endpoints at 1000 but the safe portable max is 100 — same as GitHub.
_PAGE_SIZE = 100

# HTTP client timeout (seconds).  Generous for the events search endpoint
# which can be slow on wide windows.
_HTTP_TIMEOUT = 30.0

# Maximum sleep when rate-limited.  Defends against bogus reset headers.
_MAX_RATE_LIMIT_SLEEP_SECONDS = 60 * 60

# Maximum backoff retries on rate-limit.  After this many waits we surrender
# and re-raise to the caller.
_MAX_RATE_LIMIT_RETRIES = 3

# Org UID must look like a short slug — letters, digits, dot, hyphen.
# Mirrors Datadog org-id format (typically ``abcdef12``) but more permissive
# so we accept customer-friendly aliases too.
_ORG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

# Datadog tag form ``key:value``.  Used to extract ``service:`` / ``host:`` /
# ``pod_name:`` / ``kube_namespace:`` from monitor and event tag lists.
_TAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/=+\-@]*$")

# Datadog monitor query service references.  Matches both ``service:checkout``
# tags and the ``avg:trace.servlet.request.duration{service:checkout}`` form
# found in monitor queries.
_QUERY_SERVICE_RE = re.compile(r"service:([A-Za-z0-9_.\-/]+)")
_QUERY_HOST_RE = re.compile(r"host:([A-Za-z0-9_.\-/]+)")
_QUERY_POD_RE = re.compile(r"pod_name:([A-Za-z0-9_.\-/]+)")

# Canonical name regexes — anchored at both ends.  These are the hard
# contract that Slack/email mention extractors depend on.
CANONICAL_MONITOR_NAME_REGEX: re.Pattern[str] = re.compile(
    r"^datadog://monitor/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/(\d+)$"
)
CANONICAL_DASHBOARD_NAME_REGEX: re.Pattern[str] = re.compile(
    r"^datadog://dashboard/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/([A-Za-z0-9_-]+)$"
)
CANONICAL_SERVICE_NAME_REGEX: re.Pattern[str] = re.compile(
    r"^datadog://service/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/([A-Za-z0-9_.\-/]+)$"
)
CANONICAL_SLO_NAME_REGEX: re.Pattern[str] = re.compile(
    r"^datadog://slo/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/([A-Za-z0-9_-]+)$"
)
CANONICAL_EVENT_NAME_REGEX: re.Pattern[str] = re.compile(
    r"^datadog://event/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/(\d+)$"
)


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------


class DatadogConfig(BaseModel):
    """Public configuration for the Datadog connector (no secrets)."""

    org_uid: str
    """Short organisation identifier used to namespace canonical entity names.

    Datadog API keys are org-scoped, so this is a stable label rather than a
    runtime credential.  Must match ``_ORG_PATTERN``.
    """

    site: str = _DEFAULT_SITE
    """Datadog site (``datadoghq.com``, ``datadoghq.eu``, ``us3.datadoghq.com``...).

    Affects only the API base URL; canonical entity names are site-independent
    so ingestion of the same org from different sites stays idempotent.
    """

    include_monitors: bool = True
    """Enumerate monitors during ``discover()``.  Disable for org-id-only runs."""

    include_dashboards: bool = True
    """Enumerate dashboards during ``discover()``."""

    include_service_catalog: bool = True
    """Enumerate service catalog entries during ``discover()``."""

    include_slos: bool = True
    """Enumerate SLOs during ``discover()``."""

    events_lookback_minutes: int = 60
    """Events fetch window: ``[now - events_lookback_minutes, now]``.

    Default of 60 minutes matches typical ingestion cron intervals; operators
    that run the connector less frequently should raise this.  Re-runs are
    idempotent because event IDs are stable across the window overlap.
    """

    api_base_url: str | None = None
    """Optional explicit API root.  Defaults to ``https://api.{site}``; only
    override for testing — production stays on the site-derived base."""


# ---------------------------------------------------------------------------
# Canonical-name helpers (entity name conventions per the architect memo)
# ---------------------------------------------------------------------------


def canonical_monitor_name(org_uid: str, monitor_id: int | str) -> str:
    """Return the canonical monitor-entity name."""
    return f"datadog://monitor/{org_uid}/{monitor_id}"


def canonical_dashboard_name(org_uid: str, dashboard_id: str) -> str:
    """Return the canonical dashboard-entity name."""
    return f"datadog://dashboard/{org_uid}/{dashboard_id}"


def canonical_service_name(org_uid: str, service: str) -> str:
    """Return the canonical service-entity name (org-scoped)."""
    return f"datadog://service/{org_uid}/{service}"


def canonical_slo_name(org_uid: str, slo_id: str) -> str:
    """Return the canonical SLO-entity name."""
    return f"datadog://slo/{org_uid}/{slo_id}"


def canonical_event_name(org_uid: str, event_id: int | str) -> str:
    """Return the canonical event-entity name."""
    return f"datadog://event/{org_uid}/{event_id}"


def canonical_host_name(host: str) -> str:
    """Return the canonical host-entity name.

    Hosts are global (a hostname is unique within a Datadog org but also
    typically across orgs in a workspace) so we do not prefix with org_uid;
    this lets the same physical host alert across multiple monitors and still
    deduplicate in the graph.
    """
    return f"host://{host}"


def canonical_pod_name(pod: str, namespace: str | None = None) -> str:
    """Return the canonical pod-entity name compatible with the OTel emitter."""
    if namespace:
        return f"{namespace}/{pod}"
    return f"pod/{pod}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_org_uid(org_uid: str) -> str:
    """Validate the org_uid slug; returns it unchanged on success."""
    if not _ORG_PATTERN.match(org_uid):
        raise ValueError(f"Invalid org_uid {org_uid!r}; must match {_ORG_PATTERN.pattern}")
    return org_uid


def _build_auth_headers(secrets: dict[str, str]) -> dict[str, str]:
    """Build authorization headers from runtime secrets.

    Datadog uses two parallel header fields rather than Bearer auth so the
    Application key (read-scoped) can rotate independently of the API key
    (used for ingestion).
    """
    api_key = secrets.get("datadog_api_key", "").strip()
    app_key = secrets.get("datadog_app_key", "").strip()
    if not api_key:
        raise ValueError("Datadog connector requires 'datadog_api_key' in secrets.")
    if not app_key:
        raise ValueError(
            "Datadog connector requires 'datadog_app_key' in secrets "
            "(Application key with read scopes for monitors, dashboards, "
            "service catalog, SLOs, and events)."
        )
    return {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Accept": "application/json",
        "User-Agent": "omniscience-datadog-connector/0.2.0",
    }


def _api_base(cfg: DatadogConfig) -> str:
    """Resolve the API base URL from the config (explicit override wins)."""
    if cfg.api_base_url:
        return cfg.api_base_url.rstrip("/")
    return f"https://api.{cfg.site}"


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse a Datadog ISO-8601 timestamp; returns ``None`` on missing input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _coerce_modified(value: Any) -> datetime | None:
    """Coerce a Datadog ``modified`` value (str/int/None) into a UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        return _parse_iso8601(value)
    return None


def _entity_external_id(kind: str, org_uid: str, raw_id: str) -> str:
    """Stable ID for de-duplication: SHA-1 of ``kind:org:id``.

    SHA-1 is used as a *fingerprint*, not a security primitive.
    """
    fingerprint = f"{kind}:{org_uid}:{raw_id}"
    return hashlib.sha1(fingerprint.encode()).hexdigest()  # noqa: S324


def _tag_value(tags: list[str], key: str) -> str | None:
    """Return the first ``key:value`` tag value, or ``None``."""
    prefix = f"{key}:"
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(prefix):
            value = tag[len(prefix) :].strip()
            if value and _TAG_VALUE_RE.match(value):
                return value
    return None


def _extract_services_from_query(query: str) -> list[str]:
    """Pull all ``service:X`` references out of a Datadog monitor query."""
    return sorted({m.group(1) for m in _QUERY_SERVICE_RE.finditer(query or "")})


def _extract_hosts_from_query(query: str) -> list[str]:
    """Pull all ``host:X`` references out of a Datadog monitor query."""
    return sorted({m.group(1) for m in _QUERY_HOST_RE.finditer(query or "")})


def _extract_pods_from_query(query: str) -> list[str]:
    """Pull all ``pod_name:X`` references out of a Datadog monitor query."""
    return sorted({m.group(1) for m in _QUERY_POD_RE.finditer(query or "")})


async def _sleep_until_rate_limit_reset(response: httpx.Response) -> bool:
    """If the response indicates exhaustion, sleep until reset.

    Returns ``True`` when a sleep happened and the caller should retry,
    ``False`` when no rate-limit handling is needed.

    Datadog headers:
    * ``X-RateLimit-Remaining`` — requests left in current window
    * ``X-RateLimit-Reset`` — seconds (not epoch!) until window resets
    * ``X-RateLimit-Period`` — window length (informational)
    """
    remaining = response.headers.get("X-RateLimit-Remaining", "")
    if remaining != "0":
        return False
    reset_str = response.headers.get("X-RateLimit-Reset", "")
    try:
        sleep_for = float(reset_str)
    except (TypeError, ValueError):
        return False
    sleep_for = max(0.0, sleep_for)
    sleep_for = min(sleep_for, float(_MAX_RATE_LIMIT_SLEEP_SECONDS))
    logger.warning(
        "datadog.rate_limited",
        extra={"sleep_seconds": int(sleep_for)},
    )
    await asyncio.sleep(sleep_for)
    return True


async def _request_with_rate_limit(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Issue an HTTP request, transparently waiting on Datadog rate limits."""
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        response = await client.request(method, url, params=params)
        if response.status_code == 429:
            slept = await _sleep_until_rate_limit_reset(response)
            if slept and attempt < _MAX_RATE_LIMIT_RETRIES:
                continue
        return response
    return response


# ---------------------------------------------------------------------------
# Edge construction
# ---------------------------------------------------------------------------


def _monitor_edges(
    org_uid: str,
    monitor_id: int,
    services: list[str],
    hosts: list[str],
    pods: list[str],
) -> list[dict[str, str]]:
    """Build edges for one monitor: monitor -[:TARGETS]-> service/host/pod."""
    monitor_name = canonical_monitor_name(org_uid, monitor_id)
    edges: list[dict[str, str]] = []
    for svc in services:
        edges.append(
            {
                "from": monitor_name,
                "to": canonical_service_name(org_uid, svc),
                "type": "TARGETS",
            }
        )
    for host in hosts:
        edges.append(
            {
                "from": monitor_name,
                "to": canonical_host_name(host),
                "type": "TARGETS",
            }
        )
    for pod in pods:
        edges.append(
            {
                "from": monitor_name,
                "to": canonical_pod_name(pod),
                "type": "TARGETS",
            }
        )
    return edges


def _event_edges(
    org_uid: str,
    event_id: int,
    monitor_id: int | None,
) -> list[dict[str, str]]:
    """Build edges for one event: event -[:CORRELATES]-> monitor (if linked)."""
    if monitor_id is None:
        return []
    return [
        {
            "from": canonical_event_name(org_uid, event_id),
            "to": canonical_monitor_name(org_uid, monitor_id),
            "type": "CORRELATES",
        }
    ]


def _dashboard_edges(
    org_uid: str,
    dashboard_id: str,
    services: list[str],
) -> list[dict[str, str]]:
    """Build edges for one dashboard: dashboard -[:OBSERVES]-> service."""
    dash_name = canonical_dashboard_name(org_uid, dashboard_id)
    return [
        {
            "from": dash_name,
            "to": canonical_service_name(org_uid, svc),
            "type": "OBSERVES",
        }
        for svc in services
    ]


# ---------------------------------------------------------------------------
# Markdown rendering (one per entity kind for downstream chunking)
# ---------------------------------------------------------------------------


def _render_monitor_markdown(monitor: dict[str, Any]) -> str:
    """Render a Datadog monitor object as a Markdown document."""
    name = str(monitor.get("name", "")).strip() or "(unnamed monitor)"
    monitor_id = monitor.get("id", "")
    msg = str(monitor.get("message", "")).strip()
    query = str(monitor.get("query", "")).strip()
    monitor_type = str(monitor.get("type", "")).strip()
    overall_state = str(monitor.get("overall_state", "")).strip()
    tags = monitor.get("tags") or []
    creator = ""
    creator_field = monitor.get("creator")
    if isinstance(creator_field, dict):
        creator = str(creator_field.get("email") or creator_field.get("name") or "")

    lines: list[str] = [f"# Monitor #{monitor_id}: {name}", ""]
    if monitor_type:
        lines.append(f"- Type: `{monitor_type}`")
    if overall_state:
        lines.append(f"- State: `{overall_state}`")
    if creator:
        lines.append(f"- Creator: {creator}")
    if monitor.get("created"):
        lines.append(f"- Created: {monitor['created']}")
    if monitor.get("modified"):
        lines.append(f"- Modified: {monitor['modified']}")
    lines.append("")
    if query:
        lines.append("## Query")
        lines.append("")
        lines.append("```")
        lines.append(query)
        lines.append("```")
        lines.append("")
    if msg:
        lines.append("## Message")
        lines.append("")
        lines.append(msg)
        lines.append("")
    tag_strs = [str(t) for t in tags if isinstance(t, str)]
    if tag_strs:
        lines.append("## Tags")
        lines.append("")
        for tag in tag_strs:
            lines.append(f"- `{tag}`")
        lines.append("")
    return "\n".join(lines)


def _render_dashboard_markdown(dashboard: dict[str, Any]) -> str:
    """Render a Datadog dashboard object as Markdown (title + description + widgets)."""
    title = str(dashboard.get("title", "")).strip() or "(untitled dashboard)"
    dash_id = dashboard.get("id", "")
    description = str(dashboard.get("description", "") or "").strip()
    widgets = dashboard.get("widgets") or []

    lines: list[str] = [f"# Dashboard {dash_id}: {title}", ""]
    if dashboard.get("modified_at"):
        lines.append(f"- Modified: {dashboard['modified_at']}")
    if dashboard.get("author_handle"):
        lines.append(f"- Author: {dashboard['author_handle']}")
    lines.append("")
    if description:
        lines.append("## Description")
        lines.append("")
        lines.append(description)
        lines.append("")
    widget_titles: list[str] = []
    for widget in widgets:
        if isinstance(widget, dict):
            definition = widget.get("definition") or {}
            if isinstance(definition, dict):
                widget_title = str(definition.get("title", "")).strip()
                if widget_title:
                    widget_titles.append(widget_title)
    if widget_titles:
        lines.append("## Widgets")
        lines.append("")
        for wt in widget_titles:
            lines.append(f"- {wt}")
        lines.append("")
    return "\n".join(lines)


def _render_service_markdown(service: dict[str, Any]) -> str:
    """Render a service catalog entry as Markdown."""
    attrs = service.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    schema = attrs.get("schema") or {}
    if not isinstance(schema, dict):
        schema = {}
    name = (
        str(schema.get("dd-service") or attrs.get("name") or service.get("id") or "").strip()
        or "(unnamed service)"
    )
    description = str(schema.get("description") or "").strip()
    team = str(schema.get("team") or "").strip()
    tier = str(schema.get("tier") or "").strip()
    lifecycle = str(schema.get("lifecycle") or "").strip()
    contacts = schema.get("contacts") or []
    links = schema.get("links") or []

    lines: list[str] = [f"# Service: {name}", ""]
    if team:
        lines.append(f"- Team: {team}")
    if tier:
        lines.append(f"- Tier: {tier}")
    if lifecycle:
        lines.append(f"- Lifecycle: {lifecycle}")
    lines.append("")
    if description:
        lines.append("## Description")
        lines.append("")
        lines.append(description)
        lines.append("")
    if isinstance(contacts, list) and contacts:
        lines.append("## Contacts")
        lines.append("")
        for c in contacts:
            if isinstance(c, dict):
                contact_name = str(c.get("name") or "")
                contact_value = str(c.get("contact") or "")
                if contact_name or contact_value:
                    lines.append(f"- {contact_name}: {contact_value}")
        lines.append("")
    if isinstance(links, list) and links:
        lines.append("## Links")
        lines.append("")
        for link in links:
            if isinstance(link, dict):
                link_name = str(link.get("name") or "")
                link_url = str(link.get("url") or "")
                if link_url:
                    lines.append(f"- [{link_name or link_url}]({link_url})")
        lines.append("")
    return "\n".join(lines)


def _render_slo_markdown(slo: dict[str, Any]) -> str:
    """Render an SLO object as Markdown."""
    name = str(slo.get("name", "")).strip() or "(unnamed SLO)"
    slo_id = slo.get("id", "")
    description = str(slo.get("description", "") or "").strip()
    slo_type = str(slo.get("type", "")).strip()
    thresholds = slo.get("thresholds") or []
    tags = slo.get("tags") or []

    lines: list[str] = [f"# SLO {slo_id}: {name}", ""]
    if slo_type:
        lines.append(f"- Type: `{slo_type}`")
    if slo.get("modified_at"):
        lines.append(f"- Modified: {slo['modified_at']}")
    lines.append("")
    if description:
        lines.append("## Description")
        lines.append("")
        lines.append(description)
        lines.append("")
    if isinstance(thresholds, list) and thresholds:
        lines.append("## Thresholds")
        lines.append("")
        for th in thresholds:
            if isinstance(th, dict):
                timeframe = th.get("timeframe", "")
                target = th.get("target", "")
                warning = th.get("warning", "")
                lines.append(f"- timeframe={timeframe} target={target} warning={warning}")
        lines.append("")
    tag_strs = [str(t) for t in tags if isinstance(t, str)]
    if tag_strs:
        lines.append("## Tags")
        lines.append("")
        for tag in tag_strs:
            lines.append(f"- `{tag}`")
        lines.append("")
    return "\n".join(lines)


def _render_event_markdown(event: dict[str, Any]) -> str:
    """Render an event as Markdown (title + body + tags + linked monitor)."""
    title = str(event.get("title", "")).strip() or "(untitled event)"
    event_id = event.get("id", "")
    text = str(event.get("text", "") or "").strip()
    alert_type = str(event.get("alert_type", "") or "").strip()
    source = str(event.get("source", "") or "").strip()
    monitor_id = event.get("monitor_id")
    date_happened = event.get("date_happened")
    tags = event.get("tags") or []

    lines: list[str] = [f"# Event {event_id}: {title}", ""]
    if alert_type:
        lines.append(f"- Alert type: `{alert_type}`")
    if source:
        lines.append(f"- Source: {source}")
    if monitor_id is not None:
        lines.append(f"- Monitor: {monitor_id}")
    if date_happened is not None:
        lines.append(f"- Happened at (epoch): {date_happened}")
    lines.append("")
    if text:
        lines.append("## Body")
        lines.append("")
        lines.append(text)
        lines.append("")
    tag_strs = [str(t) for t in tags if isinstance(t, str)]
    if tag_strs:
        lines.append("## Tags")
        lines.append("")
        for tag in tag_strs:
            lines.append(f"- `{tag}`")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


_EntityKind = Literal["monitor", "dashboard", "service", "slo", "event"]


class DatadogConnector(Connector):
    """Connector for Datadog monitors, dashboards, service catalog, SLOs, events.

    Stateless: all state derived from config + secrets at call time.

    See module docstring for the auth model and canonical-name contract.
    """

    connector_type: ClassVar[str] = "datadog"
    config_schema: ClassVar[type[BaseModel]] = DatadogConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the API+App key pair works against the Datadog org.

        Calls the cheapest authenticated endpoint
        (``GET /api/v1/validate``) which exists explicitly for this purpose
        and counts against no functional quota.
        """
        cfg: DatadogConfig = config  # type: ignore[assignment]
        _validate_org_uid(cfg.org_uid)
        headers = _build_auth_headers(secrets)
        url = f"{_api_base(cfg)}/api/v1/validate"
        async with httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client:
            resp = await _request_with_rate_limit(client, "GET", url)
            if resp.status_code in (401, 403):
                raise PermissionError(
                    "Datadog authentication failed — check datadog_api_key / "
                    "datadog_app_key and required token scopes."
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"Datadog API validate failed: HTTP {resp.status_code}")

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate monitors, dashboards, service catalog entries, SLOs."""
        cfg: DatadogConfig = config  # type: ignore[assignment]
        _validate_org_uid(cfg.org_uid)
        headers = _build_auth_headers(secrets)
        async with httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client:
            if cfg.include_monitors:
                async for ref in self._discover_monitors(client, cfg):
                    yield ref
            if cfg.include_dashboards:
                async for ref in self._discover_dashboards(client, cfg):
                    yield ref
            if cfg.include_service_catalog:
                async for ref in self._discover_services(client, cfg):
                    yield ref
            if cfg.include_slos:
                async for ref in self._discover_slos(client, cfg):
                    yield ref

    # ----- discovery: monitors ---------------------------------------------

    async def _discover_monitors(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
    ) -> AsyncIterator[DocumentRef]:
        """Page through ``GET /api/v1/monitor`` and yield one ref per monitor."""
        url = f"{_api_base(cfg)}/api/v1/monitor"
        page = 0
        while True:
            params: dict[str, Any] = {
                "page": page,
                "page_size": _PAGE_SIZE,
            }
            resp = await _request_with_rate_limit(client, "GET", url, params=params)
            if resp.status_code >= 400:
                logger.warning(
                    "datadog.discover.monitors.api_error",
                    extra={"status": resp.status_code},
                )
                return
            items = resp.json()
            if not isinstance(items, list) or not items:
                return
            for item in items:
                ref = self._build_monitor_ref(cfg, item)
                if ref is not None:
                    yield ref
            if len(items) < _PAGE_SIZE:
                return
            page += 1

    def _build_monitor_ref(
        self, cfg: DatadogConfig, monitor: dict[str, Any]
    ) -> DocumentRef | None:
        """Translate a monitor JSON into a :class:`DocumentRef`."""
        monitor_id = monitor.get("id")
        if not isinstance(monitor_id, int):
            return None
        updated_at = _coerce_modified(monitor.get("modified"))
        uri = canonical_monitor_name(cfg.org_uid, monitor_id)
        tag_list = [t for t in (monitor.get("tags") or []) if isinstance(t, str)]
        service_from_tag = _tag_value(tag_list, "service")
        services_from_query = _extract_services_from_query(str(monitor.get("query", "")))
        all_services: list[str] = sorted(
            set(services_from_query) | ({service_from_tag} if service_from_tag else set())
        )
        return DocumentRef(
            external_id=_entity_external_id("monitor", cfg.org_uid, str(monitor_id)),
            uri=uri,
            updated_at=updated_at,
            metadata={
                "kind": "monitor",
                "name": uri,
                "org_uid": cfg.org_uid,
                "monitor_id": monitor_id,
                "monitor_type": monitor.get("type", ""),
                "overall_state": monitor.get("overall_state", ""),
                "title": monitor.get("name", ""),
                "services_hint": all_services,
            },
        )

    # ----- discovery: dashboards -------------------------------------------

    async def _discover_dashboards(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
    ) -> AsyncIterator[DocumentRef]:
        """Page through ``GET /api/v1/dashboard``."""
        url = f"{_api_base(cfg)}/api/v1/dashboard"
        start = 0
        while True:
            params: dict[str, Any] = {"start": start, "count": _PAGE_SIZE}
            resp = await _request_with_rate_limit(client, "GET", url, params=params)
            if resp.status_code >= 400:
                logger.warning(
                    "datadog.discover.dashboards.api_error",
                    extra={"status": resp.status_code},
                )
                return
            data = resp.json()
            if not isinstance(data, dict):
                return
            dashboards = data.get("dashboards")
            if not isinstance(dashboards, list) or not dashboards:
                return
            for item in dashboards:
                ref = self._build_dashboard_ref(cfg, item)
                if ref is not None:
                    yield ref
            if len(dashboards) < _PAGE_SIZE:
                return
            start += len(dashboards)

    def _build_dashboard_ref(self, cfg: DatadogConfig, dash: dict[str, Any]) -> DocumentRef | None:
        dash_id = dash.get("id")
        if not isinstance(dash_id, str) or not dash_id:
            return None
        updated_at = _coerce_modified(dash.get("modified_at"))
        uri = canonical_dashboard_name(cfg.org_uid, dash_id)
        return DocumentRef(
            external_id=_entity_external_id("dashboard", cfg.org_uid, dash_id),
            uri=uri,
            updated_at=updated_at,
            metadata={
                "kind": "dashboard",
                "name": uri,
                "org_uid": cfg.org_uid,
                "dashboard_id": dash_id,
                "title": dash.get("title", ""),
                "author_handle": dash.get("author_handle", ""),
            },
        )

    # ----- discovery: service catalog --------------------------------------

    async def _discover_services(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
    ) -> AsyncIterator[DocumentRef]:
        """Page through service catalog v2 (``GET /api/v2/services/definitions``)."""
        url = f"{_api_base(cfg)}/api/v2/services/definitions"
        page = 0
        while True:
            params: dict[str, Any] = {
                "page[number]": page,
                "page[size]": _PAGE_SIZE,
            }
            resp = await _request_with_rate_limit(client, "GET", url, params=params)
            if resp.status_code >= 400:
                logger.warning(
                    "datadog.discover.services.api_error",
                    extra={"status": resp.status_code},
                )
                return
            envelope = resp.json()
            if not isinstance(envelope, dict):
                return
            data = envelope.get("data")
            if not isinstance(data, list) or not data:
                return
            for item in data:
                ref = self._build_service_ref(cfg, item)
                if ref is not None:
                    yield ref
            if len(data) < _PAGE_SIZE:
                return
            page += 1

    def _build_service_ref(
        self, cfg: DatadogConfig, service: dict[str, Any]
    ) -> DocumentRef | None:
        attrs = service.get("attributes") or {}
        if not isinstance(attrs, dict):
            return None
        schema = attrs.get("schema") or {}
        if not isinstance(schema, dict):
            return None
        svc_name = schema.get("dd-service") or attrs.get("name") or service.get("id")
        if not isinstance(svc_name, str) or not svc_name:
            return None
        updated_at = _coerce_modified(attrs.get("modified") or attrs.get("modified_at"))
        uri = canonical_service_name(cfg.org_uid, svc_name)
        return DocumentRef(
            external_id=_entity_external_id("service", cfg.org_uid, svc_name),
            uri=uri,
            updated_at=updated_at,
            metadata={
                "kind": "service",
                "name": uri,
                "org_uid": cfg.org_uid,
                "service": svc_name,
                "team": schema.get("team", ""),
                "tier": schema.get("tier", ""),
                "lifecycle": schema.get("lifecycle", ""),
            },
        )

    # ----- discovery: SLOs --------------------------------------------------

    async def _discover_slos(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
    ) -> AsyncIterator[DocumentRef]:
        """Page through ``GET /api/v1/slo``."""
        url = f"{_api_base(cfg)}/api/v1/slo"
        offset = 0
        while True:
            params: dict[str, Any] = {"offset": offset, "limit": _PAGE_SIZE}
            resp = await _request_with_rate_limit(client, "GET", url, params=params)
            if resp.status_code >= 400:
                logger.warning(
                    "datadog.discover.slos.api_error",
                    extra={"status": resp.status_code},
                )
                return
            envelope = resp.json()
            if not isinstance(envelope, dict):
                return
            data = envelope.get("data")
            if not isinstance(data, list) or not data:
                return
            for item in data:
                ref = self._build_slo_ref(cfg, item)
                if ref is not None:
                    yield ref
            if len(data) < _PAGE_SIZE:
                return
            offset += len(data)

    def _build_slo_ref(self, cfg: DatadogConfig, slo: dict[str, Any]) -> DocumentRef | None:
        slo_id = slo.get("id")
        if not isinstance(slo_id, str) or not slo_id:
            return None
        updated_at = _coerce_modified(slo.get("modified_at") or slo.get("modified"))
        uri = canonical_slo_name(cfg.org_uid, slo_id)
        return DocumentRef(
            external_id=_entity_external_id("slo", cfg.org_uid, slo_id),
            uri=uri,
            updated_at=updated_at,
            metadata={
                "kind": "slo",
                "name": uri,
                "org_uid": cfg.org_uid,
                "slo_id": slo_id,
                "slo_type": slo.get("type", ""),
                "title": slo.get("name", ""),
            },
        )

    # ----- fetch -----------------------------------------------------------

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch one entity by ref kind.

        For ``kind == "monitor"``, this also performs an *events fetch* over
        the configured lookback window and emits a synthetic edge list that
        links event IDs to the monitor.  Real per-event :class:`DocumentRef`
        objects are emitted by :meth:`fetch_events` which the ingestion
        pipeline calls explicitly during a sync run.
        """
        cfg: DatadogConfig = config  # type: ignore[assignment]
        kind = str(ref.metadata.get("kind", ""))
        headers = _build_auth_headers(secrets)
        async with httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client:
            if kind == "monitor":
                return await self._fetch_monitor(client, cfg, ref)
            if kind == "dashboard":
                return await self._fetch_dashboard(client, cfg, ref)
            if kind == "service":
                return await self._fetch_service(client, cfg, ref)
            if kind == "slo":
                return await self._fetch_slo(client, cfg, ref)
            if kind == "event":
                return await self._fetch_event(client, cfg, ref)
        raise ValueError(f"DocumentRef has unknown kind: {kind!r} (uri={ref.uri})")

    async def _fetch_monitor(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
        ref: DocumentRef,
    ) -> FetchedDocument:
        monitor_id = ref.metadata.get("monitor_id")
        if not isinstance(monitor_id, int):
            raise ValueError(f"Monitor ref missing monitor_id: {ref.uri}")
        url = f"{_api_base(cfg)}/api/v1/monitor/{monitor_id}"
        resp = await _request_with_rate_limit(client, "GET", url)
        monitor: dict[str, Any] = {}
        if resp.status_code < 400:
            payload = resp.json()
            if isinstance(payload, dict):
                monitor = payload

        query = str(monitor.get("query", ""))
        tag_list = [t for t in (monitor.get("tags") or []) if isinstance(t, str)]

        def _merged(query_values: list[str], tag_key: str) -> list[str]:
            seen: set[str] = set(query_values)
            from_tag = _tag_value(tag_list, tag_key)
            if from_tag:
                seen.add(from_tag)
            return sorted(seen)

        services = _merged(_extract_services_from_query(query), "service")
        hosts = _merged(_extract_hosts_from_query(query), "host")
        pods = _merged(_extract_pods_from_query(query), "pod_name")

        markdown = _render_monitor_markdown(monitor)
        edges = _monitor_edges(cfg.org_uid, monitor_id, services, hosts, pods)

        enriched_metadata: dict[str, Any] = {
            **ref.metadata,
            "monitor_url": ref.uri,
            "services": services,
            "hosts": hosts,
            "pods": pods,
            "edges": edges,
        }
        return FetchedDocument(
            ref=DocumentRef(
                external_id=ref.external_id,
                uri=ref.uri,
                updated_at=ref.updated_at,
                metadata=enriched_metadata,
            ),
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    async def _fetch_dashboard(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
        ref: DocumentRef,
    ) -> FetchedDocument:
        dash_id = ref.metadata.get("dashboard_id")
        if not isinstance(dash_id, str) or not dash_id:
            raise ValueError(f"Dashboard ref missing dashboard_id: {ref.uri}")
        url = f"{_api_base(cfg)}/api/v1/dashboard/{dash_id}"
        resp = await _request_with_rate_limit(client, "GET", url)
        dashboard: dict[str, Any] = {}
        if resp.status_code < 400:
            payload = resp.json()
            if isinstance(payload, dict):
                dashboard = payload

        # Collect ``service:X`` references from any template variable or widget
        # ``query`` field — a cheap-but-useful proxy for dashboard observation
        # targets.  We don't recurse into nested widget definitions deeply.
        services_seen: set[str] = set()
        for widget in dashboard.get("widgets") or []:
            if isinstance(widget, dict):
                definition = widget.get("definition") or {}
                if isinstance(definition, dict):
                    for req in definition.get("requests") or []:
                        if isinstance(req, dict):
                            q = str(req.get("q", ""))
                            services_seen.update(_extract_services_from_query(q))
        for tv in dashboard.get("template_variables") or []:
            if isinstance(tv, dict) and str(tv.get("name", "")).lower() == "service":
                default = tv.get("default")
                if isinstance(default, str) and default and default != "*":
                    services_seen.add(default)

        services = sorted(services_seen)
        markdown = _render_dashboard_markdown(dashboard)
        edges = _dashboard_edges(cfg.org_uid, dash_id, services)
        enriched_metadata: dict[str, Any] = {
            **ref.metadata,
            "dashboard_url": ref.uri,
            "services": services,
            "edges": edges,
        }
        return FetchedDocument(
            ref=DocumentRef(
                external_id=ref.external_id,
                uri=ref.uri,
                updated_at=ref.updated_at,
                metadata=enriched_metadata,
            ),
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    async def _fetch_service(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
        ref: DocumentRef,
    ) -> FetchedDocument:
        service_name = ref.metadata.get("service")
        if not isinstance(service_name, str) or not service_name:
            raise ValueError(f"Service ref missing service name: {ref.uri}")
        url = f"{_api_base(cfg)}/api/v2/services/definitions/{service_name}"
        resp = await _request_with_rate_limit(client, "GET", url)
        envelope: dict[str, Any] = {}
        if resp.status_code < 400:
            payload = resp.json()
            if isinstance(payload, dict):
                envelope = payload
        service_data = envelope.get("data")
        if not isinstance(service_data, dict):
            service_data = {}
        markdown = _render_service_markdown(service_data)
        return FetchedDocument(
            ref=DocumentRef(
                external_id=ref.external_id,
                uri=ref.uri,
                updated_at=ref.updated_at,
                metadata={**ref.metadata, "service_url": ref.uri},
            ),
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    async def _fetch_slo(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
        ref: DocumentRef,
    ) -> FetchedDocument:
        slo_id = ref.metadata.get("slo_id")
        if not isinstance(slo_id, str) or not slo_id:
            raise ValueError(f"SLO ref missing slo_id: {ref.uri}")
        url = f"{_api_base(cfg)}/api/v1/slo/{slo_id}"
        resp = await _request_with_rate_limit(client, "GET", url)
        envelope: dict[str, Any] = {}
        if resp.status_code < 400:
            payload = resp.json()
            if isinstance(payload, dict):
                envelope = payload
        slo_data = envelope.get("data")
        if not isinstance(slo_data, dict):
            slo_data = {}
        markdown = _render_slo_markdown(slo_data)
        return FetchedDocument(
            ref=DocumentRef(
                external_id=ref.external_id,
                uri=ref.uri,
                updated_at=ref.updated_at,
                metadata={**ref.metadata, "slo_url": ref.uri},
            ),
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    async def _fetch_event(
        self,
        client: httpx.AsyncClient,
        cfg: DatadogConfig,
        ref: DocumentRef,
    ) -> FetchedDocument:
        event_id = ref.metadata.get("event_id")
        if not isinstance(event_id, int):
            raise ValueError(f"Event ref missing event_id: {ref.uri}")
        url = f"{_api_base(cfg)}/api/v1/events/{event_id}"
        resp = await _request_with_rate_limit(client, "GET", url)
        envelope: dict[str, Any] = {}
        if resp.status_code < 400:
            payload = resp.json()
            if isinstance(payload, dict):
                envelope = payload
        event_data = envelope.get("event")
        if not isinstance(event_data, dict):
            event_data = {}
        monitor_id_raw = event_data.get("monitor_id")
        monitor_id = monitor_id_raw if isinstance(monitor_id_raw, int) else None
        markdown = _render_event_markdown(event_data)
        edges = _event_edges(cfg.org_uid, event_id, monitor_id)
        return FetchedDocument(
            ref=DocumentRef(
                external_id=ref.external_id,
                uri=ref.uri,
                updated_at=ref.updated_at,
                metadata={
                    **ref.metadata,
                    "event_url": ref.uri,
                    "monitor_id": monitor_id,
                    "edges": edges,
                },
            ),
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    # ----- events bitemporal sync ------------------------------------------

    async def fetch_events(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        *,
        now: datetime | None = None,
    ) -> AsyncIterator[DocumentRef]:
        """Sync events (audit + alert state changes) in the lookback window.

        This is the bitemporal entry point — caller invokes this once per
        sync tick and consumes the resulting refs.  Each event ref carries a
        canonical ``datadog://event/...`` URI plus the linked monitor ID (if
        any) in its metadata so the entity linker can emit the
        ``[:CORRELATES]`` edge without re-fetching.

        ``now`` is injectable purely so the deterministic VCR cassette tests
        can lock the window; production callers always pass ``None``.
        """
        cfg: DatadogConfig = config  # type: ignore[assignment]
        _validate_org_uid(cfg.org_uid)
        headers = _build_auth_headers(secrets)
        end = now or datetime.now(tz=UTC)
        start = end - timedelta(minutes=cfg.events_lookback_minutes)
        url = f"{_api_base(cfg)}/api/v1/events"
        async with httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client:
            params: dict[str, Any] = {
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
            }
            resp = await _request_with_rate_limit(client, "GET", url, params=params)
            if resp.status_code >= 400:
                logger.warning(
                    "datadog.fetch_events.api_error",
                    extra={"status": resp.status_code},
                )
                return
            envelope = resp.json()
            if not isinstance(envelope, dict):
                return
            events = envelope.get("events")
            if not isinstance(events, list):
                return
            for event in events:
                if isinstance(event, dict):
                    ref = self._build_event_ref(cfg, event)
                    if ref is not None:
                        yield ref

    def _build_event_ref(self, cfg: DatadogConfig, event: dict[str, Any]) -> DocumentRef | None:
        event_id = event.get("id")
        if not isinstance(event_id, int):
            return None
        date_happened = event.get("date_happened")
        updated_at: datetime | None = None
        if isinstance(date_happened, (int, float)):
            updated_at = datetime.fromtimestamp(float(date_happened), tz=UTC)
        monitor_id_raw = event.get("monitor_id")
        monitor_id = monitor_id_raw if isinstance(monitor_id_raw, int) else None
        uri = canonical_event_name(cfg.org_uid, event_id)
        return DocumentRef(
            external_id=_entity_external_id("event", cfg.org_uid, str(event_id)),
            uri=uri,
            updated_at=updated_at,
            metadata={
                "kind": "event",
                "name": uri,
                "org_uid": cfg.org_uid,
                "event_id": event_id,
                "monitor_id": monitor_id,
                "alert_type": event.get("alert_type", ""),
                "source": event.get("source", ""),
                "title": event.get("title", ""),
                "date_happened": date_happened,
            },
        )

    def webhook_handler(self) -> WebhookHandler | None:
        """Datadog alert webhooks are owned by the alerts connector.

        Returning ``None`` here keeps the API-pull and the webhook-push paths
        cleanly separated.  See ``packages/connectors/.../alerts/`` for the
        push-side handler.
        """
        return None
