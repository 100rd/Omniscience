"""PagerDuty full source connector (issue #239).

Promotes the existing push-only :mod:`omniscience_connectors.alerts` PagerDuty
webhook receiver into a fully fledged source connector that *also* pulls the
PagerDuty catalog and incident timeline via the REST API.

Scope
-----
Read-only in v1:

* ``discover()`` enumerates services, teams, escalation policies and on-call
  shifts.  Each returns one :class:`DocumentRef` per entity with the canonical
  PagerDuty entity ID.
* ``fetch()`` performs a bitemporal incident sync — pulls the incident
  ``state`` plus the full ``log_entries`` timeline so downstream
  ``resolve_incident`` can replay history at any point in wall time.
* ``webhook_handler()`` delegates to the existing alerts/pagerduty receiver
  so we keep one set of signature-verification code.

Bi-directional write-back (acks/snooze/resolve) is deferred to v2 Action Mode
— see issue #230.  The placeholders in this module raise
:class:`NotImplementedError` with a clear pointer so callers fail loudly until
the action plane lands.

Edges emitted (via ``metadata['edges']``)
-----------------------------------------
* ``incident -> service`` (``FILED_AGAINST``)
* ``service  -> team``    (``OWNED_BY``)
* ``service  -> escalation`` (``USES_ESCALATION``)
* ``escalation -> team``  (``OWNED_BY``)
* ``on-call shift -> user`` (``ON_CALL``)

ACL invariant (P0)
------------------
We never read ``tenant_id``, ``customer_id``, ``account_id`` or any other
field from the PagerDuty payload for tenancy.  The ingest pipeline sets
``workspace_id`` from the :class:`Source` row that owns the connector.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from omniscience_connectors.alerts.webhook import AlertsWebhookHandler
from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
)
from omniscience_connectors.pagerduty.models import (
    IncidentLogEntry,
    OnCallShift,
    PagerDutyEscalation,
    PagerDutyIncident,
    PagerDutyService,
    PagerDutyTeam,
)

__all__ = [
    "PagerDutyConfig",
    "PagerDutyConnector",
    "canonical_escalation_name",
    "canonical_incident_name",
    "canonical_oncall_shift_name",
    "canonical_service_name",
    "canonical_team_name",
    "canonical_user_name",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_API_BASE = "https://api.pagerduty.com"
_PAGE_SIZE = 100
"""PagerDuty REST list-endpoint maximum page size."""

_MAX_RATE_LIMIT_RETRIES = 5
"""Maximum 429-retry attempts before surrendering to the caller."""

_MAX_RATE_LIMIT_SLEEP_SECONDS = 60
"""Upper bound on a single ``Retry-After`` sleep to avoid hangs from bogus headers."""

_DEFAULT_INCIDENT_LOOKBACK_DAYS = 30
"""How many days of incidents ``discover()`` enumerates by default."""

_USER_AGENT = "omniscience-pagerduty-connector/0.2.0"


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------


class PagerDutyConfig(BaseModel):
    """Public configuration for the PagerDuty connector (no secrets).

    The API token itself is supplied at call-time via ``secrets['api_token']``
    and is NEVER read from this model.  ``webhook_secret`` likewise lives in
    secrets and is consumed by the inherited alerts/pagerduty receiver.
    """

    api_base_url: str = _DEFAULT_API_BASE
    """Override only for PagerDuty EU (``https://api.eu.pagerduty.com``) or
    integration tests with a recorded fixture host."""

    team_ids: list[str] = Field(default_factory=list)
    """Optional filter — when non-empty, the connector restricts service/
    escalation/schedule enumeration to these team IDs.  Empty = all teams
    accessible to the API token."""

    service_ids: list[str] = Field(default_factory=list)
    """Optional filter for incident sync — when non-empty, only incidents
    against these services are fetched."""

    incident_lookback_days: int = _DEFAULT_INCIDENT_LOOKBACK_DAYS
    """How many days of incident history to enumerate on each discover run.
    Bitemporal log entries are always fetched in full for any incident yielded."""

    include_resolved_incidents: bool = True
    """When ``False``, ``discover()`` skips incidents in ``resolved`` state."""


# ---------------------------------------------------------------------------
# Canonical-name helpers (drive entity linking downstream)
# ---------------------------------------------------------------------------


def canonical_service_name(service_id: str) -> str:
    """Return the canonical name for a PagerDuty service."""
    return f"pagerduty://service/{service_id}"


def canonical_team_name(team_id: str) -> str:
    return f"pagerduty://team/{team_id}"


def canonical_escalation_name(policy_id: str) -> str:
    return f"pagerduty://escalation/{policy_id}"


def canonical_oncall_shift_name(schedule_id: str, user_id: str, start: datetime) -> str:
    """Stable name for an on-call shift block.

    Schedules emit overlapping shifts across re-fetches; keying the canonical
    name on ``(schedule, user, start)`` keeps deduplication idempotent.
    """
    return f"pagerduty://oncall/{schedule_id}/{user_id}/{int(start.timestamp())}"


def canonical_user_name(user_id: str) -> str:
    return f"pagerduty://user/{user_id}"


def canonical_incident_name(incident_id: str) -> str:
    return f"pagerduty://incident/{incident_id}"


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------


def _build_auth_headers(secrets: Mapping[str, str]) -> dict[str, str]:
    """Build PagerDuty REST API headers (Token auth + JSON-only).

    PagerDuty accepts a REST API key in the ``Authorization`` header as
    ``Token token=<key>``.  We require an account-level read-only token; the
    required scopes are documented in ``docs/connectors/pagerduty.md``.
    """
    token = (secrets.get("api_token") or "").strip()
    if not token:
        raise ValueError(
            "PagerDuty secrets must contain 'api_token' "
            "(account-level REST API key — see docs/connectors/pagerduty.md)."
        )
    return {
        "Authorization": f"Token token={token}",
        "Accept": "application/vnd.pagerduty+json;version=2",
        "User-Agent": _USER_AGENT,
    }


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse a PagerDuty ISO-8601 timestamp; ``None`` on missing/invalid input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def _sleep_until_rate_limit_reset(response: httpx.Response) -> bool:
    """If PagerDuty returned 429, honour ``Retry-After`` and report a retry.

    PagerDuty's REST API returns HTTP 429 with a ``Retry-After`` header (whole
    seconds) when an API key exceeds 960 req/min (the documented hard cap).
    Returns ``True`` when a sleep happened and the caller should retry,
    ``False`` otherwise.  Mirrors the github_pr connector pattern.
    """
    if response.status_code != 429:
        return False
    retry_after = response.headers.get("Retry-After", "")
    try:
        sleep_for = float(retry_after) if retry_after else 1.0
    except ValueError:
        sleep_for = 1.0
    sleep_for = max(0.0, min(sleep_for, float(_MAX_RATE_LIMIT_SLEEP_SECONDS)))
    logger.warning(
        "pagerduty.rate_limited",
        extra={"sleep_seconds": int(sleep_for), "retry_after_header": retry_after},
    )
    await asyncio.sleep(sleep_for)
    return True


async def _request_with_rate_limit(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """Issue a request, transparently waiting on PagerDuty 429s."""
    response: httpx.Response | None = None
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        response = await client.request(method, url, params=dict(params or {}))
        if response.status_code == 429:
            slept = await _sleep_until_rate_limit_reset(response)
            if slept and attempt < _MAX_RATE_LIMIT_RETRIES:
                continue
        return response
    assert response is not None  # noqa: S101 — loop invariant
    return response


async def _paginate(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Paginate a PagerDuty list endpoint using ``offset`` + ``more``.

    PagerDuty's list endpoints return ``{"<resource>": [...], "more": bool,
    "offset": int, "limit": int}``; iterate until ``more`` is false.
    """
    offset = 0
    while True:
        query: dict[str, Any] = dict(params or {})
        query["offset"] = offset
        query["limit"] = _PAGE_SIZE
        response = await _request_with_rate_limit(client, "GET", url, params=query)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            return
        yield body
        more = bool(body.get("more"))
        if not more:
            return
        offset += int(body.get("limit") or _PAGE_SIZE)


