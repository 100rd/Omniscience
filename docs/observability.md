# Observability

Index of dashboards, alerts, runbooks, and Prometheus metric families
exposed by Omniscience.

## Metric families

The server exposes Prometheus metrics on `/metrics`. The current set:

| Family | Type | Labels | Source |
|---|---|---|---|
| `omniscience_http_requests_total` | counter | `method`, `path`, `status_code` | TracingMiddleware |
| `omniscience_http_request_duration_seconds` | histogram | `method`, `path` | TracingMiddleware |
| `omniscience_http_requests_in_progress` | gauge | `method`, `path` | TracingMiddleware |
| `omniscience_source_freshness_age_seconds` | gauge | `source_id`, `source_name` | FreshnessWorker (Issue #112) |
| `omniscience_source_stale_total` | gauge | — | FreshnessWorker |
| `omniscience_scheduler_syncs_triggered_total` | counter | `source_type` | SchedulerWorker |
| `omniscience_scheduler_check_duration_seconds` | histogram | — | SchedulerWorker |
| `omniscience_graph_records_total` | gauge | `tier`, `store` | RetentionWorker (Issue #135 / ADR-0009 §8) |
| `omniscience_retention_eviction_total` | counter | `transition`, `store` | RetentionWorker |
| `omniscience_retention_worker_duration_seconds` | histogram | `phase`, `store` | RetentionWorker |
| `omniscience_retention_worker_lag_seconds` | gauge | — | RetentionWorker |

## Dashboards

Provisioned via `monitoring/grafana/dashboards/`:

| File | UID | Coverage |
|---|---|---|
| `freshness.json` | `omniscience-freshness-slos` | Source freshness SLO (Issue #112) |
| `retention.json` | `omniscience-retention-slos` | Retention worker per-tier counts, eviction throughput, phase latency, lag SLO, top-N partitions (Issue #136 / ADR-0009 §8) |

## Alerts

Provisioned via `monitoring/prometheus/alerts/`:

| File | Rules |
|---|---|
| `retention.yaml` | `RetentionWorkerLagWarning` (warning, 24h), `RetentionWorkerLagCritical` (critical, 7d/P1), `RetentionWorkerStalled` (critical, 12h no-tick), `RetentionEvictionInconsistent` (warning, cross-store divergence) |

## Runbooks

| Doc | Coverage |
|---|---|
| `runbooks/retention.md` | Retention worker incident response — alert routing, dashboard interpretation, manual operations (run-now, dry-run report) (Issue #136) |

## Admin UI

The admin app (`apps/admin`) ships with two operator-facing pages tied
to the metric families above:

- `/freshness` — per-source freshness deep dive (Issue #112).
- `/retention` — retention status (counts + lag SLO), per-tenant
  table, **Run now** action, **Dry run report** action (Issue #136).

The Dashboard home (`/`) embeds the **Retention** panel as a card with
a link to `/retention` for the deep dive.

## Related

- [ADR-0009 §8](decisions/0009-retention-tiering-policy.md#8-observability-and-slos) — retention SLOs
- [`docs/freshness-and-lineage.md`](freshness-and-lineage.md) — freshness SLO contract
- Prometheus scrape configuration: `monitoring/prometheus.yml`
