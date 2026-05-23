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

## 2026-05-23 — senior-backend-engineer — Issue #231: Runbook parser + suggest

**What**: Shipped the runbook connector (markdown parser + tiny YAML front-matter parser + matcher), the `suggest_runbook` MCP tool, and `POST /api/v1/incidents/{id}/runbook-step` REST endpoint. Step events are recorded with the full bitemporal triple (`valid_from`, `valid_to`, `recorded_at`) into an in-memory ring buffer plus a structured-log event plus a Prometheus counter. Confidence scoring is a weighted sum (exact 0.55, pattern 0.25, tags 0.15, severity 0.05) — monotonicity invariant asserted by integration test.

**Files added**:
- `packages/connectors/src/omniscience_connectors/runbook/{__init__.py,models.py,parser.py,linker.py,connector.py}`
- `apps/server/src/omniscience_server/runbook.py`
- `apps/server/src/omniscience_server/rest/runbook.py`
- `tests/test_runbook_connector.py`
- `tests/integration/test_runbook_suggest.py`
- `tests/fixtures/runbook/{db-connections-exhausted,redis-evictions,generic-5xx,malformed}.md`
- `docs/runbooks/runbook-connector.md`

**Files modified** (additive only):
- `packages/connectors/src/omniscience_connectors/__init__.py` — registered `RunbookConnector`; exported config + connector.
- `apps/server/src/omniscience_server/rest/router.py` — included `runbook_router`.
- `apps/server/src/omniscience_server/mcp/server.py` — registered `suggest_runbook` MCP tool.

**Outcome**: 38 new tests passing (0.09s for runbook-only run; 39 with the unrelated benchmark runbook test). `mypy --strict` clean across 218 files. `ruff` + `ruff format` clean. 142 connector regression tests + 67 incident/replay/blast-radius regression tests still green. Branch `feat/231-runbook-parser`.

**Tags**: backend, connector, runbook, parser, incidents, bitemporal, mcp, rest, issue-231, wave-2