# ---------------------------------------------------------------------------
# JSON shape -> model adapters
# ---------------------------------------------------------------------------


def _team_from_json(payload: dict[str, Any]) -> PagerDutyTeam | None:
    pid = str(payload.get("id") or "")
    if not pid:
        return None
    return PagerDutyTeam(
        id=pid,
        name=str(payload.get("name") or pid),
        summary=str(payload["summary"]) if payload.get("summary") else None,
        html_url=str(payload["html_url"]) if payload.get("html_url") else None,
    )


def _service_from_json(payload: dict[str, Any]) -> PagerDutyService | None:
    sid = str(payload.get("id") or "")
    if not sid:
        return None
    teams = payload.get("teams") or []
    team_ids = [str(t.get("id")) for t in teams if isinstance(t, dict) and t.get("id")]
    esc_obj = payload.get("escalation_policy") or {}
    esc_id = str(esc_obj.get("id")) if isinstance(esc_obj, dict) and esc_obj.get("id") else None
    return PagerDutyService(
        id=sid,
        name=str(payload.get("name") or sid),
        status=str(payload["status"]) if payload.get("status") else None,
        description=(str(payload["description"]) if payload.get("description") else None),
        team_ids=team_ids,
        escalation_policy_id=esc_id,
        html_url=str(payload["html_url"]) if payload.get("html_url") else None,
    )


