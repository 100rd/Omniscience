# ADR 0009 — Retention tiering policy and storage layout

- **Status**: Proposed
- **Date**: 2026-04-24
- **Amends**: [ADR-0005](0005-neo4j-as-graph-store.md) §Negative-operational (graph footprint posture, retention is the dominant cost driver), [ADR-0006](0006-qdrant-as-vector-store.md) §Deployment-posture (Qdrant retention mirror)

## Implementation notes

Placeholder. This ADR flips to `Implemented` when the Wave 6 closer ([#139](https://github.com/100rd/Omniscience/issues/139)) lands — i.e. when the retention worker ([#135](https://github.com/100rd/Omniscience/issues/135)), retention metrics ([#136](https://github.com/100rd/Omniscience/issues/136)), and bitemporal contract tests ([#138](https://github.com/100rd/Omniscience/issues/138)) all merge and the property-test gate is green for at least one full ingestion cycle on a representative fixture.

## Context

Vision §5.3 ([`docs/vision.md`](../vision.md) line 76) commits Omniscience to a tiered retention posture:

> Retention: 90 days hot (queryable at full fidelity), 1 year warm (queryable at snapshot granularity), archive beyond.

Vision §11 (line 177) qualifies the temporal model:

> Bitemporal semantics are powerful but expensive to query correctly. Initial implementation may restrict time-travel queries to a subset of entity types.

Two ADRs already in flight observe that retention is the dominant cost shape on each side of the storage split:

- [ADR-0005](0005-neo4j-as-graph-store.md) §Negative-operational: *"Disk footprint grows with temporal retention; 1-year warm retention (§5.3) will be the dominant storage cost."*
- [ADR-0006](0006-qdrant-as-vector-store.md) §Negative-operational: HNSW RAM cost is the dominant cost for the vector path; bounded only by retention-driven point-count caps and (in v0.6) by quantization.

The bitemporal ADR ([#128](https://github.com/100rd/Omniscience/issues/128), `0008-bitemporal-schema-for-neo4j.md`, parallel sibling in this wave) defines the property semantics, units, identity model, "still valid" sentinel, and interval convention for `valid_from`, `valid_to`, and `recorded_at`. **This ADR cites ADR-0008 as the source of truth for those semantics and does not redefine them.** ADR-0009 fixes the operational shape of the tiers and the worker that moves data between them. The two ADRs are co-equal in epic [#97](https://github.com/100rd/Omniscience/issues/97) Wave 1 and reconcile in Wave 6 ([#139](https://github.com/100rd/Omniscience/issues/139)).

The decision center for ADR-0009 is the warm-tier shape. Vision commits to "snapshot granularity" but does not pick a unit; the warm window is 275 days (90d → 365d), and storage cost scales linearly with snapshot frequency over that window. The wrong choice here is the difference between a multi-tenant Neo4j instance fitting on a single PVC and a multi-instance shard story arriving years earlier than intended. The architect memo on epic #97 flagged this open question and deferred to the ADR author with cost numbers.

## Decision

### §1 Tier definitions

#### Hot (0–90 days, default)

- Full bitemporal fidelity. Every version of every node, every end-dated edge, every `recorded_at` ingestion timestamp is present.
- Lives in the live Neo4j store ([ADR-0005](0005-neo4j-as-graph-store.md) deployment posture). Same database, same indexes, same Bolt port.
- Queryable via MCP / REST `as_of` at arbitrary timestamp precision. The `(workspace_id, recorded_at)` composite index from ADR-0008 backs the predicate.
- Queryable via batch tools (Cypher shell, neo4j-admin export). Same store.
- Restore SLA: **N/A** — hot is the live store.

#### Warm (90 days – 1 year)

**Decision: snapshot-per-day.** One frozen graph state per UTC day, materialized as separate node and relationship rows under a `:Snapshot:Daily` discriminator label, in the same Neo4j database as hot.

- Snapshot identity: `(workspace_id, snapshot_date_utc)`. The retention worker emits one snapshot per workspace per day.
- Snapshot content: every entity and edge whose `recorded_at` falls within the snapshot day, projected through the bitemporal interval convention from ADR-0008 — i.e. each entity's "as-of-end-of-day" state, with intermediate intra-day versions collapsed.
- Storage shape: `(:EntitySnapshot:Daily {workspace_id, entity_id, snapshot_date, valid_from, valid_to, recorded_at_at_snapshot, ...properties})` and `(:RelationshipSnapshot:Daily {workspace_id, edge_id, snapshot_date, ...})`. Discriminator labels keep snapshot rows out of the live `:Entity` indexes — performance isolation for hot reads is index-backed, not query-rewrite-only.
- Queryable via MCP / REST `as_of` at **day** precision. A request with `as_of=2025-11-12T14:30:00Z` against a date in the warm window resolves to the `2025-11-12` snapshot. Sub-day precision is rounded to the snapshot boundary; the response envelope carries a `tier: "warm"` and `snapshot_date` field for caller observability.
- Queryable via batch tools — yes, same store.
- Restore SLA: **N/A** — warm is online.

The choice of **per-day** over per-week or compacted-by-day is justified in §Alternatives-rejected. The summary: per-day's storage cost is bounded (≈275 snapshots × per-workspace daily delta, see §Cost), and incident-debugging fidelity at day granularity is the floor of customer-acceptable; per-week is below that floor; compacted-by-day adds writer-side complexity that is not justified by the storage saving.

#### Archive (>1 year)

**Decision: object storage snapshots in Parquet, indexable but not exposed on the live MCP/REST surface.**

- Storage backend: S3-compatible object storage, configured by Helm value `omniscience.archive.bucket`. Encryption at rest with KMS-managed keys (mandatory in non-dev environments; dev profile allows SSE-S3).
- Object format: **Parquet**. One file per `(workspace_id, snapshot_date)`, columnar layout matches the warm snapshot row shape. Parquet wins over JSONL on compression ratio (≈3-5× smaller for entity-row workloads) and over neo4j-dump on portability — Parquet is queryable from DuckDB, Athena, Spark without a Neo4j installation, which matters for the offline-restore use case where the operator may not want to spin a full Neo4j replica to inspect data.
- Bucket layout: `s3://{bucket}/{workspace_id}/{year}/{month}/{day}/snapshot.parquet`. Workspace as the top-level prefix gives operations a single-bucket-prefix delete capability for tenant offboarding (per [#117](https://github.com/100rd/Omniscience/issues/117) carry-forward).
- Archive retention: **7 years** by default, configurable per-deployment. Beyond 7 years, snapshots are deleted by lifecycle rule on the bucket. A separate ADR will revisit the 7-year horizon if customer compliance requires longer (SOC 2 Type II — Q3 2026, see vision §6 — does not mandate beyond 7y).
- Queryable via MCP / REST `as_of` — **no**. An `as_of` request that resolves to the archive window returns a structured `degraded_response` envelope (see §4 below). No silent failure; no synchronous archive read on the request path.
- Queryable via batch tools — **yes**, via DuckDB / Athena / Spark over Parquet. The "offline restore" CLI is a separate sub-issue (out of scope here per the scope guardrails); this ADR commits to opening it as a follow-up in epic [#97](https://github.com/100rd/Omniscience/issues/97) before [#139](https://github.com/100rd/Omniscience/issues/139) closes.
- Restore SLA: best-effort, **no online SLO**. Operator-initiated only; the response is "we will restore this snapshot to a side database on request" rather than "the live system fetches transparently".

### §2 Eviction triggers

- **Time-based**, on `recorded_at` only. ADR-0008 fixes `recorded_at` as the operator-time clock; ADR-0008 evicts on operator time, never on world time. The boundary is configurable (§10) but defaults to 90 days (hot→warm) and 365 days (warm→archive).
- **Eligibility predicate** (Cypher shape, indicative — final form in [#135](https://github.com/100rd/Omniscience/issues/135)):
  ```
  MATCH (n) WHERE n.workspace_id = $workspace_id
    AND n.recorded_at < $hot_cutoff
    AND NOT (n:Snapshot)
  ```
  Composite-index-backed via `(workspace_id, recorded_at)` from ADR-0008. The worker iterates per-workspace; cross-tenant batches are forbidden (see §Consequences-security).
- **Edge end-dating does NOT trigger eviction.** A relationship with `valid_to` set in the past (per ADR-0008 §3 tombstone semantics) remains in the hot tier as long as its `recorded_at` is within the hot window. World-time end-dating and operator-time eviction are orthogonal concerns. This ADR states the boundary explicitly to forestall the most likely operator-vs-world confusion.
- **Eviction granularity: per-version.** A node identity whose latest version is hot but whose history extends into warm has its older versions evicted to warm while the latest stays hot. The writer-side query (per ADR-0008's identity model — property-versioned-via-state-relationship is the most likely pick, but ADR-0008 is agnostic) must support per-version eligibility selection. The alternative (per-identity, "all versions stay together") is rejected in §Alternatives-rejected: it makes hot footprint unbounded for long-lived entities.

### §3 Worker shape

- **Pluggable scheduler: in-process FastAPI lifespan + APScheduler.** The retention worker runs as a periodic background task launched at FastAPI startup, scheduled via APScheduler at a configurable interval (default: every 6 hours). Justification:
  - **In-process over Kubernetes CronJob**: tighter feedback loop, single observability surface, no separate image/Helm template/RBAC for v1. Scale-out happens by horizontal-scaling the server replicas and electing a single worker via the existing NATS coordination primitives (the `INGEST_CHANGES` stream from [#127](https://github.com/100rd/Omniscience/issues/127) already provides workspace-scoped exclusive consumers; we re-use that primitive for retention worker leadership).
  - **In-process over NATS-triggered**: retention is wall-clock periodic, not event-driven. NATS subjects would be a strange fit ("publish a tick every 6h"); APScheduler handles that natively.
  - **CronJob revisit trigger**: if eviction lag SLO (§8) breaches in a way that is bounded by single-process throughput rather than by Cypher query plan, the worker spins out into a Kubernetes CronJob in a follow-up ADR. The `GraphStore` interface is unchanged either way; only the orchestration shifts.
- **Idempotency**: re-running the worker is a no-op unless time has advanced. Eligibility is re-derived from `recorded_at` < cutoff each run; a previously-evicted record is excluded by the discriminator label (`:Snapshot` for warm, absence-from-store for archive). The worker tolerates partial-prior-run state — a crash after marking but before moving leaves the next run able to resume from the marker.
- **Concurrency vs. live writes: read-then-mark-then-move.** Three-phase, bounded blast radius:
  1. **Read** in a short read transaction: select eligible record ids in batches of 1000 per workspace, page through.
  2. **Mark** in a short write transaction: set `n.tier = 'warm_pending'` (or `'archive_pending'`) on each eligible id. Cheap property update, no relationship writes, no identity churn.
  3. **Move** in a follower transaction: read marked records, write the snapshot row (warm) or stream-upload the Parquet object (archive), then delete the live row in a separate transaction. The split prevents a single long-lived write transaction from blocking ingestion.

  The writer-side query (per ADR-0008 §1) ignores rows with `tier != 'hot'` on the hot path — it does not see in-flight warm/archive movers, so live ingestion proceeds without coordination.
- **Dry-run mode (mandatory)**: `OMNISCIENCE_RETENTION_DRY_RUN=true` makes the worker emit metrics, log eligible counts, and produce a structured "would-evict" report without writing anything. Required for ops review before flipping the worker to live in production for the first time on each deployment.
- **Per-tenant iteration**: the worker iterates workspaces sequentially within a run; eviction batches are workspace-scoped. The ACL invariant is structural — there is no cross-workspace query in the eviction path at all (see §Consequences-security).

### §4 Read-path behaviour across tiers

- **Hot reads (no `as_of`, or `as_of` within hot window)**: query goes only against the live store. The discriminator labels (`:Snapshot:Daily`) keep warm rows out of the `:Entity` index hits. **Performance isolation guarantee**: a "current state" read MUST NEVER touch warm or archive — this is enforced at the query level by adding `AND NOT (n:Snapshot)` to every hot-path Cypher (or by relying on the index not seeing snapshot rows; the application's `GraphStore` adapter picks the form in [#132](https://github.com/100rd/Omniscience/issues/132)).
- **Warm reads (`as_of` in warm window)**: the application's `GraphStore` adapter rewrites the query to target the snapshot label set keyed by the resolved `snapshot_date`. **Cypher-side `UNION` is rejected** (see §Alternatives-rejected) because it loses index pin-pointing and forces both halves of the union to evaluate. The adapter resolves the tier first (a small `recorded_at` check), then issues a single tier-specific query. The response envelope carries `tier: "warm"` and `snapshot_date: "2025-11-12"` so callers know the granularity floor.
- **Archive reads (`as_of` in archive window)**: the MCP tool returns a structured `degraded_response` envelope. Shape:
  ```json
  {
    "status": "degraded",
    "reason": "as_of_in_archive_tier",
    "as_of_requested": "2024-01-15T03:14:00Z",
    "archive_window_start": "2025-04-24T00:00:00Z",
    "hint": "Archive snapshots are available via offline restore tooling; see docs/runbooks/archive-restore.md (sub-issue TBD in epic #97)."
  }
  ```
  No silent failure. No synchronous archive fetch on the request path. The response is documented in the MCP API contract and treated as a first-class non-error outcome by the structured-evidence pattern (vision §7 — Omniscience emits structured evidence, callers craft the answer).
- **Archive existence MUST NOT leak across workspaces.** A `degraded_response` for a workspace that has no archive at the requested `as_of` is identical in shape to one that does — the existence-of-archive bit is workspace-scoped and never observable from the response (see §Consequences-security).

### §5 Qdrant alignment

- Qdrant payload retention mirrors Neo4j. Chunks in the Qdrant collection ([ADR-0006](0006-qdrant-as-vector-store.md) schema posture) with `recorded_at` past the hot→warm boundary are evicted from the collection in the same retention run that handles the graph.
- **Warm shape on Qdrant**: snapshot-per-day, mirrored from the graph. A snapshot collection per workspace per day is rejected (collection-multiplication anti-pattern from ADR-0006 §Schema-posture); instead, snapshot points carry a `tier: "warm"` payload field and `snapshot_date`, with a payload index on `tier`. Hot reads filter `tier = "hot"` (in addition to `workspace_id`); warm reads filter `tier = "warm" AND snapshot_date = $D`. This matches ADR-0006's "workspace is a payload field, not a collection key" posture.
- **Archive shape on Qdrant**: chunks past the warm→archive boundary are evicted from Qdrant entirely. **Re-embedding from archive is not supported in v1.** Embeddings are reproducible from the Postgres `chunks` table (per ADR-0006 implementation notes), but the chunks table is itself subject to retention — the existing tombstone/janitor model in `docs/schema.md` §"Tombstones not deletes". The trade-off is explicit: archive vector search is impossible in v1; archive `degraded_response` for vector-shaped queries returns the same envelope as graph-shaped queries.
- The Qdrant retention path is part of [#135](https://github.com/100rd/Omniscience/issues/135)'s worker — one retention run, two stores, one eligibility predicate. The cross-store consistency invariant (graph and vector see the same tier for the same `(workspace_id, recorded_at)` cohort) is a property-test target in [#138](https://github.com/100rd/Omniscience/issues/138).

### §6 Postgres operational metadata

Postgres tables (`sources`, `ingestion_runs`, `documents`, `chunks`, `tokens`, `workspaces`) are **NOT subject to graph retention tiers.** Their lifecycle is the existing tombstone/janitor model documented in `docs/schema.md` §"Tombstones not deletes". This boundary is the same one ADR-0008 §7 draws and the same one the architect memo on epic #97 calls out explicitly. The ADR states it here to forestall a well-meaning future PR that drags retention into Alembic migrations.

The architect memo is precise: *"Sources are configured or not; ingestion runs happened or didn't; tokens are valid or revoked. The existing tombstone/janitor model is the right shape for that data. Adding bitemporal columns to those tables would multiply Postgres churn for a property that none of those tables need."* The same logic applies to retention tiering — none of the operational tables have a "what did this look like at as-of" semantic.

### §7 Storage layout for warm and archive

- **Warm physical layout: same Neo4j database as hot, discriminator label.** Justification:
  - Same-database keeps the operational surface small (one Neo4j to back up, monitor, upgrade).
  - Discriminator labels (`:Snapshot:Daily`) carry their own indexes; the hot-path indexes on `:Entity` do not see snapshot rows. Hot read performance is index-backed and unchanged.
  - Disk cost of warm is bounded by snapshot count × per-workspace daily delta; on a target v0.5 envelope (≤10 workspaces, ≤1M entities, ≈10K daily deltas per workspace), the warm rowcount over 275 days is ≈2.75M rows per workspace — tractable on the same 100GB SSD as hot ([ADR-0005](0005-neo4j-as-graph-store.md) resource envelope) for v0.5 scale, with headroom for a 10× growth before sizing matters.
  - **Separate-database** rejected: doubles the operational surface (two Neo4j upgrades, two backup paths, two Helm sub-charts) for a clear win only at >5× v0.5 scale. Revisit trigger captured below.
  - **Separate-cluster** rejected outright: triples the operational surface; appropriate only if warm storage must run on different hardware (cheap rotational disk vs. NVMe), which the v0.5 envelope does not demand.
- **Archive physical layout: S3-compatible object storage, KMS-encrypted, Parquet format.** Justified above (§1 Archive). Bucket layout enforces tenant prefix isolation, supports lifecycle rules, and stays within the self-hosted-or-private-SaaS posture (vision §6).
- **Restore tooling**: out of scope for this ADR. A follow-up sub-issue MUST be opened in epic [#97](https://github.com/100rd/Omniscience/issues/97) for the offline restore CLI before [#139](https://github.com/100rd/Omniscience/issues/139) closes. The CLI's contract: read a `(workspace_id, snapshot_date)` from S3, write to a side Neo4j (or DuckDB) for ad-hoc inspection, never to the live store.

### §8 Observability and SLOs

Required Prometheus metrics on the existing `/metrics` surface:

- `omniscience_graph_records_total{tier="hot|warm|archive", store="neo4j|qdrant"}` — gauge. Per-tier per-store record count, scraped after each retention run.
- `omniscience_retention_eviction_total{transition="hot_to_warm|warm_to_archive", store="neo4j|qdrant"}` — counter. Increments per record evicted.
- `omniscience_retention_worker_duration_seconds{phase="read|mark|move", store="neo4j|qdrant"}` — histogram. Per-phase per-store wall time.
- `omniscience_retention_worker_lag_seconds` — gauge. Wall time since the oldest `recorded_at`-overdue record was evicted, per workspace. Aggregated as `max` for the deployment-wide alert.

**SLO (steady state): retention worker lag stays below 24 hours.** A run scheduled every 6 hours with the read-then-mark-then-move pattern should evict the previous day's overdue cohort within one run. Lag > 24h is a SLO breach (warning); lag > 7 days is **P1** (existing freshness-style alert path).

**Failure mode runbook**: §9 covers the alert paths; the runbook itself lands as a separate sub-issue in epic [#97](https://github.com/100rd/Omniscience/issues/97), referenced from `docs/runbooks/`.

### §9 Failure modes

- **Worker crash during a run**: the next scheduled run resumes from idempotency (per §3). Crash count is observable via `omniscience_retention_worker_duration_seconds` (missing data points correlate with crash) and via the existing FastAPI lifespan error path. No data loss — read-then-mark-then-move is crash-safe.
- **Eviction lag past 7 days**: P1 alert via the existing alert path (the same one freshness uses). Runbook: check worker logs, check `omniscience_retention_worker_duration_seconds` histogram for slow phase, check Neo4j query plan for the eligibility predicate (most likely cause: index missed because `(workspace_id, recorded_at)` from ADR-0008 didn't backfill).
- **Archive read failure during a `degraded_response`**: the `degraded_response` envelope itself does not synchronously read archive. The runbook applies only to the offline-restore CLI (separate sub-issue).
- **Object storage outage**: archive writes fail, retention worker logs and exits the move phase early; eligible records remain marked but not moved. Next run retries. Manageable: object storage outages on AWS S3 are minutes-to-hours, not days; a backlog of unmoved-archive records does not affect query correctness (they remain in warm and are queryable).
- **Snapshot inconsistency between Neo4j and Qdrant**: cross-store consistency is a property-test target ([#138](https://github.com/100rd/Omniscience/issues/138)); a divergence beyond a per-cohort tolerance (TBD in [#138](https://github.com/100rd/Omniscience/issues/138)) escalates to a P2 alert. Mitigation in v1: re-run the worker; if persistent, trigger a per-workspace re-sync from Postgres `chunks` (the canonical source for vector lineage).

### §10 Configurability

- **Tier boundaries (90d, 1y) are global-only in v1.** Configured via Helm values:
  ```yaml
  omniscience:
    retention:
      hotDays: 90      # default
      warmDays: 365    # default (cumulative — not warm window length)
      archiveYears: 7  # default
  ```
  Per-tenant override is **out of scope for v1**. Rationale:
  - Per-tenant boundaries multiply test surface (the property tests in [#138](https://github.com/100rd/Omniscience/issues/138) become per-tenant rather than per-deployment); v1 doesn't have a customer query pattern that demands tenant-specific retention.
  - Self-hosted deployments are predominantly single-tenant or small-N-tenant in v0.5 scale (vision §3 — design partners are platform/SRE teams, not multi-tenant SaaS aggregators); global-only covers the addressable case.
  - **Per-tenant configurability is a real revisit trigger** (see Revisit triggers): when the first customer asks ("we want 7y hot for compliance"), the migration path is straightforward — store the override in the `workspaces` Postgres table, threaded through to the retention worker per-iteration.

## Alternatives rejected

### Warm-tier shape

Three alternatives considered, all rejected.

#### Snapshot-per-week
Rejected. Cost saving is real (≈40 weekly snapshots vs. 275 daily — 7× rowcount reduction over the warm window), but the fidelity loss is unacceptable for the incident-debugging use case that motivates the temporal graph (vision §5.3 + the architect memo on epic #97). A weekly snapshot at granularity 7d means an SRE asking *"what did the graph look like 4 days before the incident?"* gets a response rounded to the nearest week — likely one Tuesday's state for a Thursday incident. That is below the customer-acceptable floor ("MTTR on targeted incident classes from 3-4h to 10-20min", vision §9, requires sub-day temporal precision in the warm window).

#### Snapshot-per-hour
Rejected upfront. Cost is 24× the per-day envelope; storage at v0.5 scale would be ≈66M warm rows per workspace over 275 days. That breaks the same-database posture (§7) and forces a separate-cluster decision much earlier than v0.5 needs. Hourly fidelity in the warm window provides no measurable customer benefit — incident debugging that needs sub-day precision is happening on the hot tier (90d hot), not in the 90d-365d window where the dominant query is *"what did the graph look like roughly N months ago?"*.

#### Compacted-by-day (keep latest version per `recorded_at` day, drop intermediate)
Rejected, but more reluctantly. The cost story is attractive: rowcount in the warm window equals rowcount of distinct `(workspace_id, entity_id, day)` triples, which for typical entities (cluster nodes, K8s deployments, Terraform resources) is approximately the active entity count per day, not the total version count. The fidelity story is also acceptable for the dominant query pattern.

The reasons to reject:

1. **Writer-side complexity.** The retention worker has to evaluate "which of N versions emitted this day is the latest" on the move phase, which adds an aggregation pass to every run. The per-day snapshot approach reads the live store row-by-row and emits one snapshot row per entity per day with no aggregation — the dumber, more debuggable shape.
2. **Round-trip semantics.** Compacted-by-day loses the "Omniscience learned this fact at 14:00Z and revised it at 16:00Z" trace. ADR-0008 fixes `recorded_at` as the operator-time clock, and a property of the bitemporal model is that operator-time learning history is itself queryable. Dropping intermediate `recorded_at` versions in the warm tier loses that property silently — the warm-tier `recorded_at` field would be unreliable in a way no other tier exhibits.
3. **The cost saving is bounded.** At v0.5 scale, per-day's 2.75M rows per workspace over 275 days is well within the 100GB SSD envelope ([ADR-0005](0005-neo4j-as-graph-store.md)). Compacted-by-day saves ≈30-50% of that, which moves the squeeze date from "year 5 at 100× growth" to "year 6 at 100× growth" — not material to v0.5 design.

Compacted-by-day is the right answer if the storage squeeze materializes earlier than expected. It is captured as a Revisit trigger.

#### Cold-only (no warm tier — hot ↔ archive direct)
Rejected. The 90d → 1y window is exactly the window where incident-pattern analysis happens (post-mortem retros at 6 months, "we had this same failure mode last quarter" at 4 months). Skipping warm makes that window unqueryable on the live MCP/REST surface — every query in that range becomes a `degraded_response`. The vision commitment in §5.3 is explicit about three tiers; cold-only contradicts it.

### Worker shape

#### In-process (FastAPI lifespan + APScheduler) — chosen
Justified in §3.

#### Kubernetes CronJob
Rejected for v1. Separate image, separate Helm template, separate RBAC, separate observability surface. Wins are real at scale (independent failure domain, independent scaling) but are not the v0.5 problem. Captured as a Revisit trigger.

#### NATS-triggered (publish a tick on a subject, server consumes it)
Rejected. Retention is wall-clock periodic, not event-driven. NATS subjects are the wrong primitive for "every 6 hours". The existing `INGEST_CHANGES` stream from [#127](https://github.com/100rd/Omniscience/issues/127) is event-driven (operator pod changes); retention would be a misuse of that infrastructure.

### Archive format

#### Parquet — chosen
Justified in §1 Archive.

#### JSONL
Rejected. Larger by 3-5× at typical entity-row shapes (verbose field names, no columnar compression). Only win is grep-ability without tooling — but the offline-restore use case explicitly assumes operator tooling (DuckDB / Athena / Spark), so grep-ability is not a deciding factor.

#### Neo4j-dump (`neo4j-admin dump`)
Rejected. Strongest fidelity (round-trip restore is a single `neo4j-admin load` command) but loses portability — inspecting the dump requires a running Neo4j instance, which is the opposite of what the offline-restore use case wants. Also: dump format is Neo4j-version-coupled, which makes long-horizon archive reads dependent on Neo4j version compatibility we don't want to commit to over a 7-year archive horizon.

### Eviction trigger granularity

#### Per-version — chosen
Justified in §2.

#### Per-identity ("all versions stay together until the latest is overdue")
Rejected. Long-lived entities (a Kubernetes namespace that has existed for 2 years) would keep all 2 years of versions in hot, defeating the retention strategy. Per-identity makes hot footprint unbounded for stable infrastructure, which is the opposite of the design intent.

## Consequences

### Positive

- **Vision §5.3 commitment is operationally specified.** The three-tier shape (hot/warm/archive), the warm granularity (per-day), and the archive disposition (queryable Parquet, no live MCP) are now decisions, not aspirations. Wave 4 ([#135](https://github.com/100rd/Omniscience/issues/135), [#136](https://github.com/100rd/Omniscience/issues/136)) implements from this ADR alone.
- **Storage footprint is bounded.** The combination of per-day warm and 7-year archive caps deployed-Neo4j disk by a known function of (workspace count, daily delta, retention boundary). [ADR-0005](0005-neo4j-as-graph-store.md)'s 100GB SSD envelope is sized for this — see §Cost.
- **Hot read performance is unchanged.** Discriminator labels keep snapshot rows out of `:Entity` indexes; the hot-path Cypher does not see warm data; ADR-0006's payload index on `tier` does the same on Qdrant. Performance isolation is index-backed.
- **`degraded_response` is a first-class envelope, not silent failure.** Callers (LLM-driven agents per ADR-0003) get structured evidence about *why* a query returned reduced fidelity, which is the whole vision §7 posture ("the calling agent crafts the answer").
- **Observability is concrete.** Prometheus metrics, SLO, alert paths are specified. [#136](https://github.com/100rd/Omniscience/issues/136) implements from this ADR alone.

### Negative — operational

- **Snapshot worker becomes a standing operational concern.** Failure modes are documented (§9), but a retention worker that silently falls behind is a credible incident shape. Mitigation: the lag SLO + P1 escalation path; dry-run mode required before first live activation per deployment.
- **Object storage dependency is new.** Archive lives in S3-compatible storage; deployments without one (air-gapped, no S3-compatible object store available) cannot use the archive tier. Workaround: configure `archiveYears: 0` (effectively disable archive) and accept that retention drops data older than 1 year. Documented in `docs/deploy.md` (cross-doc consequence below).
- **Backup story is now multi-tier.** [ADR-0005](0005-neo4j-as-graph-store.md) §Deployment-posture specified offline `neo4j-admin backup`; that backup now has to capture both hot and warm rows (single Neo4j database — automatic) plus, separately, the archive bucket has its own object-storage replication. Two backup paths, one for the live database, one for the archive bucket.
- **Schema migrations interact with retention.** If a future ADR changes the entity schema, warm snapshots in the old shape and archive snapshots in the old shape coexist with hot rows in the new shape. Schema-migration runbooks now have to document what happens to historical snapshots; the simplest answer is "snapshot format is versioned by ingestion-time schema and read paths are version-aware" — but this is a non-trivial complexity tax that this ADR opens. Captured as Risk.

### Negative — team

- **Cypher fluency on snapshot semantics.** The retention worker's eligibility predicate, the per-version eviction logic, and the warm-tier read rewrite are non-trivial Cypher. The same onboarding cost ADR-0005 listed for the Neo4j adapter applies again here for the retention path. Mitigation: pair [#135](https://github.com/100rd/Omniscience/issues/135)'s implementation PR with a short retention-tier cheat-sheet in `docs/`.
- **Parquet-side fluency.** Archive reads (offline restore) require DuckDB / Athena / Spark, which is not a current team skill. Mitigation: the offline-restore CLI is a separate sub-issue with its own design and its own review window.
- **Observability dashboard authoring.** The retention metrics dashboard ([#136](https://github.com/100rd/Omniscience/issues/136)) requires PromQL fluency on histograms (`rate`, `histogram_quantile`) plus Grafana template variables — the same skills the existing freshness dashboard required, so the cost is incremental rather than new.

### Negative — security

**ACL invariant is non-negotiable. Every retention worker query is composite on `workspace_id`. Tier transitions never widen reads across workspaces.** This carries forward the boundary that [ADR-0005](0005-neo4j-as-graph-store.md) §Negative-security mandated, that [#117](https://github.com/100rd/Omniscience/issues/117) corrected for the existing pgvector path, and that [#119](https://github.com/100rd/Omniscience/issues/119) shipped as the read-side enforcement.

Specific carry-forward:

1. The retention worker's eligibility predicate MUST include `n.workspace_id = $workspace_id` in every Cypher template — index-backed via the `(workspace_id, recorded_at)` composite from ADR-0008. The worker's iteration shape is per-workspace (§3) — at no point does a single Cypher query cross workspaces, even for batch efficiency.
2. The Qdrant retention path follows ADR-0006's `must`-clause filter on `workspace_id` for every read, every scroll, every count. The retention adapter constructs `Filter(must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)), ...])` via the typed filter-builder; raw `Filter` construction is review-rejected.
3. The archive bucket layout (`s3://{bucket}/{workspace_id}/{year}/{month}/{day}/snapshot.parquet`) puts `workspace_id` at the top-level prefix. IAM policies on the archive bucket SHOULD scope per-prefix so a compromised retention worker credential can only write to the workspace-scoped prefix. (IAM policy is operational; this ADR specifies the layout that makes per-prefix scoping possible.)
4. The `degraded_response` envelope for archive reads MUST NOT leak existence information across workspaces. A request to workspace A for an `as_of` in archive returns the same envelope shape regardless of whether workspace A has any archive at that timestamp or whether it has been offboarded entirely. The "have you ever existed" bit is workspace-scoped and unobservable from the response. Same posture as the cross-workspace isolation contract test that ADR-0006 mandates for Qdrant.
5. **Cross-tenant eviction batches are forbidden.** A naive batched query "evict everything past the cutoff across all workspaces" is more efficient and is wrong — it bypasses the workspace-level enforcement boundary. The contract test in [#138](https://github.com/100rd/Omniscience/issues/138) MUST include "two workspaces with overlapping `recorded_at`, run the worker, assert eviction batches are workspace-scoped".

The architect memo on epic #97 calls this out explicitly: *"The ACL invariant from ADR-0005 / #117 / #119 is carried forward on every read, every write, every eviction batch, every backfill query."* [#129](https://github.com/100rd/Omniscience/issues/129)'s scope guardrails state this as a non-negotiable.

### Negative — cost

Retention IS the cost. Numbers below are order-of-magnitude indicative for v0.5 scale; the [#136](https://github.com/100rd/Omniscience/issues/136) dashboard reports measured values in production.

- **Hot footprint, Neo4j**: ≈ X GiB per workspace at 1M entities × ~3 versions/entity over 90 days, at typical entity row size 1 KiB → **≈3 GiB per workspace per 90 days**. Well within ADR-0005's 100GB SSD envelope at ≤10 workspaces.
- **Warm footprint, Neo4j**: per-day snapshots over 275 days × ~10K active entities per workspace per day × 1 KiB row size → **≈2.75 GiB per workspace** in the warm window. Combined hot+warm is ≈6 GiB per workspace; 10 workspaces is ≈60 GiB — half of the envelope, which is the design intent.
- **Hot+warm footprint, Qdrant**: ≈4 GiB per workspace for hot vector points (1M entities × ~5 chunks/entity × 768-dim × 4 bytes) + ≈2 GiB per workspace for warm payload-only points (snapshots are payload, not embeddings — see §5). RAM-resident HNSW for the hot fraction; warm is on-disk-payload-only.
- **Archive footprint, S3**: Parquet compression at ≈3× over JSONL → roughly 1 GiB per workspace per year of archive (column-store + delta-coding on `recorded_at` columns). At 7y archive × 10 workspaces, that's ≈70 GiB in S3 — at S3 standard storage pricing, on the order of $1.50/month for the entire deployment. **Archive is not the storage cost driver; warm is.**
- **Archive read cost**: zero in steady state (`degraded_response` does not read archive). Operator-initiated restores via DuckDB / Athena are bounded by individual snapshot file sizes (≈3 MiB per snapshot per workspace at v0.5 scale).
- **Worker compute**: APScheduler tick every 6h runs ≈10 workspace iterations × ≈1000-row batches × 3 phases (read/mark/move) — bounded sub-minute per workspace, total worker time per run ≈10 minutes at v0.5 scale. Negligible compared to ingestion compute.
- **Backup envelope**: warm rows are in the same Neo4j database as hot, so the existing nightly `neo4j-admin backup` window (per ADR-0005) covers them. Archive bucket replication (S3 cross-region or similar) is the operator's standing object-storage cost, on the order of $1-3/month at v0.5 scale.

The dominant cost remains the hot+warm Neo4j+Qdrant footprint, exactly as ADR-0005 §Negative-operational and ADR-0006 §Negative-cost predicted. ADR-0008 caps that footprint via the warm granularity choice (per-day, not per-hour) and via the 1y warm boundary; squeezes beyond v0.5 scale trip the Revisit triggers below.

### Risks

- **Schema-migration interaction with snapshots.** A future ADR that changes entity properties opens a question: do warm snapshots in the old schema get migrated, dropped, or read with version-aware adapters? This ADR does not pre-empt the answer; the schema-migration runbook (separate doc, separate sub-issue) decides per-migration. Documented as a known long-tail risk.
- **Customer-config drift.** Tier boundaries are global-only in v1 (§10). The first customer ask for per-tenant boundaries is the moment the global-only choice becomes a migration. The migration path is documented (move boundaries to the `workspaces` Postgres table) — not implemented.
- **Cross-store consistency between Neo4j and Qdrant** under retention. The eviction worker handles both stores in one run, but an inconsistency window exists between the Neo4j move and the Qdrant move. Property tests in [#138](https://github.com/100rd/Omniscience/issues/138) are the gate; if the divergence budget is breached in production, the runbook escalates to a full per-workspace re-sync.
- **ADR numbering reconciliation** (resolved at PR-open time). The K8s operator ADR ([#101](https://github.com/100rd/Omniscience/issues/101), epic #98, merged in PR #142) occupies `0007-k8s-operator-architecture.md`; the bitemporal sibling ([#128](https://github.com/100rd/Omniscience/issues/128)) consequently lands at `0008-bitemporal-schema-for-neo4j.md` (ADR-0008); this retention ADR lands at `0009-retention-tiering-policy.md` (ADR-0009). All cross-references in this file use the resolved numbering. **No semantic decision depends on the file path** — the ADR identity is the bitemporal/retention contract, not the prefix.
- **ADR-0008 alignment on `recorded_at` units and "still valid" sentinel.** ADR-0009 §1 (snapshot identity, eligibility predicate) and §2 (per-version eviction) operate on `recorded_at`. ADR-0008 fixes the units (Cypher datetime is the most likely pick per the architect memo) and the "still valid" sentinel for `valid_to` (`NULL` is the SQL-bitemporal default). If ADR-0008 picks a different sentinel (e.g. `+infinity` epoch-ms), the §2 eligibility predicate's interaction with end-dated edges still holds — eviction is on `recorded_at` only, never `valid_to`. The conservative path is taken: this ADR does not assume the sentinel value, only that ADR-0008 fixes it.

## Revisit triggers

- **Storage squeeze on warm.** Disk usage of warm exceeds 50% of the deployed Neo4j PVC. Revisit warm-tier shape (compacted-by-day becomes the candidate replacement) and/or move warm to a separate Neo4j database.
- **Customer query pattern shift.** Telemetry shows `as_of` queries clustered in specific sub-day ranges in the warm window — revisit per-day granularity (rare; more likely the hot window expands).
- **Per-tenant configurability ask.** First customer requests retention boundaries different from the global default. Revisit §10; migration path is documented.
- **Eviction lag SLO breach.** Worker lag SLO (24h) is repeatedly breached and root-cause is single-process throughput, not query plan. Revisit §3 worker shape (Kubernetes CronJob is the candidate replacement).
- **Archive query pattern emerges.** Customer ask for synchronous archive queries on the live MCP surface. Revisit §1 archive disposition (queryable parquet via batch tools is already supported; "live MCP" is the change).
- **Compliance horizon shift.** SOC 2 Type II (Q3 2026) or sector-specific compliance (e.g. financial regulation) requires archive retention longer than 7 years, or shorter for data minimization. Revisit §1 archive retention.
- **Object storage availability constraint.** Customer deploys air-gapped or in an environment without S3-compatible object storage. Revisit §1 archive disposition (filesystem-backed archive is a candidate, with explicit operator responsibility for the offsite copy).
- **Schema-migration friction.** A schema migration breaks warm snapshot compatibility in a way that demands a structural rewrite. Revisit §7 storage layout — version-aware snapshot adapters become a requirement rather than an option.

## Cross-doc consequences

- [`docs/decisions/0005-neo4j-as-graph-store.md`](0005-neo4j-as-graph-store.md) §Negative-operational has a one-line cross-reference to add: "Retention shape and worker behaviour fixed in ADR-0009." Amendment lands in a follow-up sub-issue, not this PR (per scope guardrails).
- [`docs/decisions/0006-qdrant-as-vector-store.md`](0006-qdrant-as-vector-store.md) §Deployment-posture has a one-line cross-reference: "Qdrant retention mirror specified in ADR-0009 §5." Amendment lands in a follow-up sub-issue, not this PR.
- ADR-0008 (bitemporal schema, parallel sibling — `0008-bitemporal-schema-for-neo4j.md` per [#128](https://github.com/100rd/Omniscience/issues/128); see Risks for the numbering collision note) is the canonical source for `recorded_at` units, "still valid" sentinel, and identity model. ADR-0009 cites; ADR-0008 is amended in Wave 6 ([#139](https://github.com/100rd/Omniscience/issues/139)) only if reconciliation requires it.
- [`docs/schema.md`](../schema.md) — no change required. The Postgres operational tables are explicitly out of scope for graph retention tiers (§6); the existing tombstone/janitor model is unchanged. A one-line cross-reference ("Graph retention tiers and the lifecycle separation are specified in ADR-0009 §6") is appropriate as a follow-up amendment, not in this PR.
- [`docs/deploy.md`](../deploy.md) — needs updates for the archive bucket configuration (Helm value `omniscience.archive.bucket`, KMS key reference, IAM policy guidance) and for the global retention boundaries (§10). Lands in [#135](https://github.com/100rd/Omniscience/issues/135) or [#136](https://github.com/100rd/Omniscience/issues/136) as part of the implementation PR, not in this ADR.
- [`docs/vision.md`](../vision.md) §5.3 already names the three-tier shape; cross-link this ADR alongside ADR-0008. One-line addition is in scope for a Wave 6 amendment, not this PR.
- A new `docs/runbooks/retention-worker-runbook.md` lands with [#136](https://github.com/100rd/Omniscience/issues/136) (alert/dashboard PR), covering §9 failure modes.
- The offline-restore CLI sub-issue is opened in epic [#97](https://github.com/100rd/Omniscience/issues/97) before [#139](https://github.com/100rd/Omniscience/issues/139) closes; its design lives in a separate ADR if non-trivial, otherwise in `docs/runbooks/archive-restore.md`.

## Links

- Parent epic: [#97](https://github.com/100rd/Omniscience/issues/97)
- This issue: [#129](https://github.com/100rd/Omniscience/issues/129)
- Parallel sibling (canonical bitemporal contract): [#128](https://github.com/100rd/Omniscience/issues/128) — `0008-bitemporal-schema-for-neo4j.md`
- Blocks: [#135](https://github.com/100rd/Omniscience/issues/135) (retention worker), [#136](https://github.com/100rd/Omniscience/issues/136) (retention metrics)
- Wave 6 reconciliation: [#138](https://github.com/100rd/Omniscience/issues/138) (property tests), [#139](https://github.com/100rd/Omniscience/issues/139) (flip Status to Implemented)
- Pairs with: [ADR-0005](0005-neo4j-as-graph-store.md) (Neo4j graph store), [ADR-0006](0006-qdrant-as-vector-store.md) (Qdrant vector store)
- Vision references: [`docs/vision.md`](../vision.md) §5.3 (retention commitment), §11 (temporal complexity caveat)
- ACL carry-forward: [ADR-0005](0005-neo4j-as-graph-store.md) §Negative-security, [#117](https://github.com/100rd/Omniscience/issues/117), [#119](https://github.com/100rd/Omniscience/issues/119)
