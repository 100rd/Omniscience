"""Entity models for the PagerDuty full connector.

Distinct from the push-only :mod:`omniscience_connectors.alerts.connector`
``NormalizedAlert`` shape: this module describes the *catalog* (services,
teams, escalation policies, on-call schedules) and *incidents* that the
pull-based connector enumerates via the REST API.

ACL invariant (P0)
------------------
Like all source connectors, none of these models read or trust tenancy
fields from PagerDuty payloads.  Workspace identity is set upstream from
the :class:`Source` row's ``tenant_id`` at ingest time; the values carried
here are forensic context only.

Bi-directional write-back (acks, snoozes, resolves) is **deferred to v2**
Action Mode.  See issue #230 and the comment in
:mod:`omniscience_connectors.pagerduty.connector` for the migration plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "IncidentLogEntry",
    "OnCallShift",
    "PagerDutyEscalation",
    "PagerDutyIncident",
    "PagerDutyService",
    "PagerDutyTeam",
]


# ---------------------------------------------------------------------------
# Catalog entities
# ---------------------------------------------------------------------------


class PagerDutyTeam(BaseModel):
    """A PagerDuty team — owns services and escalation policies."""

    id: str
    """Stable PagerDuty team ID (e.g. ``P123456``)."""

    name: str
    """Human-readable team name."""

    summary: str | None = None
    """Short description as returned by the REST API."""

    html_url: str | None = None
    """Web URL pointing at the team in the PagerDuty UI."""


class PagerDutyService(BaseModel):
    """A PagerDuty service — the object incidents are filed against."""

    id: str
    """Stable PagerDuty service ID."""

    name: str
    """Human-readable service name."""

    status: str | None = None
    """Service status (``active``, ``warning``, ``critical``, ``maintenance``, ``disabled``)."""

    description: str | None = None

    team_ids: list[str] = Field(default_factory=list)
    """IDs of the teams that own this service.  Drives the
    ``service → team`` edge emitted by the connector."""

    escalation_policy_id: str | None = None
    """ID of the escalation policy attached to this service.  Drives the
    ``service → escalation`` edge."""

    html_url: str | None = None


class PagerDutyEscalation(BaseModel):
    """A PagerDuty escalation policy — ordered list of escalation rules."""

    id: str
    name: str
    summary: str | None = None
    description: str | None = None
    team_ids: list[str] = Field(default_factory=list)
    num_loops: int = 0
    """Number of times the policy loops if nobody acknowledges."""

    rule_count: int = 0
    """Number of escalation rules (delay tiers) in the policy."""

    html_url: str | None = None


class OnCallShift(BaseModel):
    """A single on-call shift block from a PagerDuty schedule.

    Conceptually maps to one ``(user, escalation_policy, start, end)``
    interval — the unit that drives the ``on-call shift → user`` edge.
    """

    schedule_id: str
    """ID of the parent schedule."""

    schedule_name: str | None = None

    user_id: str
    """PagerDuty user ID who is on-call during this shift."""

    user_name: str | None = None

    user_email: str | None = None
    """Email of the on-call user.  Forensic context only; never used for tenancy."""

    escalation_policy_id: str | None = None
    """Escalation policy that resolved this on-call shift, if available."""

    escalation_level: int | None = None
    """1-based escalation level within the policy."""

    start: datetime
    """Shift start (UTC)."""

    end: datetime
    """Shift end (UTC)."""

    html_url: str | None = None


# ---------------------------------------------------------------------------
# Incident state (bitemporal)
# ---------------------------------------------------------------------------


IncidentStatus = Literal["triggered", "acknowledged", "resolved"]


class IncidentLogEntry(BaseModel):
    """One immutable line of the PagerDuty incident timeline.

    Log entries cover every state transition: triggered, acknowledged,
    notified, escalated, snoozed, resolved, annotated.  We carry them
    verbatim (modulo type-shape) to support bitemporal replay downstream.
    """

    id: str
    type: str
    """PagerDuty log-entry ``type`` (e.g. ``trigger_log_entry``)."""

    created_at: datetime
    """Wall-clock time the entry was recorded at PagerDuty (UTC)."""

    summary: str | None = None
    agent_type: str | None = None
    """Who/what produced the entry — ``user``, ``service``, ``api``, ``escalation_policy``…"""

    agent_id: str | None = None
    channel_type: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    """Original log entry preserved for forensics."""


class PagerDutyIncident(BaseModel):
    """A PagerDuty incident — the bitemporally synced root entity."""

    id: str
    incident_number: int
    title: str
    status: IncidentStatus
    urgency: Literal["high", "low"] | None = None
    created_at: datetime
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    service_id: str
    """Service the incident is filed against — drives ``incident → service`` edge."""

    escalation_policy_id: str | None = None
    """Effective escalation policy at the time of capture."""

    assignee_user_ids: list[str] = Field(default_factory=list)
    acknowledger_user_ids: list[str] = Field(default_factory=list)
    log_entries: list[IncidentLogEntry] = Field(default_factory=list)
    html_url: str | None = None