def _escalation_from_json(payload: dict[str, Any]) -> PagerDutyEscalation | None:
    pid = str(payload.get("id") or "")
    if not pid:
        return None
    teams = payload.get("teams") or []
    team_ids = [str(t.get("id")) for t in teams if isinstance(t, dict) and t.get("id")]
    rules = payload.get("escalation_rules") or []
    return PagerDutyEscalation(
        id=pid,
        name=str(payload.get("name") or pid),
        summary=str(payload["summary"]) if payload.get("summary") else None,
        description=(str(payload["description"]) if payload.get("description") else None),
        team_ids=team_ids,
        num_loops=int(payload.get("num_loops") or 0),
        rule_count=len(rules) if isinstance(rules, list) else 0,
        html_url=str(payload["html_url"]) if payload.get("html_url") else None,
    )


def _log_entry_from_json(payload: dict[str, Any]) -> IncidentLogEntry | None:
    lid = str(payload.get("id") or "")
    if not lid:
        return None
    created_at = _parse_iso8601(str(payload.get("created_at") or "")) or datetime.now(tz=UTC)
    agent = payload.get("agent") or {}
    channel = payload.get("channel") or {}
    return IncidentLogEntry(
        id=lid,
        type=str(payload.get("type") or "unknown_log_entry"),
        created_at=created_at,
        summary=str(payload["summary"]) if payload.get("summary") else None,
        agent_type=(
            str(agent.get("type")) if isinstance(agent, dict) and agent.get("type") else None
        ),
        agent_id=(str(agent.get("id")) if isinstance(agent, dict) and agent.get("id") else None),
        channel_type=(
            str(channel.get("type")) if isinstance(channel, dict) and channel.get("type") else None
        ),
        raw=payload,
    )


