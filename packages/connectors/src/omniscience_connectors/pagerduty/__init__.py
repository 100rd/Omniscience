"""PagerDuty full bi-directional source connector (issue #239).

Read-only in v1 — bi-directional acks/snooze/resolve are deferred to v2
Action Mode (issue #230).  The push-only webhook receiver in
:mod:`omniscience_connectors.alerts` is reused unchanged; this package adds
catalog discovery (services, teams, escalation policies, on-call schedules)
and bitemporal incident sync.
"""

from omniscience_connectors.pagerduty.connector import (
    PagerDutyConfig,
    PagerDutyConnector,
    canonical_escalation_name,
    canonical_incident_name,
    canonical_oncall_shift_name,
    canonical_service_name,
    canonical_team_name,
    canonical_user_name,
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
    "IncidentLogEntry",
    "OnCallShift",
    "PagerDutyConfig",
    "PagerDutyConnector",
    "PagerDutyEscalation",
    "PagerDutyIncident",
    "PagerDutyService",
    "PagerDutyTeam",
    "canonical_escalation_name",
    "canonical_incident_name",
    "canonical_oncall_shift_name",
    "canonical_service_name",
    "canonical_team_name",
    "canonical_user_name",
]
