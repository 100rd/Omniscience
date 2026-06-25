# ADR-0017: Per-source epoch pin and fail-closed watermark policy

**Status**: Accepted  
**Date**: 2026-06-25  
**Supersedes**: parts of ADR-0005 §Epoch filtering (single cross-source watermark)

---

## Context

The original GraphRAG retrieval pipeline (ADR-0005) applied a single global
watermark — the minimum version across all three stores and all sources — to
every vector hit.  This "global-min" approach was correct under the assumption
of a single source per workspace, but caused catastrophic results in
multi-source workspaces: a cold source B (pg watermark = 0) would drive the
global floor to zero and black out *all* hits from healthy source A.

consilium-v10-AP1 replaced the global floor with a per-source ceiling map
(`per_source_watermark: dict[str, int]`) returned by `GlobalReconciler.check_convergence`.
Each hit is evaluated against the watermark of its **own** source only.
A cold source B no longer decimates healthy source A.

However, two gaps remained:

1. **Mixed-epoch leak (AP1)**: `check_convergence` only populated the map for
   sources with `pg_version > 0`.  A source that had been indexed into Qdrant
   or Neo4j but had no Postgres documents (e.g. a newly registered source whose
   first documents had not yet committed) was absent from the map.  The retrieval
   layer treated absence as "no ceiling known → pass through" (fail-open), leaking
   future-epoch evidence for that source.

2. **Unobservable leak rate (AP6)**: No metric was emitted when hits from
   unmapped sources slipped through.  The leak rate was invisible to operators.

3. **Undocumented guarantee change (AP6)**: The per-source semantics represent a
   deliberate relaxation of the cross-source causal-temporal invariant — a
   composed answer now legitimately mixes epochs ACROSS sources (each source's
   evidence is pinned to that source's own epoch, not a single global snapshot).

---

## Decision

### 1. Complete map population (v11-AP1)

`GlobalReconciler.check_convergence` now iterates the **union** of source ids
known to any store (pg ∪ neo4j ∪ qdrant).  Every source receives an explicit
entry in `per_source_watermark`:

- Sources with `pg_version > 0`: ceiling = `min(pg, neo4j, qdrant)` as before.
- Cold sources (`pg_version == 0` or absent from pg): ceiling = `0` (explicit).
- Sources known only to neo4j or qdrant (projection-only): ceiling = `0`.

This ensures the map is always **complete** — no source can be absent.

### 2. Fail-closed not-in-map policy (v11-AP1)

`_apply_watermark_filter` now operates **fail-closed** by default
(`strict_epoch=True`):

- A hit whose source has **no entry** in the map AND has a non-`None`
  `applied_version` is **DROPPED**.
- A hit with `applied_version is None` always passes through regardless of
  map membership (no version info → no ceiling to enforce; typically graph
  nodes injected by the anchor stage rather than versioned vector chunks).

The old fail-open behaviour (pass-through for unmapped sources) is available
via `GraphRAGComposer(strict_epoch=False)` — explicitly opt-in, not the
default, and not recommended for production deployments.

**Availability trade-off**: in the window between a source being registered in
neo4j/qdrant and its first document being committed to Postgres, that source's
hits will be dropped.  This is intentional — better to serve no evidence than
mixed-epoch evidence.  The `conformance-live` gate (v10-AP6) exercises this
path with a real cold source to prevent regression.

### 3. Leak-rate metric (v11-AP1 / v11-AP6)

A new Prometheus counter is emitted by the retrieval layer:

```
omniscience_graphrag_epoch_dropped_unmapped_total
```

This counts hits dropped by the fail-closed policy (source absent from the
watermark map, `applied_version` not `None`).  A sustained non-zero rate
signals a reconciler gap or a newly-indexed source not yet visible in Postgres.

### 4. `version is None` pass-through policy

Both the vector-hit filter (`_apply_watermark_filter`) and the graph-node
filter (`_apply_graph_watermark_filter`) treat `applied_version / version is
None` as "no ceiling" — the hit or node passes through unconditionally.  This
handles:

- Graph nodes surfaced by the anchor stage that carry no version metadata.
- Vector chunks written before versioning was introduced (legacy data).

The `None` rate is observable via the existing structlog events; a dedicated
metric can be added if None-rate observability becomes operationally important.

### 5. Per-source epoch semantics (causal-temporal implication)

A composed GraphRAG answer **legitimately mixes epochs across sources**.
Source A's evidence is pinned to source A's own watermark; source B's evidence
is pinned to source B's own watermark.  The two watermarks may differ.

This means:

- For queries spanning a single source: the answer reflects a causally
  consistent snapshot of that source's documents.
- For queries spanning multiple sources: the answer reflects each source's
  *own* consistent snapshot, but those snapshots may be at different wall-clock
  times.  A causal-temporal join across sources (e.g. "what did team A know
  when team B updated service X?") is not guaranteed to be consistent.

Operators who require strict cross-source consistency (rare) should:
1. Use a single-source workspace, OR
2. Apply an explicit `as_of` timestamp so both stores filter to the same
   logical time, OR
3. Set `strict_epoch=False` and implement cross-source version pinning at the
   application layer.

---

## Consequences

**Positive**:

- Mixed-epoch evidence from cold or projection-only sources is prevented.
- Leak rate is observable via `omniscience_graphrag_epoch_dropped_unmapped_total`.
- The per-source semantics are now explicitly documented.

**Negative**:

- A newly registered source will have its hits dropped until its first
  Postgres document is committed (typically sub-second, but observable in
  fast-ingestion scenarios).
- Cross-source causal-temporal consistency is NOT guaranteed (documented above).

**Neutral**:

- `strict_epoch=False` provides a backward-compatible escape hatch for
  operators who need to opt out of the fail-closed policy.
- The `version is None` pass-through is unchanged from v10 behaviour.

---

## Related decisions

- ADR-0005: GraphRAG staged pipeline (original watermark design)
- ADR-0008: Bitemporal schema for Neo4j (source of `version` field semantics)
- consilium-v9-AP4: nullable confidence introduction (superseded confidence
  propagation for None-versioned nodes)