def _incident_from_json(
    payload: dict[str, Any],
    log_entries_payload: list[dict[str, Any]] | None = None,
) -> PagerDutyIncident | None:
    iid = str(payload.get("id") or "")
    if not iid:
        return None
    service = payload.get("service") or {}
    service_id = str(service.get("id")) if isinstance(service, dict) and service.get("id") else ""
    if not service_id:
        return None

    esc = payload.get("escalation_policy") or {}
    esc_id = str(esc.get("id")) if isinstance(esc, dict) and esc.get("id") else None

    status_raw = str(payload.get("status") or "triggered").lower()
    status: Literal["triggered", "acknowledged", "resolved"] = (
        "triggered" if status_raw not in ("acknowledged", "resolved") else status_raw  # type: ignore[assignment]
    )

    urgency_raw = str(payload.get("urgency") or "").lower() or None
    urgency: Literal["high", "low"] | None = (
        urgency_raw if urgency_raw in ("high", "low") else None  # type: ignore[assignment]
    )

    assignments = payload.get("assignments") or []
    assignee_ids = [
        str((a.get("assignee") or {}).get("id"))
        for a in assignments
        if isinstance(a, dict)
        and isinstance(a.get("assignee"), dict)
        and (a.get("assignee") or {}).get("id")
    ]
    acks = payload.get("acknowledgements") or []
    ack_ids = [
        str((a.get("acknowledger") or {}).get("id"))
        for a in acks
        if isinstance(a, dict)
        and isinstance(a.get("acknowledger"), dict)
        and (a.get("acknowledger") or {}).get("id")
    ]

    created_at = _parse_iso8601(str(payload.get("created_at") or "")) or datetime.now(tz=UTC)

    log_models: list[IncidentLogEntry] = []
    for raw_entry in log_entries_payload or []:
        entry = _log_entry_from_json(raw_entry)
        if entry is not None:
            log_models.append(entry)

    return PagerDutyIncident(
        id=iid,
        incident_number=int(payload.get("incident_number") or 0),
        title=str(payload.get("title") or payload.get("summary") or ""),
        status=status,
        urgency=urgency,
        created_at=created_at,
        updated_at=_parse_iso8601(str(payload.get("last_status_change_at") or "")),
        resolved_at=_parse_iso8601(str(payload.get("resolved_at") or "")),
        service_id=service_id,
        escalation_policy_id=esc_id,
        assignee_user_ids=assignee_ids,
        acknowledger_user_ids=ack_ids,
        log_entries=log_models,
        html_url=str(payload["html_url"]) if payload.get("html_url") else None,
    )


# ---------------------------------------------------------------------------
# DocumentRef builders
# ---------------------------------------------------------------------------


def _ref_for_team(team: PagerDutyTeam) -> DocumentRef:
    name = canonical_team_name(team.id)
    return DocumentRef(
        external_id=name,
        uri=team.html_url or name,
        metadata={
            "kind": "PagerDutyTeam",
            "name": name,
            "pagerduty_team_id": team.id,
            "display_name": team.name,
            "summary": team.summary,
        },
    )


def _ref_for_service(service: PagerDutyService) -> DocumentRef:
    name = canonical_service_name(service.id)
    edges: list[dict[str, str]] = []
    for tid in service.team_ids:
        edges.append({"type": "OWNED_BY", "from": name, "to": canonical_team_name(tid)})
    if service.escalation_policy_id:
        edges.append(
            {
                "type": "USES_ESCALATION",
                "from": name,
                "to": canonical_escalation_name(service.escalation_policy_id),
            }
        )
    return DocumentRef(
        external_id=name,
        uri=service.html_url or name,
        metadata={
            "kind": "PagerDutyService",
            "name": name,
            "pagerduty_service_id": service.id,
            "display_name": service.name,
            "status": service.status,
            "description": service.description,
            "team_ids": service.team_ids,
            "escalation_policy_id": service.escalation_policy_id,
            "edges": edges,
        },
    )


def _ref_for_escalation(escalation: PagerDutyEscalation) -> DocumentRef:
    name = canonical_escalation_name(escalation.id)
    edges: list[dict[str, str]] = [
        {"type": "OWNED_BY", "from": name, "to": canonical_team_name(tid)}
        for tid in escalation.team_ids
    ]
    return DocumentRef(
        external_id=name,
        uri=escalation.html_url or name,
        metadata={
            "kind": "PagerDutyEscalation",
            "name": name,
            "pagerduty_escalation_id": escalation.id,
            "display_name": escalation.name,
            "summary": escalation.summary,
            "description": escalation.description,
            "team_ids": escalation.team_ids,
            "num_loops": escalation.num_loops,
            "rule_count": escalation.rule_count,
            "edges": edges,
        },
    )


