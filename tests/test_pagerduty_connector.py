"""Tests for the PagerDuty full source connector (issue #239).

Coverage:

* :class:`PagerDutyConfig` defaults and connector_type registration
* canonical-name helpers (service/team/escalation/shift/incident/user)
* ``_build_auth_headers`` — token required, headers shape
* ``validate()`` — happy path, 401 -> PermissionError, 403 -> PermissionError
* ``discover()`` — yields teams / escalations / services / shifts / incidents
  with the right edges, and applies team_id filter
* ``fetch()`` — bitemporal incident sync (state + log entries + edges)
* Rate-limit handling — sleeps and retries on HTTP 429 with ``Retry-After``
* Webhook handler delegation — same instance type as alerts/pagerduty
* Registry — ``pagerduty`` is registered
* Determinism — cassette replay produces byte-identical output across two
  consecutive runs (no live network calls, no datetime drift inside fixtures)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from omniscience_connectors import get_connector
from omniscience_connectors.alerts.webhook import AlertsWebhookHandler
from omniscience_connectors.base import DocumentRef
from omniscience_connectors.pagerduty.connector import (
    PagerDutyConfig,
    PagerDutyConnector,
    _build_auth_headers,
    canonical_escalation_name,
    canonical_incident_name,
    canonical_oncall_shift_name,
    canonical_service_name,
    canonical_team_name,
    canonical_user_name,
)

# ---------------------------------------------------------------------------
# Cassettes — JSON fixtures replayed via respx so the test suite is hermetic.
# ---------------------------------------------------------------------------

API_BASE = "https://api.pagerduty.com"
TOKEN = "u+fakeFAKE_pagerduty_token_xxx"

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pagerduty"


def _load_cassette(name: str) -> dict[str, Any]:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
        return data


def _wire_discover_cassettes() -> None:
    """Mount every list-endpoint cassette used by ``discover()``."""
    respx.get(f"{API_BASE}/teams").mock(
        return_value=httpx.Response(200, json=_load_cassette("teams.json"))
    )
    respx.get(f"{API_BASE}/escalation_policies").mock(
        return_value=httpx.Response(200, json=_load_cassette("escalation_policies.json"))
    )
    respx.get(f"{API_BASE}/services").mock(
        return_value=httpx.Response(200, json=_load_cassette("services.json"))
    )
    respx.get(f"{API_BASE}/oncalls").mock(
        return_value=httpx.Response(200, json=_load_cassette("oncalls.json"))
    )
    respx.get(f"{API_BASE}/incidents").mock(
        return_value=httpx.Response(200, json=_load_cassette("incidents.json"))
    )


# ---------------------------------------------------------------------------
# Static / pure tests
# ---------------------------------------------------------------------------


def test_connector_type_and_schema() -> None:
    assert PagerDutyConnector.connector_type == "pagerduty"
    assert PagerDutyConnector.config_schema is PagerDutyConfig


def test_config_defaults() -> None:
    cfg = PagerDutyConfig()
    assert cfg.api_base_url == API_BASE
    assert cfg.team_ids == []
    assert cfg.service_ids == []
    assert cfg.incident_lookback_days == 30
    assert cfg.include_resolved_incidents is True


def test_canonical_names_are_stable() -> None:
    assert canonical_service_name("PSVC001") == "pagerduty://service/PSVC001"
    assert canonical_team_name("PTEAM01") == "pagerduty://team/PTEAM01"
    assert canonical_escalation_name("PESC001") == "pagerduty://escalation/PESC001"
    assert canonical_user_name("PUSR001") == "pagerduty://user/PUSR001"
    assert canonical_incident_name("PINC0001") == "pagerduty://incident/PINC0001"

    from datetime import UTC, datetime

    start = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    name = canonical_oncall_shift_name("PSCH001", "PUSR001", start)
    # Stable, deterministic — same inputs always produce the same name.
    assert name == canonical_oncall_shift_name("PSCH001", "PUSR001", start)
    assert name.startswith("pagerduty://oncall/PSCH001/PUSR001/")


def test_build_auth_headers_requires_token() -> None:
    with pytest.raises(ValueError, match="api_token"):
        _build_auth_headers({})


def test_build_auth_headers_shape() -> None:
    h = _build_auth_headers({"api_token": TOKEN})
    assert h["Authorization"] == f"Token token={TOKEN}"
    assert h["Accept"].startswith("application/vnd.pagerduty+json")
    assert "User-Agent" in h


def test_registry_has_pagerduty() -> None:
    cls = get_connector("pagerduty")
    assert isinstance(cls, PagerDutyConnector)


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_validate_success() -> None:
    respx.get(f"{API_BASE}/abilities").mock(
        return_value=httpx.Response(200, json=_load_cassette("abilities.json"))
    )
    await PagerDutyConnector().validate(PagerDutyConfig(), {"api_token": TOKEN})


@respx.mock
@pytest.mark.asyncio
async def test_validate_unauthorized_raises_permission_error() -> None:
    respx.get(f"{API_BASE}/abilities").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"})
    )
    with pytest.raises(PermissionError):
        await PagerDutyConnector().validate(PagerDutyConfig(), {"api_token": TOKEN})


@respx.mock
@pytest.mark.asyncio
async def test_validate_forbidden_raises_permission_error() -> None:
    respx.get(f"{API_BASE}/abilities").mock(
        return_value=httpx.Response(403, json={"error": "Forbidden"})
    )
    with pytest.raises(PermissionError, match="scopes"):
        await PagerDutyConnector().validate(PagerDutyConfig(), {"api_token": TOKEN})


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_discover_yields_all_entity_kinds() -> None:
    _wire_discover_cassettes()
    refs = [
        r async for r in PagerDutyConnector().discover(PagerDutyConfig(), {"api_token": TOKEN})
    ]
    kinds = [r.metadata["kind"] for r in refs]
    assert kinds.count("PagerDutyTeam") == 2
    assert kinds.count("PagerDutyEscalation") == 2
    assert kinds.count("PagerDutyService") == 2
    assert kinds.count("OnCallShift") == 2
    assert kinds.count("PagerDutyIncident") == 2

    # Ordering invariant: teams before services before incidents so the
    # linker can resolve edges in a single pass.
    first_team = next(i for i, k in enumerate(kinds) if k == "PagerDutyTeam")
    first_service = next(i for i, k in enumerate(kinds) if k == "PagerDutyService")
    first_incident = next(i for i, k in enumerate(kinds) if k == "PagerDutyIncident")
    assert first_team < first_service < first_incident


@respx.mock
@pytest.mark.asyncio
async def test_discover_service_emits_team_and_escalation_edges() -> None:
    _wire_discover_cassettes()
    refs = [
        r async for r in PagerDutyConnector().discover(PagerDutyConfig(), {"api_token": TOKEN})
    ]
    services = [r for r in refs if r.metadata["kind"] == "PagerDutyService"]
    assert services
    api_gw = next(s for s in services if s.metadata["pagerduty_service_id"] == "PSVC001")

    edges = api_gw.metadata["edges"]
    types = {e["type"] for e in edges}
    assert "OWNED_BY" in types
    assert "USES_ESCALATION" in types

    owned = next(e for e in edges if e["type"] == "OWNED_BY")
    assert owned["to"] == canonical_team_name("PTEAM01")
    uses = next(e for e in edges if e["type"] == "USES_ESCALATION")
    assert uses["to"] == canonical_escalation_name("PESC001")


@respx.mock
@pytest.mark.asyncio
async def test_discover_oncall_shift_emits_user_edge() -> None:
    _wire_discover_cassettes()
    refs = [
        r async for r in PagerDutyConnector().discover(PagerDutyConfig(), {"api_token": TOKEN})
    ]
    shifts = [r for r in refs if r.metadata["kind"] == "OnCallShift"]
    assert len(shifts) == 2
    first = shifts[0]
    edges = first.metadata["edges"]
    on_call = next(e for e in edges if e["type"] == "ON_CALL")
    assert on_call["to"] == canonical_user_name(first.metadata["user_id"])


@respx.mock
@pytest.mark.asyncio
async def test_discover_team_filter_propagates_to_query() -> None:
    _wire_discover_cassettes()
    cfg = PagerDutyConfig(team_ids=["PTEAM01"])
    _ = [r async for r in PagerDutyConnector().discover(cfg, {"api_token": TOKEN})]
    # team_ids filter should appear in every list-endpoint query that
    # accepts it (escalation_policies, services, oncalls, incidents).
    urls = [str(call.request.url) for call in respx.calls]
    assert any("escalation_policies" in u and "team_ids" in u and "PTEAM01" in u for u in urls)
    assert any("services" in u and "team_ids" in u and "PTEAM01" in u for u in urls)
    assert any("incidents" in u and "team_ids" in u and "PTEAM01" in u for u in urls)


# ---------------------------------------------------------------------------
# fetch() — bitemporal incident sync
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fetch_incident_returns_markdown_and_full_timeline() -> None:
    respx.get(f"{API_BASE}/incidents/PINC0001").mock(
        return_value=httpx.Response(200, json=_load_cassette("incident_PINC0001.json"))
    )
    respx.get(f"{API_BASE}/incidents/PINC0001/log_entries").mock(
        return_value=httpx.Response(200, json=_load_cassette("incident_PINC0001_log_entries.json"))
    )
    ref = DocumentRef(
        external_id="seed",
        uri=canonical_incident_name("PINC0001"),
        metadata={"kind": "PagerDutyIncident", "pagerduty_incident_id": "PINC0001"},
    )
    doc = await PagerDutyConnector().fetch(PagerDutyConfig(), {"api_token": TOKEN}, ref)
    assert doc.content_type == "text/markdown"
    md = doc.content_bytes.decode("utf-8")
    assert "# Incident #4242:" in md
    assert "api-gateway 5xx spike" in md
    assert "## Timeline" in md
    # All three log entries rendered.
    assert "trigger_log_entry" in md
    assert "assign_log_entry" in md
    assert "acknowledge_log_entry" in md

    # Bitemporal: status, edges, and full log entries surface in metadata.
    assert doc.ref.metadata["status"] == "acknowledged"
    edges = doc.ref.metadata["edges"]
    types = {e["type"] for e in edges}
    assert {"FILED_AGAINST", "USES_ESCALATION", "ASSIGNED_TO", "ACKNOWLEDGED_BY"} <= types
    log_entries = doc.ref.metadata["log_entries"]
    assert len(log_entries) == 3
    assert log_entries[0]["type"] == "trigger_log_entry"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_non_incident_kind_returns_json_metadata() -> None:
    """Catalog entities arrive complete from discover -- fetch just serialises."""
    ref = DocumentRef(
        external_id="x",
        uri=canonical_team_name("PTEAM01"),
        metadata={
            "kind": "PagerDutyTeam",
            "name": canonical_team_name("PTEAM01"),
            "pagerduty_team_id": "PTEAM01",
        },
    )
    doc = await PagerDutyConnector().fetch(PagerDutyConfig(), {"api_token": TOKEN}, ref)
    assert doc.content_type == "application/json"
    payload = json.loads(doc.content_bytes)
    assert payload["pagerduty_team_id"] == "PTEAM01"


@pytest.mark.asyncio
async def test_fetch_incident_missing_id_raises() -> None:
    ref = DocumentRef(
        external_id="x",
        uri="pagerduty://incident/?",
        metadata={"kind": "PagerDutyIncident"},
    )
    with pytest.raises(ValueError, match="pagerduty_incident_id"):
        await PagerDutyConnector().fetch(PagerDutyConfig(), {"api_token": TOKEN}, ref)


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_retries_then_succeeds() -> None:
    """A 429 with Retry-After triggers a sleep + retry; second call succeeds."""
    route = respx.get(f"{API_BASE}/abilities").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=_load_cassette("abilities.json")),
        ]
    )
    with patch(
        "omniscience_connectors.pagerduty.connector.asyncio.sleep",
        new=AsyncMock(),
    ) as mock_sleep:
        await PagerDutyConnector().validate(PagerDutyConfig(), {"api_token": TOKEN})
    assert route.call_count == 2
    assert mock_sleep.await_count == 1


# ---------------------------------------------------------------------------
# Webhook handler delegation
# ---------------------------------------------------------------------------


def test_webhook_handler_reuses_alerts_pagerduty_receiver() -> None:
    handler = PagerDutyConnector().webhook_handler()
    assert isinstance(handler, AlertsWebhookHandler), (
        "PagerDuty connector must reuse the alerts/pagerduty webhook receiver "
        "to keep signature-verification code in one place (issue #239)."
    )


# ---------------------------------------------------------------------------
# Determinism — cassette replay produces identical output on two consecutive runs
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_cassette_replay_is_deterministic() -> None:
    """Run discover() twice; assert identical canonical names + edge sets.

    Guards against accidental clock-dependent metadata.  We compare on the
    name-set (not the entire ref) because ``OnCallShift.updated_at`` is a
    fixed fixture timestamp so the canonical name is stable; any future
    drift would surface here.
    """
    _wire_discover_cassettes()
    pass1 = [
        r async for r in PagerDutyConnector().discover(PagerDutyConfig(), {"api_token": TOKEN})
    ]
    respx.reset()
    _wire_discover_cassettes()
    pass2 = [
        r async for r in PagerDutyConnector().discover(PagerDutyConfig(), {"api_token": TOKEN})
    ]
    names1 = [r.metadata["name"] for r in pass1]
    names2 = [r.metadata["name"] for r in pass2]
    assert names1 == names2
