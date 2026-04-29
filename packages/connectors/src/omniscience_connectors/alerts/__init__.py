"""Alerts source connector for Omniscience.

Push-only connector for SRE alerting providers (PagerDuty, Datadog).
Discovery is a no-op; alerts arrive exclusively via signed webhooks.

Public surface
--------------
``AlertsConnector``      — connector type ``"alerts"``.
``AlertsConfig``         — public configuration schema.
``AlertsWebhookHandler`` — HMAC-SHA256 signature verification.
``NormalizedAlert``      — provider-agnostic alert payload.
"""

from omniscience_connectors.alerts.connector import (
    AlertsConfig,
    AlertsConnector,
    NormalizedAlert,
)
from omniscience_connectors.alerts.webhook import AlertsWebhookHandler

__all__ = [
    "AlertsConfig",
    "AlertsConnector",
    "AlertsWebhookHandler",
    "NormalizedAlert",
]