def _ref_for_oncall_shift(shift: OnCallShift) -> DocumentRef:
    name = canonical_oncall_shift_name(shift.schedule_id, shift.user_id, shift.start)
    edges: list[dict[str, str]] = [
        {"type": "ON_CALL", "from": name, "to": canonical_user_name(shift.user_id)},
    ]
    if shift.escalation_policy_id:
        edges.append(
            {
                "type": "FOR_ESCALATION",
                "from": name,
                "to": canonical_escalation_name(shift.escalation_policy_id),
            }
        )
    return DocumentRef(
        external_id=name,
        uri=shift.html_url or name,
        updated_at=shift.start,
        metadata={
            "kind": "OnCallShift",
            "name": name,
            "schedule_id": shift.schedule_id,
            "schedule_name": shift.schedule_name,
            "user_id": shift.user_id,
            "user_name": shift.user_name,
            "user_email": shift.user_email,
            "escalation_policy_id": shift.escalation_policy_id,
            "escalation_level": shift.escalation_level,
            "start": shift.start.isoformat(),
            "end": shift.end.isoformat(),
            "edges": edges,
        },
    )


def _ref_for_incident(incident: PagerDutyIncident) -> DocumentRef:
    name = canonical_incident_name(incident.id)
    # Stable fingerprint changes whenever the timeline grows or status changes,
    # so the downstream pipeline triggers a re-ingest only when needed.
    fingerprint_src = (
        f"{incident.id}:{incident.status}:{len(incident.log_entries)}:"
        f"{incident.updated_at.isoformat() if incident.updated_at else ''}"
    )
    external_id = hashlib.sha1(fingerprint_src.encode()).hexdigest()  # noqa: S324
    return DocumentRef(
        external_id=external_id,
        uri=incident.html_url or name,
        updated_at=incident.updated_at or incident.created_at,
        metadata={
            "kind": "PagerDutyIncident",
            "name": name,
            "pagerduty_incident_id": incident.id,
            "incident_number": incident.incident_number,
            "title": incident.title,
            "status": incident.status,
            "urgency": incident.urgency,
            "service_id": incident.service_id,
            "escalation_policy_id": incident.escalation_policy_id,
            "created_at": incident.created_at.isoformat(),
            "updated_at": (incident.updated_at.isoformat() if incident.updated_at else None),
            "resolved_at": (incident.resolved_at.isoformat() if incident.resolved_at else None),
            "assignee_user_ids": incident.assignee_user_ids,
            "acknowledger_user_ids": incident.acknowledger_user_ids,
        },
    )


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class PagerDutyConnector(Connector):
    """Full PagerDuty source connector.

    Pull-style discovery for the catalog (teams, services, escalation
    policies, on-call schedules) and bitemporal incident sync.  Webhook
    handling is delegated unchanged to the existing alerts/pagerduty
    receiver — see :meth:`webhook_handler`.

    Bi-directional write-back (acks, snooze, resolve) is **deferred to v2
    Action Mode** — tracking issue #230.  The :meth:`acknowledge` /
    :meth:`snooze` / :meth:`resolve` placeholders raise
    :class:`NotImplementedError` until the action plane lands.
    """

    connector_type: ClassVar[str] = "pagerduty"
    config_schema: ClassVar[type[BaseModel]] = PagerDutyConfig

    # ---- lifecycle --------------------------------------------------------

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the token by calling the cheapest authenticated endpoint."""
        cfg: PagerDutyConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.api_base_url.rstrip("/")
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await _request_with_rate_limit(
                client,
                "GET",
                f"{base}/abilities",
            )
            if resp.status_code == 401:
                raise PermissionError(
                    "PagerDuty authentication failed — invalid 'api_token' secret."
                )
            if resp.status_code == 403:
                raise PermissionError(
                    "PagerDuty token lacks required scopes — see docs/connectors/pagerduty.md."
                )
            resp.raise_for_status()

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield catalog entities and recent incidents as :class:`DocumentRef`s.

        Order: teams -> escalation policies -> services -> on-call shifts ->
        incidents.  Earlier entities are emitted first so the downstream
        pipeline can resolve cross-edges (services reference teams /
        escalation policies; incidents reference services) at link time.
        """
        cfg: PagerDutyConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.api_base_url.rstrip("/")

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            async for ref in self._discover_teams(client, base, cfg):
                yield ref
            async for ref in self._discover_escalations(client, base, cfg):
                yield ref
            async for ref in self._discover_services(client, base, cfg):
                yield ref
            async for ref in self._discover_oncall_shifts(client, base, cfg):
                yield ref
            async for ref in self._discover_incidents(client, base, cfg):
                yield ref

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch the full incident state + bitemporal log timeline.

        Only ``kind == 'PagerDutyIncident'`` refs require a fetch — catalog
        entities arrive complete from discover.  For any other kind we return
        the metadata serialised as JSON so the pipeline has a single uniform
        artefact per ref.
        """
        cfg: PagerDutyConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.api_base_url.rstrip("/")
        kind = str(ref.metadata.get("kind") or "")

        if kind != "PagerDutyIncident":
            import json

            payload = json.dumps(ref.metadata, sort_keys=True, default=str).encode("utf-8")
            return FetchedDocument(
                ref=ref,
                content_bytes=payload,
                content_type="application/json",
            )

        incident_id = str(ref.metadata.get("pagerduty_incident_id") or "")
        if not incident_id:
            raise ValueError("PagerDutyIncident ref missing 'pagerduty_incident_id' in metadata.")

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            inc_resp = await _request_with_rate_limit(
                client, "GET", f"{base}/incidents/{incident_id}"
            )
            inc_resp.raise_for_status()
            inc_body = inc_resp.json().get("incident") or {}

            log_entries: list[dict[str, Any]] = []
            async for page in _paginate(
                client,
                f"{base}/incidents/{incident_id}/log_entries",
                params={"include[]": "channels", "is_overview": "false"},
            ):
                entries = page.get("log_entries") or []
                if isinstance(entries, list):
                    log_entries.extend(e for e in entries if isinstance(e, dict))

        incident = _incident_from_json(inc_body, log_entries)
        if incident is None:
            raise RuntimeError(
                f"PagerDuty returned an unparseable incident payload for {incident_id!r}."
            )

        enriched_ref = _ref_for_incident(incident)
        # Append the per-log-entry edges (incident -> agent user, etc.) into
        # the metadata so the linker can wire them up.
        edges: list[dict[str, str]] = [
            {
                "type": "FILED_AGAINST",
                "from": canonical_incident_name(incident.id),
                "to": canonical_service_name(incident.service_id),
            }
        ]
        if incident.escalation_policy_id:
            edges.append(
                {
                    "type": "USES_ESCALATION",
                    "from": canonical_incident_name(incident.id),
                    "to": canonical_escalation_name(incident.escalation_policy_id),
                }
            )
        for uid in incident.assignee_user_ids:
            edges.append(
                {
                    "type": "ASSIGNED_TO",
                    "from": canonical_incident_name(incident.id),
                    "to": canonical_user_name(uid),
                }
            )
        for uid in incident.acknowledger_user_ids:
            edges.append(
                {
                    "type": "ACKNOWLEDGED_BY",
                    "from": canonical_incident_name(incident.id),
                    "to": canonical_user_name(uid),
                }
            )
        enriched_ref.metadata["edges"] = edges
        enriched_ref.metadata["log_entries"] = [
            {
                "id": e.id,
                "type": e.type,
                "created_at": e.created_at.isoformat(),
                "summary": e.summary,
                "agent_type": e.agent_type,
                "agent_id": e.agent_id,
                "channel_type": e.channel_type,
            }
            for e in incident.log_entries
        ]

        markdown = _render_incident_markdown(incident)
        return FetchedDocument(
            ref=enriched_ref,
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        """Reuse the existing alerts/pagerduty webhook receiver verbatim.

        The signature verification, multi-secret rotation and replay-
        protection patterns live in
        :class:`omniscience_connectors.alerts.webhook.AlertsWebhookHandler`
        — we do not duplicate them here.  Two providers (PagerDuty + Datadog)
        share that handler today; the dispatcher inside it selects the
        PagerDuty path when ``X-PagerDuty-Signature`` is present.
        """
        return AlertsWebhookHandler()

    # ---- v2 Action Mode placeholders -- see issue #230 --------------------

    async def acknowledge(self, incident_id: str) -> None:
        """Acknowledge an incident — DEFERRED to v2 Action Mode (issue #230)."""
        raise NotImplementedError(
            "PagerDuty acknowledge() is deferred to v2 Action Mode (issue #230). "
            "v1 is read-only by design."
        )

    async def snooze(self, incident_id: str, duration_seconds: int) -> None:
        """Snooze an incident — DEFERRED to v2 Action Mode (issue #230)."""
        raise NotImplementedError(
            "PagerDuty snooze() is deferred to v2 Action Mode (issue #230). "
            "v1 is read-only by design."
        )

    async def resolve(self, incident_id: str) -> None:
        """Resolve an incident — DEFERRED to v2 Action Mode (issue #230)."""
        raise NotImplementedError(
            "PagerDuty resolve() is deferred to v2 Action Mode (issue #230). "
            "v1 is read-only by design."
        )

    # ---- internal discover stages ----------------------------------------

    async def _discover_teams(
        self,
        client: httpx.AsyncClient,
        base: str,
        cfg: PagerDutyConfig,
    ) -> AsyncIterator[DocumentRef]:
        params: dict[str, Any] = {}
        if cfg.team_ids:
            # PagerDuty supports ``ids[]`` repeat for filter.
            params["ids[]"] = cfg.team_ids
        async for page in _paginate(client, f"{base}/teams", params=params):
            for raw in page.get("teams") or []:
                if not isinstance(raw, dict):
                    continue
                model = _team_from_json(raw)
                if model is not None:
                    yield _ref_for_team(model)

    async def _discover_escalations(
        self,
        client: httpx.AsyncClient,
        base: str,
        cfg: PagerDutyConfig,
    ) -> AsyncIterator[DocumentRef]:
        params: dict[str, Any] = {}
        if cfg.team_ids:
            params["team_ids[]"] = cfg.team_ids
        async for page in _paginate(client, f"{base}/escalation_policies", params=params):
            for raw in page.get("escalation_policies") or []:
                if not isinstance(raw, dict):
                    continue
                model = _escalation_from_json(raw)
                if model is not None:
                    yield _ref_for_escalation(model)

    async def _discover_services(
        self,
        client: httpx.AsyncClient,
        base: str,
        cfg: PagerDutyConfig,
    ) -> AsyncIterator[DocumentRef]:
        params: dict[str, Any] = {"include[]": "escalation_policies"}
        if cfg.team_ids:
            params["team_ids[]"] = cfg.team_ids
        async for page in _paginate(client, f"{base}/services", params=params):
            for raw in page.get("services") or []:
                if not isinstance(raw, dict):
                    continue
                model = _service_from_json(raw)
                if model is not None:
                    yield _ref_for_service(model)

    async def _discover_oncall_shifts(
        self,
        client: httpx.AsyncClient,
        base: str,
        cfg: PagerDutyConfig,
    ) -> AsyncIterator[DocumentRef]:
        """Pull the live on-call assignments and yield one shift per row.

        We use ``/oncalls`` (not ``/schedules/{id}/users``) because it returns
        the user + escalation policy + (start, end) tuple in a single call
        and respects team filters.  PagerDuty caps each shift query at the
        instantaneous-now window unless ``since``/``until`` are passed; we
        request the next 7 days of coverage so shift handovers are captured.
        """
        params: dict[str, Any] = {
            "since": _utc_now_iso(),
            "until": _utc_offset_iso(days=7),
        }
        if cfg.team_ids:
            params["team_ids[]"] = cfg.team_ids
        async for page in _paginate(client, f"{base}/oncalls", params=params):
            for raw in page.get("oncalls") or []:
                if not isinstance(raw, dict):
                    continue
                shift = _oncall_from_json(raw)
                if shift is not None:
                    yield _ref_for_oncall_shift(shift)

    async def _discover_incidents(
        self,
        client: httpx.AsyncClient,
        base: str,
        cfg: PagerDutyConfig,
    ) -> AsyncIterator[DocumentRef]:
        since = _utc_offset_iso(days=-cfg.incident_lookback_days)
        statuses = ["triggered", "acknowledged"]
        if cfg.include_resolved_incidents:
            statuses.append("resolved")
        params: dict[str, Any] = {
            "since": since,
            "statuses[]": statuses,
        }
        if cfg.service_ids:
            params["service_ids[]"] = cfg.service_ids
        if cfg.team_ids:
            params["team_ids[]"] = cfg.team_ids
        async for page in _paginate(client, f"{base}/incidents", params=params):
            for raw in page.get("incidents") or []:
                if not isinstance(raw, dict):
                    continue
                incident = _incident_from_json(raw)
                if incident is not None:
                    yield _ref_for_incident(incident)


# ---------------------------------------------------------------------------
# Module-private helpers (continued)
# ---------------------------------------------------------------------------


def _oncall_from_json(payload: dict[str, Any]) -> OnCallShift | None:
    schedule = payload.get("schedule") or {}
    user = payload.get("user") or {}
    escalation = payload.get("escalation_policy") or {}
    if not isinstance(schedule, dict) or not isinstance(user, dict):
        return None
    sid = str(schedule.get("id") or "")
    uid = str(user.get("id") or "")
    if not sid or not uid:
        return None
    start = _parse_iso8601(str(payload.get("start") or ""))
    end = _parse_iso8601(str(payload.get("end") or ""))
    if start is None or end is None:
        return None
    return OnCallShift(
        schedule_id=sid,
        schedule_name=str(schedule.get("summary")) if schedule.get("summary") else None,
        user_id=uid,
        user_name=str(user.get("summary")) if user.get("summary") else None,
        user_email=str(user["email"]) if isinstance(user, dict) and user.get("email") else None,
        escalation_policy_id=(
            str(escalation.get("id"))
            if isinstance(escalation, dict) and escalation.get("id")
            else None
        ),
        escalation_level=(
            int(payload["escalation_level"])
            if isinstance(payload.get("escalation_level"), int)
            else None
        ),
        start=start,
        end=end,
        html_url=str(schedule["html_url"]) if schedule.get("html_url") else None,
    )


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_offset_iso(*, days: int) -> str:
    return (datetime.now(tz=UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_incident_markdown(incident: PagerDutyIncident) -> str:
    """Render an incident + timeline as Markdown for downstream chunking."""
    lines: list[str] = [
        f"# Incident #{incident.incident_number}: {incident.title}",
        "",
        f"- ID: {incident.id}",
        f"- Status: {incident.status}",
    ]
    if incident.urgency:
        lines.append(f"- Urgency: {incident.urgency}")
    lines.append(f"- Service: {canonical_service_name(incident.service_id)}")
    if incident.escalation_policy_id:
        lines.append(f"- Escalation: {canonical_escalation_name(incident.escalation_policy_id)}")
    lines.append(f"- Created at: {incident.created_at.isoformat()}")
    if incident.updated_at:
        lines.append(f"- Updated at: {incident.updated_at.isoformat()}")
    if incident.resolved_at:
        lines.append(f"- Resolved at: {incident.resolved_at.isoformat()}")
    if incident.assignee_user_ids:
        lines.append(
            "- Assignees: " + ", ".join(canonical_user_name(u) for u in incident.assignee_user_ids)
        )
    if incident.acknowledger_user_ids:
        lines.append(
            "- Acknowledgers: "
            + ", ".join(canonical_user_name(u) for u in incident.acknowledger_user_ids)
        )

    if incident.log_entries:
        lines.append("")
        lines.append("## Timeline")
        lines.append("")
        for entry in incident.log_entries:
            ts = entry.created_at.isoformat()
            who = entry.agent_id or entry.agent_type or "system"
            summary = entry.summary or entry.type
            lines.append(f"- `{ts}` [{entry.type}] {who}: {summary}")

    return "\n".join(lines) + "\n"
