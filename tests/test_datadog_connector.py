"""Tests for the Datadog connector (Issue #240).

Coverage:
- DatadogConfig defaults and connector_type
- Canonical-name helpers (monitor, dashboard, service, slo, event, host, pod)
- _validate_org_uid, _build_auth_headers
- DatadogConnector.validate — success, 401, 403, 500, missing keys
- DatadogConnector.discover — monitors, dashboards, service catalog, SLOs
- DatadogConnector.fetch — per-kind round-trips, edge emission
- DatadogConnector.fetch_events — bitemporal window, event -> monitor edges
- Rate-limit handling — 429 + X-RateLimit-Reset triggers sleep + retry
- Registry integration — datadog is registered
- VCR cassette replay — deterministic JSON cassettes under tests/fixtures/datadog
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from omniscience_connectors import get_connector
from omniscience_connectors.base import DocumentRef
from omniscience_connectors.datadog.connector import (
    CANONICAL_DASHBOARD_NAME_REGEX,
    CANONICAL_EVENT_NAME_REGEX,
    CANONICAL_MONITOR_NAME_REGEX,
    CANONICAL_SERVICE_NAME_REGEX,
    CANONICAL_SLO_NAME_REGEX,
    DatadogConfig,
    DatadogConnector,
    _build_auth_headers,
    _extract_hosts_from_query,
    _extract_pods_from_query,
    _extract_services_from_query,
    _validate_org_uid,
    canonical_dashboard_name,
    canonical_event_name,
    canonical_host_name,
    canonical_monitor_name,
    canonical_pod_name,
    canonical_service_name,
    canonical_slo_name,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ORG = "acme01"
SITE = "datadoghq.com"
API_BASE = f"https://api.{SITE}"
API_KEY = "dd_api_key_FAKE"
APP_KEY = "dd_app_key_FAKE"
SECRETS = {"datadog_api_key": API_KEY, "datadog_app_key": APP_KEY}

CASSETTE_DIR = Path(__file__).parent / "fixtures" / "datadog"


def _load_cassette(name: str) -> Any:
    """Load a deterministic JSON cassette under tests/fixtures/datadog/."""
    return json.loads((CASSETTE_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Config + helpers
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    cfg = DatadogConfig(org_uid=ORG)
    assert cfg.site == SITE
    assert cfg.include_monitors is True
    assert cfg.include_dashboards is True
    assert cfg.include_service_catalog is True
    assert cfg.include_slos is True
    assert cfg.events_lookback_minutes == 60


def test_config_connector_type() -> None:
    assert DatadogConnector.connector_type == "datadog"
    assert DatadogConnector.config_schema is DatadogConfig


@pytest.mark.parametrize("org", ["acme01", "Acme", "team-a", "org.0", "a"])
def test_validate_org_uid_accepts_good_slugs(org: str) -> None:
    assert _validate_org_uid(org) == org


@pytest.mark.parametrize("org", ["", "-bad", "white space", "x" * 100, "a/b"])
def test_validate_org_uid_rejects_bad_slugs(org: str) -> None:
    with pytest.raises(ValueError, match="Invalid org_uid"):
        _validate_org_uid(org)


def test_build_auth_headers_uses_dd_headers() -> None:
    headers = _build_auth_headers(SECRETS)
    assert headers["DD-API-KEY"] == API_KEY
    assert headers["DD-APPLICATION-KEY"] == APP_KEY
    assert headers["Accept"] == "application/json"


def test_build_auth_headers_missing_api_key_raises() -> None:
    with pytest.raises(ValueError, match="datadog_api_key"):
        _build_auth_headers({"datadog_app_key": APP_KEY})


def test_build_auth_headers_missing_app_key_raises() -> None:
    with pytest.raises(ValueError, match="datadog_app_key"):
        _build_auth_headers({"datadog_api_key": API_KEY})


# ---------------------------------------------------------------------------
# Canonical names
# ---------------------------------------------------------------------------


def test_canonical_monitor_name_matches_regex() -> None:
    name = canonical_monitor_name(ORG, 1001)
    assert name == f"datadog://monitor/{ORG}/1001"
    assert CANONICAL_MONITOR_NAME_REGEX.fullmatch(name)


def test_canonical_dashboard_name_matches_regex() -> None:
    name = canonical_dashboard_name(ORG, "abc-123-xyz")
    assert name == f"datadog://dashboard/{ORG}/abc-123-xyz"
    assert CANONICAL_DASHBOARD_NAME_REGEX.fullmatch(name)


def test_canonical_service_name_matches_regex() -> None:
    name = canonical_service_name(ORG, "checkout")
    assert name == f"datadog://service/{ORG}/checkout"
    assert CANONICAL_SERVICE_NAME_REGEX.fullmatch(name)


def test_canonical_slo_name_matches_regex() -> None:
    name = canonical_slo_name(ORG, "slo-checkout-latency")
    assert CANONICAL_SLO_NAME_REGEX.fullmatch(name)


def test_canonical_event_name_matches_regex() -> None:
    name = canonical_event_name(ORG, 9000001)
    assert CANONICAL_EVENT_NAME_REGEX.fullmatch(name)


def test_canonical_host_and_pod_helpers() -> None:
    assert canonical_host_name("db-1") == "host://db-1"
    assert canonical_pod_name("checkout-abc") == "pod/checkout-abc"
    assert canonical_pod_name("checkout-abc", "default") == "default/checkout-abc"


def test_extract_services_from_monitor_query() -> None:
    q = "avg(last_5m):avg:trace.servlet.request.duration{service:checkout,env:prod} > 1.5"
    assert _extract_services_from_query(q) == ["checkout"]


def test_extract_hosts_and_pods_from_monitor_query() -> None:
    q = "avg(last_5m):avg:postgres.replication_lag{host:db-1,pod_name:pg-0} > 30"
    assert _extract_hosts_from_query(q) == ["db-1"]
    assert _extract_pods_from_query(q) == ["pg-0"]


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_validate_success() -> None:
    respx.get(f"{API_BASE}/api/v1/validate").mock(
        return_value=httpx.Response(200, json={"valid": True})
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    await connector.validate(cfg, SECRETS)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_validate_unauthorized_raises_permission_error(status: int) -> None:
    respx.get(f"{API_BASE}/api/v1/validate").mock(
        return_value=httpx.Response(status, json={"errors": ["bad"]})
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    with pytest.raises(PermissionError):
        await connector.validate(cfg, SECRETS)


@respx.mock
@pytest.mark.asyncio
async def test_validate_server_error_raises_runtime_error() -> None:
    respx.get(f"{API_BASE}/api/v1/validate").mock(
        return_value=httpx.Response(500, json={"errors": ["boom"]})
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    with pytest.raises(RuntimeError, match="validate failed"):
        await connector.validate(cfg, SECRETS)


@pytest.mark.asyncio
async def test_validate_rejects_bad_org_uid() -> None:
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid="ok-")
    # Force bad uid via direct construction (model allows any non-empty string;
    # validation is enforced inside validate()).
    cfg.org_uid = "bad space"
    with pytest.raises(ValueError, match="Invalid org_uid"):
        await connector.validate(cfg, SECRETS)


# ---------------------------------------------------------------------------
# discover() — uses VCR cassettes
# ---------------------------------------------------------------------------


def _wire_full_discover() -> None:
    """Register respx routes for a full discover() over all four entity kinds.

    Returns ``[]`` after one populated page for each list endpoint to
    terminate pagination deterministically.
    """
    respx.get(f"{API_BASE}/api/v1/monitor").mock(
        side_effect=[
            httpx.Response(200, json=_load_cassette("monitors_page_0.json")),
            httpx.Response(200, json=[]),
        ]
    )
    respx.get(f"{API_BASE}/api/v1/dashboard").mock(
        side_effect=[
            httpx.Response(200, json=_load_cassette("dashboards_page_0.json")),
            httpx.Response(200, json={"dashboards": []}),
        ]
    )
    respx.get(f"{API_BASE}/api/v2/services/definitions").mock(
        side_effect=[
            httpx.Response(200, json=_load_cassette("services_page_0.json")),
            httpx.Response(200, json={"data": []}),
        ]
    )
    respx.get(f"{API_BASE}/api/v1/slo").mock(
        side_effect=[
            httpx.Response(200, json=_load_cassette("slos_page_0.json")),
            httpx.Response(200, json={"data": []}),
        ]
    )


@respx.mock
@pytest.mark.asyncio
async def test_discover_emits_all_four_entity_kinds() -> None:
    _wire_full_discover()
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    refs = [r async for r in connector.discover(cfg, SECRETS)]

    kinds = [r.metadata["kind"] for r in refs]
    assert kinds.count("monitor") == 2
    assert kinds.count("dashboard") == 2
    assert kinds.count("service") == 2
    assert kinds.count("slo") == 2

    # Canonical URIs match the regexes
    monitors = [r for r in refs if r.metadata["kind"] == "monitor"]
    for m in monitors:
        assert CANONICAL_MONITOR_NAME_REGEX.fullmatch(m.uri)
    dashboards = [r for r in refs if r.metadata["kind"] == "dashboard"]
    for d in dashboards:
        assert CANONICAL_DASHBOARD_NAME_REGEX.fullmatch(d.uri)
    services = [r for r in refs if r.metadata["kind"] == "service"]
    for s in services:
        assert CANONICAL_SERVICE_NAME_REGEX.fullmatch(s.uri)
    slos = [r for r in refs if r.metadata["kind"] == "slo"]
    for s in slos:
        assert CANONICAL_SLO_NAME_REGEX.fullmatch(s.uri)


@respx.mock
@pytest.mark.asyncio
async def test_discover_monitors_extracts_service_hint() -> None:
    respx.get(f"{API_BASE}/api/v1/monitor").mock(
        return_value=httpx.Response(200, json=_load_cassette("monitors_page_0.json"))
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(
        org_uid=ORG,
        include_dashboards=False,
        include_service_catalog=False,
        include_slos=False,
    )
    refs = [r async for r in connector.discover(cfg, SECRETS)]
    checkout = next(r for r in refs if r.metadata["monitor_id"] == 1001)
    assert "checkout" in checkout.metadata["services_hint"]
    postgres = next(r for r in refs if r.metadata["monitor_id"] == 1002)
    assert "postgres" in postgres.metadata["services_hint"]


@respx.mock
@pytest.mark.asyncio
async def test_discover_respects_disabled_flags() -> None:
    """When all four include flags are False, no API calls are made."""
    connector = DatadogConnector()
    cfg = DatadogConfig(
        org_uid=ORG,
        include_monitors=False,
        include_dashboards=False,
        include_service_catalog=False,
        include_slos=False,
    )
    refs = [r async for r in connector.discover(cfg, SECRETS)]
    assert refs == []
    assert len(respx.calls) == 0


@respx.mock
@pytest.mark.asyncio
async def test_discover_monitors_handles_api_error_gracefully() -> None:
    respx.get(f"{API_BASE}/api/v1/monitor").mock(
        return_value=httpx.Response(500, json={"errors": ["x"]})
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(
        org_uid=ORG,
        include_dashboards=False,
        include_service_catalog=False,
        include_slos=False,
    )
    refs = [r async for r in connector.discover(cfg, SECRETS)]
    assert refs == []


# ---------------------------------------------------------------------------
# fetch() — per-kind round-trips
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fetch_monitor_renders_markdown_and_emits_targets_edges() -> None:
    respx.get(f"{API_BASE}/api/v1/monitor/1001").mock(
        return_value=httpx.Response(200, json=_load_cassette("monitor_1001.json"))
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    ref = DocumentRef(
        external_id="x",
        uri=canonical_monitor_name(ORG, 1001),
        metadata={"kind": "monitor", "monitor_id": 1001, "org_uid": ORG},
    )
    doc = await connector.fetch(cfg, SECRETS, ref)
    assert doc.content_type == "text/markdown"
    md = doc.content_bytes.decode()
    assert "# Monitor #1001:" in md
    assert "Checkout latency" in md
    assert "service:checkout" in md

    edges = doc.ref.metadata["edges"]
    types = {e["type"] for e in edges}
    assert types == {"TARGETS"}
    targets = {e["to"] for e in edges if e["type"] == "TARGETS"}
    assert canonical_service_name(ORG, "checkout") in targets
    assert canonical_host_name("checkout-pool-a") in targets
    assert canonical_pod_name("checkout-7f6c") in targets


@respx.mock
@pytest.mark.asyncio
async def test_fetch_dashboard_emits_observes_edges() -> None:
    respx.get(f"{API_BASE}/api/v1/dashboard/abc-123-xyz").mock(
        return_value=httpx.Response(200, json=_load_cassette("dashboard_abc.json"))
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    ref = DocumentRef(
        external_id="x",
        uri=canonical_dashboard_name(ORG, "abc-123-xyz"),
        metadata={
            "kind": "dashboard",
            "dashboard_id": "abc-123-xyz",
            "org_uid": ORG,
        },
    )
    doc = await connector.fetch(cfg, SECRETS, ref)
    md = doc.content_bytes.decode()
    assert "Checkout overview" in md
    assert "Request rate" in md
    edges = doc.ref.metadata["edges"]
    assert any(
        e["type"] == "OBSERVES" and e["to"] == canonical_service_name(ORG, "checkout")
        for e in edges
    )


@respx.mock
@pytest.mark.asyncio
async def test_fetch_service_renders_catalog_entry() -> None:
    cassette = _load_cassette("services_page_0.json")
    respx.get(f"{API_BASE}/api/v2/services/definitions/checkout").mock(
        return_value=httpx.Response(200, json={"data": cassette["data"][0]})
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    ref = DocumentRef(
        external_id="x",
        uri=canonical_service_name(ORG, "checkout"),
        metadata={"kind": "service", "service": "checkout", "org_uid": ORG},
    )
    doc = await connector.fetch(cfg, SECRETS, ref)
    md = doc.content_bytes.decode()
    assert "# Service: checkout" in md
    assert "payments" in md
    assert "Synchronous checkout API" in md


@respx.mock
@pytest.mark.asyncio
async def test_fetch_slo_renders_thresholds() -> None:
    cassette = _load_cassette("slos_page_0.json")
    respx.get(f"{API_BASE}/api/v1/slo/slo-checkout-latency").mock(
        return_value=httpx.Response(200, json={"data": cassette["data"][0]})
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    ref = DocumentRef(
        external_id="x",
        uri=canonical_slo_name(ORG, "slo-checkout-latency"),
        metadata={"kind": "slo", "slo_id": "slo-checkout-latency", "org_uid": ORG},
    )
    doc = await connector.fetch(cfg, SECRETS, ref)
    md = doc.content_bytes.decode()
    assert "Checkout p99 < 500ms" in md
    assert "target=99.0" in md


@respx.mock
@pytest.mark.asyncio
async def test_fetch_event_emits_correlates_edge_to_monitor() -> None:
    respx.get(f"{API_BASE}/api/v1/events/9000001").mock(
        return_value=httpx.Response(200, json=_load_cassette("event_9000001.json"))
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    ref = DocumentRef(
        external_id="x",
        uri=canonical_event_name(ORG, 9000001),
        metadata={"kind": "event", "event_id": 9000001, "org_uid": ORG},
    )
    doc = await connector.fetch(cfg, SECRETS, ref)
    md = doc.content_bytes.decode()
    assert "Triggered" in md
    edges = doc.ref.metadata["edges"]
    assert edges == [
        {
            "from": canonical_event_name(ORG, 9000001),
            "to": canonical_monitor_name(ORG, 1001),
            "type": "CORRELATES",
        }
    ]


@pytest.mark.asyncio
async def test_fetch_unknown_kind_raises() -> None:
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    ref = DocumentRef(
        external_id="x",
        uri="datadog://unknown/x",
        metadata={"kind": "unknown"},
    )
    with pytest.raises(ValueError, match="unknown kind"):
        await connector.fetch(cfg, SECRETS, ref)


# ---------------------------------------------------------------------------
# fetch_events() — bitemporal sync window
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fetch_events_yields_refs_with_monitor_link() -> None:
    route = respx.get(f"{API_BASE}/api/v1/events").mock(
        return_value=httpx.Response(200, json=_load_cassette("events_window.json"))
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    # Lock the window so the request params are deterministic.
    pinned_now = datetime(2026, 5, 22, 18, 0, 0, tzinfo=UTC)
    refs = [r async for r in connector.fetch_events(cfg, SECRETS, now=pinned_now)]
    assert len(refs) == 3
    ids = {r.metadata["event_id"] for r in refs}
    assert ids == {9000001, 9000002, 9000003}
    for r in refs:
        assert r.metadata["monitor_id"] == 1001
        assert CANONICAL_EVENT_NAME_REGEX.fullmatch(r.uri)
        assert r.updated_at is not None and r.updated_at.tzinfo is not None

    request = route.calls[0].request
    params = dict(request.url.params)
    assert int(params["end"]) == int(pinned_now.timestamp())
    # 60-minute default lookback
    assert int(params["end"]) - int(params["start"]) == 60 * 60


@respx.mock
@pytest.mark.asyncio
async def test_fetch_events_returns_empty_on_api_error() -> None:
    respx.get(f"{API_BASE}/api/v1/events").mock(
        return_value=httpx.Response(500, json={"errors": ["x"]})
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    refs = [r async for r in connector.fetch_events(cfg, SECRETS)]
    assert refs == []


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_sleeps_and_retries() -> None:
    """429 with X-RateLimit-Reset triggers a sleep, then a retry."""
    rate_limited = httpx.Response(
        429,
        json={"errors": ["Rate limit exceeded"]},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0.05"},
    )
    success = httpx.Response(200, json={"valid": True})
    route = respx.get(f"{API_BASE}/api/v1/validate").mock(side_effect=[rate_limited, success])

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    with patch("omniscience_connectors.datadog.connector.asyncio.sleep", _fake_sleep):
        await connector.validate(cfg, SECRETS)

    assert route.call_count == 2
    assert len(sleep_calls) == 1
    # We should have slept ~0.05s (capped at MAX_RATE_LIMIT_SLEEP_SECONDS).
    assert 0.0 <= sleep_calls[0] <= 1.0


@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_no_retry_when_remaining_nonzero() -> None:
    """A 429 without X-RateLimit-Remaining=0 is surfaced (no infinite loop)."""
    route = respx.get(f"{API_BASE}/api/v1/validate").mock(
        return_value=httpx.Response(
            429, json={"errors": ["x"]}, headers={"X-RateLimit-Remaining": "5"}
        )
    )
    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    with pytest.raises(RuntimeError):
        await connector.validate(cfg, SECRETS)
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_datadog_connector_is_registered() -> None:
    connector = get_connector("datadog")
    assert isinstance(connector, DatadogConnector)


# ---------------------------------------------------------------------------
# End-to-end integration test — full discover + fetch using cassettes
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_integration_full_discover_then_fetch_with_cassettes() -> None:
    """Walk discover() and fetch() for every kind using the VCR cassettes.

    This is the "integration test with VCR cassettes" required by issue #240
    acceptance criteria.  It exercises every public code path against
    deterministic recorded JSON payloads.
    """
    _wire_full_discover()
    respx.get(f"{API_BASE}/api/v1/monitor/1001").mock(
        return_value=httpx.Response(200, json=_load_cassette("monitor_1001.json"))
    )
    respx.get(f"{API_BASE}/api/v1/dashboard/abc-123-xyz").mock(
        return_value=httpx.Response(200, json=_load_cassette("dashboard_abc.json"))
    )
    # Service + SLO fetches for the first item of each list cassette.
    svc_cassette = _load_cassette("services_page_0.json")
    respx.get(f"{API_BASE}/api/v2/services/definitions/checkout").mock(
        return_value=httpx.Response(200, json={"data": svc_cassette["data"][0]})
    )
    slo_cassette = _load_cassette("slos_page_0.json")
    respx.get(f"{API_BASE}/api/v1/slo/slo-checkout-latency").mock(
        return_value=httpx.Response(200, json={"data": slo_cassette["data"][0]})
    )
    respx.get(f"{API_BASE}/api/v1/events").mock(
        return_value=httpx.Response(200, json=_load_cassette("events_window.json"))
    )

    connector = DatadogConnector()
    cfg = DatadogConfig(org_uid=ORG)
    refs = [r async for r in connector.discover(cfg, SECRETS)]
    assert len(refs) == 8  # 2 monitors + 2 dashboards + 2 services + 2 SLOs

    # Fetch one of each kind end-to-end
    monitor_ref = next(
        r for r in refs if r.metadata["kind"] == "monitor" and r.metadata["monitor_id"] == 1001
    )
    dashboard_ref = next(
        r
        for r in refs
        if r.metadata["kind"] == "dashboard" and r.metadata["dashboard_id"] == "abc-123-xyz"
    )
    service_ref = next(
        r for r in refs if r.metadata["kind"] == "service" and r.metadata["service"] == "checkout"
    )
    slo_ref = next(
        r
        for r in refs
        if r.metadata["kind"] == "slo" and r.metadata["slo_id"] == "slo-checkout-latency"
    )

    for r in (monitor_ref, dashboard_ref, service_ref, slo_ref):
        doc = await connector.fetch(cfg, SECRETS, r)
        assert doc.content_type == "text/markdown"
        assert len(doc.content_bytes) > 0

    # Bitemporal events sync — three events all linked to monitor 1001
    pinned_now = datetime(2026, 5, 22, 18, 0, 0, tzinfo=UTC)
    events = [r async for r in connector.fetch_events(cfg, SECRETS, now=pinned_now)]
    assert len(events) == 3
    assert all(e.metadata["monitor_id"] == 1001 for e in events)
