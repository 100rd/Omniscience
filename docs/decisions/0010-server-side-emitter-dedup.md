# ADR-0010 — Server-side dedup of operator vs k8s-agentic emitters

- **Status**: Accepted
- **Date**: 2026-04-30
- **Deciders**: dev-team Lead, Architect, Security Engineer
- **Issue**: [#164](https://github.com/100rd/Omniscience/issues/164)
- **Precondition**: [#163](https://github.com/100rd/Omniscience/issues/163) — operator reconciliation worker (merged)
- **Blocks**: [#168](https://github.com/100rd/Omniscience/issues/168) — agentic deprecation plan
- **Related**: [ADR-0007 §6](0007-k8s-operator-architecture.md) calls this out as a follow-up.

## Context

The Omniscience platform now has two parallel paths that emit events
for Kubernetes resources:

1. The legacy agentic `k8s` connector
   (`packages/connectors/.../agentic/k8s.py`) — `source_type =
   "k8s-agentic"`. It asks an LLM which kinds to index and emits one
   event per resource.
2. The native in-cluster operator (`operator/`) — `source_type =
   "k8s-operator"`. It watches the API server directly and emits one
   event per resource change. Issue #163 just landed the
   reconciliation worker that makes the operator authoritative for
   the kinds it covers.

During the parallel-deprecation window of epic #98, both producers
emit events for the same Kubernetes resource within the same
workspace. Without server-side coordination this produces two writes
per resource per change with undefined ordering — exactly the
"last writer wins" race that ADR-0007 §6 lists as a known follow-up.

## Decision

Implement a **persistent-state, operator-wins** server-side dedup gate
in the ingestion worker, keyed on `(workspace_id, external_id)`.

The state lives in a new Postgres table `entity_emitter` whose
composite primary key is `(workspace_id, external_id)`. Each row
stores the *current authoritative emitter* for that pair plus
`last_emit_at` and `authority_changed_at` audit columns.

The state machine is:

| Current authority | Event emitter | TTL state | Decision |
|---|---|---|---|
| (none — no row) | any | — | **ACCEPT**, write authority = event emitter |
| same as event | same | — | **ACCEPT** (idempotent — refresh `last_emit_at`) |
| `k8s-agentic` | `k8s-operator` | — | **ACCEPT**, transition to operator (`operator_supersedes_agentic`) |
| `k8s-operator` | `k8s-agentic` | within TTL | **DROP** (`no_op_agentic_dropped`) |
| `k8s-operator` | `k8s-agentic` | TTL expired | **ACCEPT**, transition to agentic (`ttl_reassign_to_agentic`) |

The gate runs **before** the per-document pipeline so dropped events
never touch the index, vector, or graph adapters. There is no DELETE
on the dedup path: events are dropped at the gate, not after a write,
so the bitemporal layer (#131) is undisturbed.

## Why operator wins

- The operator runs in-cluster with watch-list access; it observes
  state changes immediately and is the closest possible source of
  truth. The agentic path is poll-based and lossier.
- Issue #163's reconciliation worker makes the operator
  authoritative by definition for kinds it covers — without this
  dedup, the agentic path can briefly contradict the operator's
  reconciled state.
- The operator stamps `cluster_id` deterministically (#167) so its
  external_ids are stable across restarts; the agentic path has no
  equivalent guarantee.

## Why TTL re-assignment

The reverse transition (`k8s-operator -> k8s-agentic`) is necessary
but bounded:

- If the operator is uninstalled or crashes, the agentic connector is
  the only remaining emitter. Without a TTL the agentic path would
  be permanently silenced for any pair the operator ever touched.
- If the operator merely has a transient blip (NATS hiccup, leader
  re-election) we do *not* want the agentic path to immediately
  reclaim authority — the operator's reconciliation worker (#163)
  re-emits within the configured interval (default 15 minutes).

Default TTL is **24h**. Configurable via
`OMNISCIENCE_INGEST_DEDUP_TTL_HOURS`:

- `24` (default) — agentic reclaims after 24h of operator silence.
- `0` — sticky operator authority; never re-assigned. Use when the
  operator is the only K8s emitter and a misbehaving agentic
  connector must never reclaim a row.
- `-1` — disable the dedup gate entirely (debug only). Equivalent to
  `OMNISCIENCE_DEDUP_ENABLED=false`.

## ACL invariant

`workspace_id` is the **first** column of the composite primary key
and is supplied to the gate by the worker, which derives it from
`Source.tenant_id` server-side. The gate never reads `workspace_id`
from the event payload — even if a hostile producer stamped a
forged value, it would be ignored.

This matches the load-bearing posture of the operator read endpoint
(#163) where `workspace_id` is taken from the bearer token, never
from the query string.

## Metrics

Three counters and one gauge, all under `omniscience_ingestion_dedup_*`:

- `_dedup_total{kind,action}` — coarse-grained action counter
  (`first_authority_assigned`, `no_op_idempotent_refresh`,
  `no_op_agentic_dropped`, `operator_supersedes_agentic`,
  `ttl_reassign_to_agentic`, `dedup_disabled`, `non_k8s_bypass`).
- `_dedup_drop_total{kind,dropped_emitter,authority_emitter}` —
  detailed drop counter (the headline metric for the dashboard).
- `_dedup_authority_transitions_total{kind,from_emitter,to_emitter}` —
  operator/agentic flips.
- `_dedup_authority_count{kind,emitter}` — sampled gauge of
  per-emitter row counts.

**No metric carries `workspace_id` as a label.** Same posture as #163.
The metrics endpoint cannot leak per-tenant emit counts.

## Concurrency

The hot path uses `SELECT ... FOR UPDATE` on the composite primary
key. Postgres serialises concurrent writers naturally; the
state-machine UPDATEs are guarded with a `WHERE
authority_emitter = :old_authority` clause so a losing concurrent
writer's UPDATE is a no-op rather than a clobber.

## Performance

Every dedup decision is a single PK lookup followed by at most one
UPDATE. Lab benchmarks on local Postgres show <1ms p50, well under
the issue's <5ms p50 target.

## Alternatives considered

### Alternative A — bitemporal close (the prompt's framing)
Insert the new row with `valid_from = now`, close the superseded
row with `valid_to = now`. Rejected: the bitemporal layer is
orthogonal (#131) and dedup must not depend on it; dropping at the
gate is simpler and uses one row per pair instead of N.

### Alternative B — Redis hash for authority
A Redis hash `entity_emitter:{workspace_id}` with field `external_id`.
Rejected: introduces a new infrastructure dependency for a problem
Postgres already solves with one row and one index. Re-creates the
durability/consistency problem we just paid for in #163.

### Alternative C — Time-based "last arriving wins"
First event wins for the configured window. Rejected by the issue
explicitly: arrival order is non-deterministic across producers
running on different schedules.

### Alternative D — Content-aware merge
Per-field reconciliation when both emitters disagree. Rejected as
out of scope (CRDT-shaped; #164 §Non-goals).

## Migration path

`packages/core/alembic/versions/0007_entity_emitter.py` creates the
table. The downgrade drops it; the dedup module falls back to
"accept everything" when the table is missing, so a downgrade is
safe to run on a live system (the worker is permissive in the
absence of the table — same posture as the legacy non-dedup write
path).

At deploy time the table starts empty. Pre-existing entities in the
graph store are unowned until the next emit from any source
establishes authority — the operator's reconciliation worker on the
15-minute cycle establishes authority for its covered kinds within
one cycle.

## Status

- [x] State machine + tests — `apps/server/src/omniscience_server/ingestion/dedup.py`,
      `tests/test_ingestion_dedup.py`
- [x] Migration — `packages/core/alembic/versions/0007_entity_emitter.py`
- [x] Worker integration — `apps/server/src/omniscience_server/ingestion/worker.py`
- [x] Metrics — `apps/server/src/omniscience_server/ingestion/metrics.py`
- [ ] E2E verification on a real cluster — tracked in #164 acceptance,
      lands separately when the deploy environment is ready.

## References

- [Issue #164](https://github.com/100rd/Omniscience/issues/164) — server-side dedup
- [Issue #163](https://github.com/100rd/Omniscience/issues/163) — reconciliation worker (precondition)
- [ADR-0007 §6](0007-k8s-operator-architecture.md) — original follow-up note
- [ADR-0007 §ACL](0007-k8s-operator-architecture.md) — workspace_id from token, never from payload
