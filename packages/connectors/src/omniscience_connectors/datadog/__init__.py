"""Datadog source connector for Omniscience.

Indexes Datadog monitors, dashboards, service catalog entries, SLOs, and
events as first-class bitemporal entities so the alert -> service -> host/pod
causal chain has a Datadog-native surface.

Entity kinds emitted:

* :class:`DatadogMonitor` — ``datadog://monitor/{org}/{monitor_id}``
* :class:`DatadogDashboard` — ``datadog://dashboard/{org}/{dashboard_id}``
* :class:`DatadogService` — ``datadog://service/{org}/{service_name}``
* :class:`DatadogSLO` — ``datadog://slo/{org}/{slo_id}``
* :class:`DatadogEvent` — ``datadog://event/{org}/{event_id}``

Edges:

* monitor ``-[:TARGETS]->`` service
* monitor ``-[:TARGETS]->`` host/pod (when monitor query references one)
* event ``-[:CORRELATES]->`` monitor (audit + alert state changes)
* dashboard ``-[:OBSERVES]->`` service

The companion :class:`omniscience_connectors.alerts.AlertsConnector` continues
to ingest live alert webhooks; this connector handles the *configuration*
surface (monitors/dashboards/service catalog) plus the *event stream* so
post-mortems can reconstruct what was alerting and when.
"""

from omniscience_connectors.datadog.connector import (
    CANONICAL_DASHBOARD_NAME_REGEX,
    CANONICAL_EVENT_NAME_REGEX,
    CANONICAL_MONITOR_NAME_REGEX,
    CANONICAL_SERVICE_NAME_REGEX,
    CANONICAL_SLO_NAME_REGEX,
    DatadogConfig,
    DatadogConnector,
    canonical_dashboard_name,
    canonical_event_name,
    canonical_monitor_name,
    canonical_service_name,
    canonical_slo_name,
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
    "canonical_monitor_name",
    "canonical_service_name",
    "canonical_slo_name",
]
