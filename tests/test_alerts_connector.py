"""Unit tests for the alerts connector.

Covers:
- PagerDuty payload -> NormalizedAlert
- Datadog payload -> NormalizedAlert
- Severity / status mapping for each provider
- cross_ref edge emission for ARN, pod, service, trace_id
- Connector protocol contract (discover no-op, fetch raises)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from omniscience_connectors.alerts.connector import (
    AlertsConfig,
    AlertsConnector,
    NormalizedAlert,
    extract_cross_refs,
    normalize_datadog,
    normalize_pagerduty,
)

# ---------------------------------------------------------------------------
# PagerDuty normalisation
# ---------------------------------------------------------------------------


def _pd_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event": {
            "occurred_at": "2026-04-24T10:00:00Z",
            "data": {
                "id": "PXXXXXXX",
                "title": "High CPU on web pod/checkout-7d4f9 — investigate",
                "summary": "checkout-7d4f9 CPU > 95% for 5m",
                "description": (
                    "Container in pod/checkout-7d4f9 against "
                    "arn:aws:ec2:us-east-1:111122223333:instance/i-0abc1234"
                ),
                "severity": "critical",
                "status": "triggered",
                "created_at": "2026-04-24T10:00:00Z",
                "service": {
                    "id": "PSVCID01",
                    "summary": "checkout-api",
                },
                "custom_details": {
                    "trace": "trace://aabbccddeeff00112233445566778899",
                },
            },
        }
    }
    base.update(overrides)
    return base


def test_pagerduty_normalises_core_fields() -> None:
    alert = normalize_pagerduty(_pd_payload())
    assert alert.provider == "pagerduty"
    assert alert.provider_alert_id == "PXXXXXXX"
    assert alert.severity == "critical"
    assert alert.status == "triggered"
    assert alert.summary.startswith("High CPU")
    assert alert.target_service == "checkout-api"
    assert alert.fired_at == datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)


def test_pagerduty_extracts_arn_pod_and_trace() -> None:
    alert = normalize_pagerduty(_pd_payload())
    assert alert.target_arn == "arn:aws:ec2:us-east-1:111122223333:instance/i-0abc1234"
    assert alert.target_pod == "pod/checkout-7d4f9"
    # trace_id is the bare hex; cross_ref emission re-prefixes with trace://
    assert alert.trace_id == "aabbccddeeff00112233445566778899"


def test_pagerduty_severity_mapping_defaults_to_warning() -> None:
    payload = _pd_payload()
    payload["event"] = {  # type: ignore[index]
        "data": {"id": "P1", "severity": "BogusLevel", "status": "triggered"}
    }
    alert = normalize_pagerduty(payload)
    assert alert.severity == "warning"


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        ("triggered", "triggered"),
        ("acknowledged", "acknowledged"),
        ("resolved", "resolved"),
    ],
)
def test_pagerduty_status_mapping(raw_status: str, expected: str) -> None:
    payload = _pd_payload()
    payload["event"] = {  # type: ignore[index]
        "data": {"id": "P1", "severity": "warning", "status": raw_status}
    }
    alert = normalize_pagerduty(payload)
    assert alert.status == expected


def test_pagerduty_with_no_named_entities_yields_none_targets() -> None:
    payload = {"event": {"data": {"id": "P2", "title": "DB stalled", "severity": "error"}}}
    alert = normalize_pagerduty(payload)
    assert alert.target_arn is None
    assert alert.target_pod is None
    assert alert.trace_id is None


# ---------------------------------------------------------------------------
# Datadog normalisation
# ---------------------------------------------------------------------------


def _dd_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "1234567890",
        "alert_type": "alert",
        "priority": "P1",
        "title": "[CRIT] checkout latency p99 > 500ms",
        "body": (
            "Saw arn:aws:ec2:us-east-1:111122223333:instance/i-0abc1234 "
            "spike in pod/checkout-7d4f9; trace://aabbccddeeff00112233445566778899"
        ),
        "date_happened": 1761301200,
        "tags": [
            "service:checkout",
            "env:prod",
            "pod_name:checkout-7d4f9",
        ],
    }
    base.update(overrides)
    return base


def test_datadog_normalises_core_fields() -> None:
    alert = normalize_datadog(_dd_payload())
    assert alert.provider == "datadog"
    assert alert.provider_alert_id == "1234567890"
    assert alert.severity == "critical"  # P1 -> critical
    assert alert.status == "triggered"
    assert alert.target_service == "checkout"


def test_datadog_extracts_arn_pod_trace_from_body() -> None:
    alert = normalize_datadog(_dd_payload())
    assert alert.target_arn == "arn:aws:ec2:us-east-1:111122223333:instance/i-0abc1234"
    # pod_name tag wins over body regex; both yield same value here.
    assert alert.target_pod == "checkout-7d4f9"
    assert alert.trace_id == "aabbccddeeff00112233445566778899"


@pytest.mark.parametrize(
    "priority,expected",
    [
        ("P1", "critical"),
        ("P2", "error"),
        ("P3", "warning"),
        ("P4", "info"),
        ("P5", "info"),
    ],
)
def test_datadog_priority_mapping(priority: str, expected: str) -> None:
    payload = _dd_payload(priority=priority)
    alert = normalize_datadog(payload)
    assert alert.severity == expected


@pytest.mark.parametrize(
    "alert_type,expected",
    [
        ("alert", "triggered"),
        ("alert_triggered", "triggered"),
        ("alert_recovery", "resolved"),
        ("recovery", "resolved"),
        ("alert_acknowledged", "acknowledged"),
    ],
)
def test_datadog_alert_type_to_status(alert_type: str, expected: str) -> None:
    payload = _dd_payload(alert_type=alert_type)
    alert = normalize_datadog(payload)
    assert alert.status == expected


def test_datadog_falls_back_to_warning_for_unknown_priority() -> None:
    payload = _dd_payload(priority="P99")
    alert = normalize_datadog(payload)
    assert alert.severity == "warning"


# ---------------------------------------------------------------------------
# cross_ref edge extraction
# ---------------------------------------------------------------------------


def test_extract_cross_refs_emits_alert_plus_target_refs() -> None:
    alert = NormalizedAlert(
        provider="pagerduty",
        provider_alert_id="PXXXXX",
        severity="critical",
        status="triggered",
        fired_at=datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC),
        summary="x",
        target_arn="arn:aws:ec2:us-east-1:111:instance/i-1",
        target_pod="pod/checkout",
        target_service="checkout-api",
        trace_id="aabbcc",
    )
    refs = extract_cross_refs(alert)

    # First ref: alert entity itself.
    assert refs[0].uri == "alert://pagerduty/PXXXXX"
    assert refs[0].metadata["kind"] == "alert"
    assert refs[0].metadata["severity"] == "critical"

    names = [r.uri for r in refs[1:]]
    assert "arn:aws:ec2:us-east-1:111:instance/i-1" in names
    assert "pod/checkout" in names
    assert "service://checkout-api" in names
    assert "trace://aabbcc" in names

    # All non-alert refs are FIRES_AGAINST cross_ref targets.
    for r in refs[1:]:
        assert r.metadata["edge_type"] == "FIRES_AGAINST"
        assert r.metadata["from_alert"] == "alert://pagerduty/PXXXXX"


def test_extract_cross_refs_skips_missing_targets() -> None:
    alert = NormalizedAlert(
        provider="datadog",
        provider_alert_id="42",
        severity="info",
        status="resolved",
        fired_at=datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC),
        summary="recovered",
    )
    refs = extract_cross_refs(alert)
    # Only the alert entity ref is emitted.
    assert len(refs) == 1
    assert refs[0].uri == "alert://datadog/42"


# ---------------------------------------------------------------------------
# Connector protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connector_discover_yields_nothing() -> None:
    cfg = AlertsConfig(provider="pagerduty")
    conn = AlertsConnector()
    items = [ref async for ref in conn.discover(cfg, {"webhook_secret": "x"})]
    assert items == []


@pytest.mark.asyncio
async def test_connector_fetch_raises_not_implemented() -> None:
    cfg = AlertsConfig(provider="datadog")
    conn = AlertsConnector()
    from omniscience_connectors.base import DocumentRef

    with pytest.raises(NotImplementedError):
        await conn.fetch(
            cfg,
            {"webhook_secret": "x"},
            DocumentRef(external_id="alert://datadog/1", uri="alert://datadog/1"),
        )


@pytest.mark.asyncio
async def test_connector_validate_requires_webhook_secret() -> None:
    cfg = AlertsConfig(provider="pagerduty")
    conn = AlertsConnector()
    with pytest.raises(ValueError, match="webhook_secret"):
        await conn.validate(cfg, {})


@pytest.mark.asyncio
async def test_connector_validate_accepts_valid_config() -> None:
    cfg = AlertsConfig(provider="pagerduty")
    conn = AlertsConnector()
    await conn.validate(cfg, {"webhook_secret": "abc"})


def test_connector_registered_in_registry() -> None:
    """Smoke-test: the connector is registered under type 'alerts'."""
    from omniscience_connectors import default_registry

    instance = default_registry.get("alerts")
    assert isinstance(instance, AlertsConnector)
