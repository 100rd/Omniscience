# Blast-radius API

> Issue: [#234](https://github.com/100rd/Omniscience/issues/234) (epic [#230](https://github.com/100rd/Omniscience/issues/230) H5 Track 1).
> Surfaces: MCP tool `blast_radius` + REST `GET /api/v1/blast-radius`.

The blast-radius tool answers a single change-management question:

> *If we perform `action_type` on `entity_id`, what downstream entities
> are impacted?*

It composes the existing bitemporal graph traversal
(`GraphStore.find_related` → `_TRAVERSE_*_CYPHER_TEMPLATE`) with an
action-aware edge-type allowlist and a deterministic impact-scoring
heuristic. No new Cypher templates are introduced — the action layer is
a pure-Python composition on top of the v0.2 read path.

## Endpoints

### MCP tool

```jsonc
{
  "name": "blast_radius",
  "input": {
    "entity_id":   "service://payments-api",
    "action_type": "restart",           // optional, default "restart"
    "max_depth":   3,                   // optional, default 3, clamped to [1, 5]
    "as_of":       "2026-05-22T12:00:00Z" // optional ISO-8601 UTC datetime
  }
}
```

### REST

```
GET /api/v1/blast-radius?entity_id=service%3A%2F%2Fpayments-api
                       &action=restart
                       &max_depth=3
                       &as_of=2026-05-22T12%3A00%3A00Z
```

Both surfaces return the same JSON shape:

```jsonc
{
  "seed_entity_id": "service://payments-api",
  "action_type":   "restart",
  "max_depth":     3,
  "impacted": [
    {
      "entity_id":       "service://orders-api",
      "entity_type":     "service",
      "dependency_path": [
        { "from_entity": "service://orders-api",
          "to_entity":   "service://payments-api",
          "edge_type":   "CALLS" }
      ],
      "impact_score": 0.80,
      "confidence":   1.0
    },
    {
      "entity_id":       "service://shipping-api",
      "entity_type":     "service",
      "dependency_path": [
        { "from_entity": "service://shipping-api",
          "to_entity":   "service://orders-api",
          "edge_type":   "CALLS" }
      ],
      "impact_score": 0.48,
      "confidence":   1.0
    }
  ],
  "effective_as_of": "2026-05-22T12:00:00Z",
  "meta": {
    "action_type":     "restart",
    "edge_allowlist":  ["CALLS", "DEPENDS_ON", "ROUTES_TO"],
    "max_depth":       3,
    "scoring_model":   "v0.1-deterministic"
  }
}
```

`impacted` is ranked descending by `impact_score`; ties are broken by
depth (closer first) and then lexicographically on `entity_id` for
determinism.

## Action-type semantics

Each action carries an **edge-type allowlist** — the BFS traversal
follows only the listed edges out of the seed.  The allowlist is what
makes the response "the things that break if we do X" rather than "all
the things this entity touches".

| `action_type` | What it models | Edges followed |
|---|---|---|
| `restart` | Transient unavailability while the seed is bouncing.  Anything that calls the seed over the network or declares a hard runtime dependency on it goes dark for the restart window. | `DEPENDS_ON`, `CALLS`, `ROUTES_TO` |
| `delete` | Permanent removal.  Widest blast — runtime callers go dark **and** the management plane around the seed (CI/CD pipelines, load-balancers fronting the seed, scheduling, owning teams) loses its referent. | All of the above plus `LOAD_BALANCED_BY`, `OWNED_BY`, `DEPLOYED_BY`, `SCHEDULED_ON`, `RUNS_ON` |
| `scale_down` | Degraded capacity.  Like `restart` plus the load path — when a service shrinks behind a load-balancer, the LB itself is impacted (uneven backend distribution / capacity loss). | `restart` set plus `LOAD_BALANCED_BY` |
| `cordon` | Quarantine.  Models "no new work" — the workload itself keeps serving until evicted, so call/route edges do **not** propagate.  Only scheduling edges (the nodes that the seed is scheduled onto) are surfaced so an operator can see which capacity disappears from the pool. | `SCHEDULED_ON`, `RUNS_ON` |

> **Why not just "all edges from the seed"?**
> Because two different actions on the same entity produce two different
> blast radii.  A `restart` of a service does not break its owning team
> page; a `delete` does (the page references an entity that no longer
> exists).  Filtering at the traversal layer keeps the response signal
> high — operators don't have to mentally subtract irrelevant
> neighbours.

## Impact score (v0.1 deterministic heuristic)

The score for each impacted entity is:

```
impact_score = action_multiplier × depth_decay × edge_weight
```

clipped to `[0.0, 1.0]`, where:

- `action_multiplier` is per-action:

  | Action       | Multiplier |
  |--------------|-----------:|
  | `delete`     | 1.0 |
  | `restart`    | 0.8 |
  | `scale_down` | 0.6 |
  | `cordon`     | 0.5 |

- `depth_decay = 0.6 ** (depth - 1)` — first hop is the full score;
  each subsequent hop halves-ish.
- `edge_weight` is per-edge, defaulting to `0.6` for unknown allowlisted
  edges.  Runtime-causal edges weigh more than ownership edges:

  | Edge type           | Weight |
  |---------------------|-------:|
  | `DEPENDS_ON`, `CALLS` | 1.0 |
  | `ROUTES_TO`         | 0.9 |
  | `LOAD_BALANCED_BY`  | 0.7 |
  | `SCHEDULED_ON`, `RUNS_ON` | 0.7 |
  | `DEPLOYED_BY`       | 0.5 |
  | `OWNED_BY`          | 0.4 |

> This is a **v0.1 placeholder**, not a calibrated probability.  Callers
> should treat the score as a ranking signal between entities in the
> same response, not an absolute "how bad is this on a scale of 0 to 1"
> number.  A calibrated model is a follow-up to issue #234.

## Confidence

`confidence` is `1.0` when:

- `as_of` is `None` (still-current read against the `:Entity` mirror — the
  source of truth), or
- both the seed and the neighbour carry a populated bitemporal triple
  (`recorded_at` is non-null) at `as_of`.

`confidence` is `0.5` when the row is a legacy (pre-#130 backfill)
entity and the bitemporal triple is partial — we still surface the
neighbour, but the caller is told the time-anchoring is best-effort.

## Bitemporal anchoring (ADR-0008 §5)

`as_of` is forwarded verbatim to `GraphStore.get_entity` and
`GraphStore.find_related`.  When set, every node and every edge in the
traversal satisfies the canonical predicate:

```
valid_from <= as_of AND (as_of < valid_to OR valid_to IS NULL)
```

An `as_of` in the past with an empty `impacted` list surfaces
`meta.degraded_response = "as_of_before_recorded_history"` so an empty
response is not misinterpreted as "no dependencies".

## ACL invariant (issue #117 / ADR-0005)

- `workspace_id` is taken from the caller's bearer token, **never** from
  input.  Every `GraphStore` call carries it.
- A foreign-workspace `entity_id` returns `entity_not_found` (HTTP 404
  on REST, `ValueError("entity_not_found:...")` on MCP) — the same
  response a non-existent entity would produce.  Existence is never
  leaked across workspaces.

## Errors

| Surface | Code | When |
|---|---|---|
| REST 400 / MCP `invalid_entity_id` | empty / blank seed |
| REST 400 / MCP `invalid_action_type` | `action_type` not in `{restart, delete, scale_down, cordon}` |
| REST 400 / MCP `invalid_timezone`   | `as_of` naive or non-UTC |
| REST 403 (REST only) | token has no workspace |
| REST 404 / MCP `entity_not_found` | seed missing in caller's workspace at `as_of` |

## Operational notes

- **Depth clamping.**  `max_depth` is clamped to `[1, 5]` (same envelope
  as `resolve_incident` — issue #153).  A hub entity at depth 5 already
  fans out to thousands of neighbours; deeper traversals add noise
  without recall.
- **Telemetry.**  Each request observes one sample on
  `omniscience_request_duration_seconds{surface, tool="blast_radius", as_of_kind}`.
  Use `as_of_kind` to distinguish current-state reads from historical
  ones.
- **No new Cypher.**  `blast_radius` reuses
  `_TRAVERSE_CYPHER_TEMPLATE` / `_TRAVERSE_AS_OF_CYPHER_TEMPLATE`
  from `omniscience_index.stores.neo4j_store` via the `edge_types`
  argument on `GraphStore.find_related`.  No additions to the shared
  template inventory.
