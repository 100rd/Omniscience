# Retention worker runbook

Operational runbook for the retention worker (issue #135) and the
retention dashboard / alert path (issue #136). Source of truth for the
contracts referenced here is **[ADR-0009](../decisions/0009-retention-tiering-policy.md)**.

## Overview

The retention worker evicts data across the **hot → warm → archive**
tiers per ADR-0009 §1. It runs in-process on the FastAPI lifespan with
a default 6-hour tick interval, processes one workspace at a time, and
follows the read-then-mark-then-move pattern (ADR-0009 §3) so that a
crash mid-tick leaves a re-runnable state.

**Steady-state SLO**: lag stays below 24 hours (ADR-0009 §8). Lag > 7d
trips a P1 alert via the same alert path freshness uses.

## Dashboard

The Grafana dashboard `retention.json`
(`monitoring/grafana/dashboards/retention.json`) renders five panels:

1. **Records by tier (hot / warm / archive)** — stacked area on
   `omniscience_graph_records_total`.
2. **Eviction rate (5m window)** — `rate(...)` on
   `omniscience_retention_eviction_total`.
3. **Worker phase latency (p50 / p95 / p99)** —
   `histogram_quantile(...)` on
   `omniscience_retention_worker_duration_seconds`.
4. **Retention worker lag** — time series of
   `omniscience_retention_worker_lag_seconds` with the 24h warning and
   7d P1 thresholds drawn.
5. **Top-10 (tier × store) by record count** — partition table off
   `omniscience_graph_records_total`.

The admin UI deep-dive lives at `/retention` and surfaces the same
metrics scoped to the caller's workspace, plus the per-tenant table,
**Run now** action, and **Dry run report** action.

## Alerts

`monitoring/prometheus/alerts/retention.yaml` ships four rules.

### RetentionWorkerLagWarning (severity=warning)

`omniscience_retention_worker_lag_seconds > 86400` for 5m.

**Response**:

1. Open the retention dashboard, confirm the lag panel shows the
   sustained breach.
2. Check the **Worker phase latency** panel — a p99 spike on
   `phase="move"` typically means the eligibility query lost its
   index pin (most likely cause: the
   `(workspace_id, recorded_at)` composite index from ADR-0008 hasn't
   backfilled). Verify with `cypher-shell` or the Neo4j console.
3. If the p99 is normal but lag is rising, check whether ingestion
   has accelerated faster than the worker can keep up. The worker
   processes at most `RETENTION_BATCH_SIZE` (default 500) rows per
   transaction — raise via Helm
   (`omniscience.retention.batchSize`) if the v0.5 envelope has
   grown.
4. Use the admin UI’s **Run now** button on `/retention` to trigger
   an immediate tick scoped to the affected workspace; the dashboard
   refresh shows the new lag value within 60s.

### RetentionWorkerLagCritical (severity=critical, P1)

`omniscience_retention_worker_lag_seconds > 604800` for 5m.

**Response**: identical to the warning runbook, plus:

5. Page the on-call platform engineer (P1 escalation path — same as
   freshness P1).
6. If a sustained breach persists past 30min, consider stopping the
   ingestion worker temporarily so the retention worker can catch
   up; this is documented in ADR-0009 §9 ("manageable: a backlog of
   unmoved-archive records does not affect query correctness").

### RetentionWorkerStalled (severity=critical)

`max(increase(omniscience_retention_worker_duration_seconds_count[12h])) == 0`
for 5m.

**Response**:

1. Inspect FastAPI process logs for a `retention_worker_error`
   structlog event. The worker traps exceptions in `start()` so the
   lifespan loop continues; a sustained stall usually means the
   ingestion / freshness workers crashed too (process-wide), or a
   pathological asyncio gather is blocking the loop.
2. If the FastAPI process itself is healthy but the worker is
   silent, restart the server replicas. ADR-0009 §9 crash runbook:
   *"the next scheduled run resumes from idempotency"* — there's no
   data corruption to repair.
3. After restart, confirm `omniscience_retention_worker_duration_seconds_count`
   begins incrementing within one tick interval (default 6h, but
   the first run executes immediately on lifespan start).

### RetentionEvictionInconsistent (severity=warning)

`abs(rate(... store="neo4j"[1h]) - rate(... store="qdrant"[1h])) > 0.1`
for 1h.

**Response** (ADR-0009 §9 cross-store consistency runbook):

1. Run the worker once via `/api/v1/admin/retention/run-now` (or the
   admin UI button). A transient Qdrant outage leaves marked-not-
   moved chunks; the next run fixes the divergence.
2. If the divergence persists, check Qdrant's payload-index health:
   `omniscience_chunks` collection should expose payload indexes on
   `tier` and `workspace_id`. A missing index makes the warm filter
   evaluate via full scan, which times out the move phase.
3. As a last resort, escalate to a per-workspace re-sync from the
   Postgres `chunks` table — the canonical source of vector lineage
   per ADR-0006 §implementation-notes. This is a cold-restart shape;
   coordinate with the platform team before kicking off.

## Manual operations

### Inspect would-evict counts (no mutation)

```
GET /api/v1/admin/retention/report
Authorization: Bearer <token-with-stats:read>
```

Returns the same dry-run report the admin UI's **Dry run report**
button shows. Side-effect free regardless of operator config.

### Trigger a single tick

```
POST /api/v1/admin/retention/run-now
Authorization: Bearer <token-with-stats:read>
```

Scoped to the caller's workspace. Returns 202 Accepted with a
server-generated `run_id`. The tick executes synchronously inside the
request handler.

### Per-tenant counts + lag

```
GET /api/v1/admin/retention/status
Authorization: Bearer <token-with-stats:read>
```

Cheap to call (reads the same gauges the worker writes on every
tick). The admin UI polls this endpoint every 30s.

## Scope

All three admin endpoints require the **`stats:read`** scope (matches
the rest of the admin read surface). `admin` tokens satisfy via the
scope hierarchy. Requests from tokens without an associated workspace
fail closed with 403.

## Related

- ADR-0009 §8 — observability + SLOs
- ADR-0009 §9 — failure modes
- `docs/freshness-and-lineage.md` — freshness alert path (parallel)
- Issue [#135](https://github.com/100rd/Omniscience/issues/135) — worker
- Issue [#136](https://github.com/100rd/Omniscience/issues/136) —
  dashboard + alerts + admin UI
