# Project History

## 2026-05-23 — senior-backend-engineer — Issue #240: Datadog connector

**What**: Implemented first-class Datadog connector (monitors, dashboards, service catalog, SLOs, events) with bitemporal event sync and rate-limit handling.

**Files added**:
- `packages/connectors/src/omniscience_connectors/datadog/__init__.py`
- `packages/connectors/src/omniscience_connectors/datadog/connector.py`
- `tests/test_datadog_connector.py`
- `tests/fixtures/datadog/{monitors_page_0,monitor_1001,dashboards_page_0,dashboard_abc,services_page_0,slos_page_0,events_window,event_9000001}.json`
- `docs/connectors/datadog.md`

**Files modified** (additive only):
- `packages/connectors/src/omniscience_connectors/__init__.py` — registered `DatadogConnector`, exported `DatadogConfig`/`DatadogConnector`.

**Outcome**: 44 tests passing (0.48s). `mypy --strict` clean; `ruff` + `ruff format` clean. Existing 142 connector tests still green. Branch `feat/240-datadog-connector` opened against `main`.

**Tags**: backend, connector, datadog, observability, bitemporal, issue-240, wave-3
