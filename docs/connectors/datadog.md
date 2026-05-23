# Datadog connector (`datadog`)

The `datadog` connector indexes the four most useful Datadog configuration
surfaces — **monitors**, **dashboards**, **service catalog**, and **SLOs** —
plus the **event stream** (audit + alert state changes) as first-class
bitemporal entities. Together with the existing push-only
[`alerts`](./alerts.md) connector it completes the alert -> graph causal chain
for the most common observability source in the target ICP.

| Connector | Mode | Domain |
|-----------|------|--------|
| `alerts` | push (webhook) | Live alert firings (PagerDuty + Datadog) |
| `datadog` | pull (API) | Monitor / dashboard / service catalog / SLO config + event stream |
| `otel` | pull (OTLP receiver) | Spans + traces |

The three connectors run **side-by-side** as separate `Source` rows.

## Why a separate connector?

The `alerts` connector handles *live* webhooks but cannot enumerate the
configuration surface (monitor queries, threshold definitions, service
ownership, SLO targets). Post-mortem reconstruction (#232) and blast-radius
(#234) both need to know **which monitors existed**, **what they targeted**,
and **what events fired** during an incident — none of which is reachable
from webhooks alone. This connector fills that gap.

## Authentication

Two secrets are required, resolved at call time from the per-source secrets
store. Tokens are **never logged**.

| Secret | Required | Notes |
|--------|----------|-------|
| `datadog_api_key` | yes | Datadog API key (`DD-API-KEY`) |
| `datadog_app_key` | yes | Datadog Application key (`DD-APPLICATION-KEY`) with read scopes for monitors, dashboards, service catalog, SLOs, and events |

### Required Application key scopes

Generate the Application key from
**Organization Settings -> Application Keys** and assign the following
fine-grained scopes (Datadog UI naming):

| Scope | Purpose |
|-------|---------|
| `monitors_read` | `GET /api/v1/monitor` and `GET /api/v1/monitor/{id}` |
| `dashboards_read` | `GET /api/v1/dashboard` and `GET /api/v1/dashboard/{id}` |
| `apm_service_catalog_read` | `GET /api/v2/services/definitions` |
| `slos_read` | `GET /api/v1/slo` and `GET /api/v1/slo/{id}` |
| `events_read` | `GET /api/v1/events` and `GET /api/v1/events/{id}` |

If you use legacy unscoped Application keys, all of the above is granted
implicitly — but we recommend the scoped form for least-privilege.

### Workspace scoping

Datadog API keys are organisation-scoped. An Omniscience workspace pins
exactly one `(org_uid, site)` pair via `DatadogConfig`. To ingest multiple
Datadog orgs into a single workspace, create one `Source` per org.

The `org_uid` is the **short identifier** you choose to namespace canonical
entity names (e.g. `acme-prod`, `team-platform`). It is **not** the Datadog
internal org ID — anything matching `[A-Za-z0-9][A-Za-z0-9._-]{0,62}` works.

## Configuration

```python
from omniscience_connectors import DatadogConfig

config = DatadogConfig(
    org_uid="acme-prod",
    site="datadoghq.com",         # default; use "datadoghq.eu" for EU
    include_monitors=True,        # default True
    include_dashboards=True,      # default True
    include_service_catalog=True, # default True
    include_slos=True,            # default True
    events_lookback_minutes=60,   # default 60
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `org_uid` | `str` | (required) | Short slug used to namespace canonical entity names |
| `site` | `str` | `datadoghq.com` | Datadog site (`datadoghq.com`, `datadoghq.eu`, `us3.datadoghq.com`, ...) |
| `include_monitors` | `bool` | `True` | Enumerate monitors during `discover()` |
| `include_dashboards` | `bool` | `True` | Enumerate dashboards during `discover()` |
| `include_service_catalog` | `bool` | `True` | Enumerate service catalog entries |
| `include_slos` | `bool` | `True` | Enumerate SLOs |
| `events_lookback_minutes` | `int` | `60` | Events sync window |
| `api_base_url` | `str \| None` | `None` | Override the API base (testing only) |

## Entities and edges emitted

| Entity kind | Canonical `name` |
|-------------|------------------|
| `DatadogMonitor` | `datadog://monitor/{org_uid}/{monitor_id}` |
| `DatadogDashboard` | `datadog://dashboard/{org_uid}/{dashboard_id}` |
| `DatadogService` | `datadog://service/{org_uid}/{service}` |
| `DatadogSLO` | `datadog://slo/{org_uid}/{slo_id}` |
| `DatadogEvent` | `datadog://event/{org_uid}/{event_id}` |

| Edge | Direction | Source of truth |
|------|-----------|-----------------|
| `[:TARGETS]` | monitor -> service | `service:X` tag and `service:X` clause in query |
| `[:TARGETS]` | monitor -> host | `host:X` tag and `host:X` clause in query |
| `[:TARGETS]` | monitor -> pod | `pod_name:X` tag and `pod_name:X` clause in query |
| `[:OBSERVES]` | dashboard -> service | Widget `requests[].q` and `service` template variable defaults |
| `[:CORRELATES]` | event -> monitor | Event payload's `monitor_id` field |

The host and pod entity names are deliberately **org-independent** so the
same physical resource can be targeted by monitors across multiple orgs (or
correlated to OTel spans by the existing `canonical_pod_name` helper).

### Canonical-name hard contracts

Each kind has an anchored regex exported from
`omniscience_connectors.datadog`:

```
^datadog://monitor/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/(\d+)$
^datadog://dashboard/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/([A-Za-z0-9_-]+)$
^datadog://service/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/([A-Za-z0-9_.\-/]+)$
^datadog://slo/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/([A-Za-z0-9_-]+)$
^datadog://event/([A-Za-z0-9][A-Za-z0-9._-]{0,62})/(\d+)$
```

Slack / email mention extractors canonicalise UI URLs
(`https://app.datadoghq.com/monitors/{id}`) into these same forms so
`EntityLinker.exact_name` produces `cross_ref` edges without any application-
level join.

## Bitemporal semantics

`DocumentRef.updated_at` is set from:

* **Monitors / dashboards / SLOs**: the upstream `modified` / `modified_at`
  ISO-8601 field.
* **Events**: `date_happened` (epoch seconds), coerced to UTC.

`fetch_events()` syncs the window `[now - events_lookback_minutes, now]`.
Event IDs are stable across overlapping windows, so re-runs are idempotent
even when the ingestion cron and the lookback window are configured
asymmetrically.

## Rate limiting

Datadog's default tier allows ~600 requests/minute with per-endpoint family
quotas exposed via:

* `X-RateLimit-Remaining`
* `X-RateLimit-Reset` (**seconds until reset**, not a future epoch — note
  the divergence from GitHub's epoch-based header)
* `X-RateLimit-Period` (window length, informational)

The connector sleeps until reset on 429 responses with
`X-RateLimit-Remaining: 0`, capped at one hour as a defence against bogus
reset values. Up to three retry cycles per request. All other 4xx errors
bubble up to the caller.

## Webhook handler

The Datadog connector intentionally returns `None` from
`webhook_handler()` — live alert webhooks are owned by the
[`alerts`](./alerts.md) connector. Keeping the API-pull and webhook-push
paths in separate connectors lets each evolve its rate-limit, auth, and
signature-verification story independently.

## Non-goals

* No metric-query indexing. Querying Datadog metrics live during retrieval
  is a separate "live query" surface tracked under #234.
* No log-search ingestion. Log search via the Datadog Logs API is a
  follow-up connector (`datadog_logs`).
* No `synthetics` / `incidents` / `notebooks` in this iteration; the
  configuration surface intentionally scopes to the five Wave-1 entity
  kinds.
* No write operations. Read-only by design — a single `monitor_update`
  endpoint would balloon the required token scopes and introduce a tenancy
  hazard.

## Testing

```bash
uv run pytest tests/ -k datadog -v
```

VCR-style cassettes are JSON fixtures under `tests/fixtures/datadog/`,
replayed through `respx`. To re-record (manual process pending #245):

1. Run discovery against a real Datadog test org with `RECORD=1`.
2. Sanitize cassettes (no real `monitor_id` collisions, no `creator.email`
   leaks).
3. Commit the JSON updates alongside the test.
