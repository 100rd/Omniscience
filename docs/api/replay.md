# Replay API — "What did the agent see at time T?"

> Status: Implemented in v0.5 (issue #243).  Surfaces the existing
> bitemporal substrate (ADR-0008) as a public Agentic Data Plane
> (ADP) primitive.

## Why this primitive exists

Every AI-agent procurement RFP since Q1 2026 contains the same
question, almost verbatim:

> "When the agent made decision X at time T, what data did it have?
> Can we reproduce that decision today and prove the response is the
> same?"

The Omniscience graph has already been bitemporal since ADR-0008
landed — every `:Entity` carries `valid_from`, `valid_to` and
`recorded_at`, and the read path's `as_of` parameter exposes the
ADR-0008 §5 canonical predicate.  What was missing was a **first-class
governance-facing endpoint** that packages those primitives as a
turnkey "replay" surface a CISO/GRC reviewer can point at without
understanding the underlying graph schema.

Concrete unlocks:

1. **CISO/GRC sales motion** — regulated K8s shops (the v0.5 wedge
   ICP) buy through compliance reviewers, not SREs.  Replay is the
   single most-asked feature in agentic-AI RFPs.
2. **Foundation for v2 Action Mode** — provable rollback ("if we
   re-run the decision now with the same context, do we get the same
   recommendation?") sits on top of the replay primitive.
3. **Differentiation** — no competitor (HolmesGPT, OpenSRE,
   Anyshift, Resolve AI, Komodor) exposes a public replay endpoint
   as of v0.5; the proprietary partial-replay UIs in Datadog Bits
   and PagerDuty Ops Cloud are vendor-locked.

## API surface

### MCP tool — `replay_context`

```jsonc
// Inline mode: replay an arbitrary tool at an arbitrary T.
{
  "name": "replay_context",
  "arguments": {
    "at_time": "2026-05-15T14:23:01Z",
    "tool_name": "get_related_entities",
    "arguments": {
      "entity_name": "svc.payments.checkout",
      "max_depth": 2
    }
  }
}

// Audit-id mode: re-run a previously-recorded invocation.
{
  "name": "replay_context",
  "arguments": {
    "audit_log_id": "8a7e5e1d-...-1e3c"
  }
}
```

### REST endpoint — `POST /api/v1/replay`

```jsonc
// Inline mode.
{
  "at_time": "2026-05-15T14:23:01Z",
  "query": {
    "tool_name": "get_related_entities",
    "arguments": {
      "entity_name": "svc.payments.checkout",
      "max_depth": 2
    }
  }
}

// Audit-id mode.
{
  "audit_log_id": "8a7e5e1d-...-1e3c"
}
```

Convenience accessor for the admin UI (single GET, no body):

```
GET /api/v1/replay/audit/{audit_log_id}
```

### Response envelope

```jsonc
{
  "tool_name": "get_related_entities",
  "at_time": "2026-05-15T14:23:01+00:00",
  "state_fingerprint":
    "5b8c...e4f1",                              // 64 hex chars
  "fingerprint_algorithm": "blake2b-256-canonjson-v1",
  "original_state_fingerprint":
    "5b8c...e4f1",                              // null in inline mode
  "fingerprint_match": true,                    // null in inline mode
  "audit_log_id": "8a7e5e1d-...-1e3c",          // null in inline mode
  "response": {
    /* ...the exact payload the original tool returned... */
  }
}
```

`fingerprint_match` is `null` whenever the caller did not supply an
`audit_log_id` (there is nothing to compare against).  When `false`,
the agent's context at T has been retroactively modified — see
**Drift detection** below.

## State-fingerprint hash

* **Algorithm**: BLAKE2b truncated to 256 bits, prefixed by the
  domain-separation tag `b"omniscience-replay-v1:"`.
* **Encoding**: canonical JSON (`sort_keys=True`,
  `separators=(",", ":")`, `ensure_ascii=False`, UTC-ISO datetimes,
  string UUIDs/Decimals).
* **Output**: 64-char lowercase hex string.
* **Algorithm label**: stored alongside the digest as
  `fingerprint_algorithm`.  A future v2 algorithm bump (e.g. canonical
  CBOR) will increment the version suffix and coexist with v1 rows in
  the audit-log table.

Two replays at the same `at_time` against the same indexed graph
state always produce the same fingerprint — that is the load-bearing
property the CISO/GRC reviewer compares.

## Audit-log persistence

Every original tool call (search / get_entity / get_related_entities /
resolve_incident) records one row in the `audit_log` table:

| column                  | meaning                                    |
| ----------------------- | ------------------------------------------ |
| `id`                    | opaque UUID — surfaced as `audit_log_id`   |
| `workspace_id`          | ACL invariant — every read scopes by it    |
| `tool_name`             | the MCP tool the caller invoked            |
| `arguments`             | JSONB — the wire arguments                 |
| `response`              | JSONB — the exact payload returned         |
| `state_fingerprint`     | 64-char BLAKE2b-256 hex over `response`    |
| `fingerprint_algorithm` | algorithm tag                              |
| `confidence`            | optional float (resolve_incident only)     |
| `recorded_at`           | server wall-clock                          |
| `as_of`                 | bitemporal anchor T (NULL = still-current) |

The table is **append-only** by contract.  No UPDATE or DELETE path is
exposed from the application code; retention is managed out-of-band
per ADR-0009 (#138).

## Example flows

### Incident post-mortem audit

> "The 04:11 oncall recommendation suggested rolling back PR #482.
>  Was that recommendation correct given what we knew at 04:11?"

1. Find the audit row for the original `resolve_incident` call
   (admin UI -> Replay page, filter by alert id).
2. Click "Replay" — the page re-runs `resolve_incident` against the
   graph state at the recorded `recorded_at`.
3. Compare `state_fingerprint` with `original_state_fingerprint`.
   If equal: the recommendation is reproducible — the conclusion was
   sound given the context.  If different: a retroactive correction
   landed (e.g. a missing PR was back-filled) — the page highlights
   the new fields.

### Regulator inquiry

> "Show me what your agent saw at 2026-05-12T18:00Z when it decided
>  to drain pod X."

1. POST `/api/v1/replay` with `at_time = 2026-05-12T18:00Z` and the
   original `tool_name + arguments` from the agent's trace.
2. Hand the regulator the `state_fingerprint`.  They can verify
   independently by re-issuing the same POST against any read replica
   — same anchor, same hash.

### Agent-decision review

> "We changed the prompt last week.  Does it still produce the same
>  result against last week's graph?"

1. Re-run the new prompt against the old graph by passing
   `at_time = <last-week>` to the relevant MCP tools.
2. Compare the fingerprint with the fingerprint logged in the
   audit row from last week.  Matching fingerprints prove the prompt
   change is non-regressive on that decision.

## Drift detection

`fingerprint_match = false` is **not an error**.  It means a write
landed at the recorded `as_of` AFTER the original tool call ran —
that is the legitimate ADR-0008 §2 retroactive-correction scenario.
The admin UI marks divergence with a yellow banner and shows the
field-level diff; it does not return a non-2xx status.

True replay errors (audit row missing, naive datetime, unknown
tool_name) surface as the standard `{"error": {"code": ..., ...}}`
envelope on the REST side, or as a `ValueError` with the
`code:message` form on the MCP side.

## Determinism boundaries

The deterministic-hash contract holds within these boundaries:

* Same Omniscience version + same Neo4j version.
* Same indexed graph state.  Re-indexing reshuffles which `:EntityState`
  is current at T — that is a graph mutation, not a replay bug, and
  divergence is surfaced via `fingerprint_match = false`.
* Same `fingerprint_algorithm`.  A future v2 bump deliberately
  invalidates v1 hashes on the same payload.

The contract intentionally does NOT cover:

* Cross-version JSON schema changes (an added optional field changes
  the canonical-JSON bytes).  The `fingerprint_algorithm` tag lets us
  coordinate that across upgrades.
* External-system reads we do not own (e.g. the live Slack thread
  body that `resolve_incident` cites).  The audit-log row stores the
  cited text verbatim so the replay can return the same string even
  if the upstream is gone — but if a future tool starts streaming
  fresh data through the response, that field will diverge.

## ACL invariant

Every replay call (REST or MCP) requires:

* scope `search` — replay surfaces the same data the underlying tools
  surface, so we share the scope rather than introducing a new one.
* a **workspace-scoped token** — graph reads are workspace-scoped;
  replay is no exception.  Unscoped tokens are rejected with 403.
* the audit row's `workspace_id` MUST match the caller's
  `workspace_id`.  Cross-workspace replay returns 404
  `audit_log_not_found` — existence is never leaked outside the owning
  workspace.

## References

* ADR-0008 — Bitemporal graph model.
* ADR-0009 — Tiered retention (#138 — manages audit-log volume).
* Issue #133 — `as_of` parameter on every read path.
* Issue #243 — this primitive.
