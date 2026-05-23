# Runbook connector

> Status: shipped in v0.3 — issue [#231](https://github.com/100rd/Omniscience/issues/231).

The runbook connector parses CommonMark runbooks from a filesystem tree,
emits one bitemporal entity per file plus one per heading, and surfaces
matching runbooks at incident time via the `suggest_runbook` MCP tool
and the `POST /api/v1/incidents/{id}/runbook-step` REST endpoint.

This page documents the **author-facing contract** — what to put in your
runbook so it ranks highly when a matching alert fires.

## Where runbooks live

The connector walks any directory tree you point it at. A common pattern
is a per-team git repo:

```
team-runbooks/
  payments/
    db-connections-exhausted.md
    pagerduty-flap.md
  search/
    elasticsearch-yellow.md
  README.md
```

Register the source via `omniscience-cli sources add --type runbook
--root-path /var/runbooks --source-uid team-runbooks`. The `source_uid`
becomes the first segment of every canonical name the connector emits
(`runbook://team-runbooks/payments/db-connections-exhausted.md`), so
keep it short and stable.

## Front-matter

The connector parses a small subset of YAML at the very top of each
runbook, fenced with `---`. **Everything outside this block is treated
as the runbook body and ignored by the matcher** — front-matter is the
only signal that influences ranking.

```markdown
---
title: "Database connections exhausted"
alert_names:
  - "alert://datadog/db.connections.exhausted"
alert_patterns:
  - "alert://datadog/db.connections.*"
  - "alert://pagerduty/postgres-*"
tags:
  - "service:payments"
  - "service:checkout"
  - "severity:sev2"
severity: sev2
owners:
  - "team:payments"
---

# Database connections exhausted

This runbook fires when the payments-API connection pool is saturated.
…
```

### Recognised keys

| Key | Type | Purpose |
|---|---|---|
| `title` | string | Human-readable title for the responder UI. Falls back to the first `#` heading, then the filename. |
| `alert_names` | list[string] | **Exact** canonical alert names this runbook claims. Highest-weighted signal in the matcher. |
| `alert_patterns` | list[string] | `fnmatch`-style globs over alert names (`alert://datadog/db.*`). Cover whole alert families with one runbook. |
| `tags` | list[string] | Free-form labels that align with the alert tag set. Jaccard overlap contributes to the score. Conventional shape is `key:value` (`service:payments`, `severity:sev2`). |
| `severity` | string | Tie-breaker against the alert's severity. Conventional values: `sev1`, `sev2`, `sev3`, `sev4`. |
| `owners` | list[string] | Pure metadata — surfaced in the citation block but does **not** influence ranking. |

Unknown keys are kept in `raw_front_matter` and surfaced in the
`get_document` payload so external tools can read them, but the matcher
ignores them. We will add new structured keys here additively (no
breaking changes).

### YAML subset

The connector ships its own minimal YAML parser to avoid pulling a
200 KB dependency. The subset it accepts is deliberately small:

- `key: value` scalars.
- `key:` followed by block-style `- item` lines.
- Inline flow lists: `key: [a, b, c]`.
- `"` and `'` quoted strings.
- `#` line comments.

It does **not** support nested mappings, anchors, tags, JSON-flow
mappings, or multi-line scalars. Hand-edited runbooks rarely need them.

### Defensive coercion

The parser is permissive on common author mistakes:

- A single scalar where a list is expected is wrapped: `tags: just-one`
  is equivalent to `tags: ["just-one"]`.
- A missing closing `---` fence emits a `front_matter_missing_closing_fence`
  warning, drops the would-be front-matter, and parses the file as
  body-only — the suggest path still works, the runbook just has no
  matching signals.
- A duplicate heading slug emits a `duplicate_step_slug` warning and
  disambiguates with a numeric suffix (`mitigate`, `mitigate-2`).

Warnings travel with the runbook on `parse_warnings` and surface in the
suggest response so authors can find and fix them.

## Body conventions

The body of the runbook is rendered verbatim by the responder UI; we do
not require any particular structure. That said, two conventions
maximise the responder's productivity:

1. **One ATX heading per executable step.** Each `## Triage`, `## Mitigate`,
   `## Resolve` becomes a `runbook_step` entity with its own canonical
   name (`runbook://team-runbooks/db.md#mitigate`). When the responder
   acks that they've executed a step, they reference it by that step id.
2. **Put commands in fenced code blocks.** The parser extracts every
   fenced block per step (`code_blocks`) so the responder UI can render
   one-click "copy command" buttons.

```markdown
## Mitigate

Scale the pool out by 50% — this is reversible.

\`\`\`bash
kubectl -n payments scale deploy/payments-api --replicas=8
\`\`\`
```

## How suggestions are ranked

The matcher computes a single confidence score per `(runbook, alert)`
pair as a weighted sum of four features:

```
confidence = clamp(
    0.55 * exact_alert_name_match    # 1.0 iff any alert_name == alert.name
  + 0.25 * pattern_match_score       # 1.0 on first fnmatch hit
  + 0.15 * tag_jaccard               # |runbook.tags ∩ alert.tags| / |union|
  + 0.05 * severity_match,           # 1.0 iff runbook.severity == alert.severity
  0.0, 1.0,
)
```

The weights are exposed as constants in
`omniscience_connectors.runbook.linker` so a future calibrated model
(post-#231 follow-up) can swap them without renaming the public surface.

### Monotonicity invariant

Adding a matching signal to a runbook (one more exact `alert_names`
entry, one more overlapping `tags` entry) **must not lower the
confidence score** for an alert it was already matching. The integration
test `tests/integration/test_runbook_suggest.py::
test_suggest_runbook_monotonic_with_more_matching_tags` pins this in CI.

### Why `confidence` and not `relevance`?

The score is a v0.1 deterministic placeholder — we call it `confidence`
to make the calibration story explicit: a 0.6 today is *not* the same
"probability the runbook is right" as a 0.6 after #232 lands the
calibrated model. Callers should treat the score as a stable schema slot
that they can pin retries against, not a calibrated probability.

## Recording an executed step

When the responder takes an action from a runbook, the responder UI
posts to:

```http
POST /api/v1/incidents/{incident_id}/runbook-step
Content-Type: application/json
Authorization: Bearer <workspace-scoped token>

{
  "alert_id":      "alert://datadog/db.connections.exhausted",
  "runbook_name":  "runbook://team-runbooks/db-connections-exhausted.md",
  "step_id":       "mitigate",
  "outcome":       "executed",
  "actor":         "alice@payments.example.com",
  "note":          "Scaled to 8 replicas; pool gauge fell from 95% to 38%."
}
```

The event is recorded with the full bitemporal triple (`valid_from`,
`valid_to`, `recorded_at`) per ADR-0008 §1 and surfaced on:

- `GET /api/v1/incidents/{incident_id}/runbook-steps` — the per-incident
  step log the responder UI renders below the suggestion.
- The `runbook_step_executed` structured-log event — picked up by any
  log shipper for durable retention.
- The `omniscience_runbook_step_events_total` Prometheus counter —
  ops-team visibility on how often runbooks actually fire.

The recorder is process-local in v0.3 (capacity 10 000 events, FIFO
eviction). A follow-up (tracked under epic #230) promotes it to a
proper SQL table so cross-replica reads do not depend on sticky
sessions. The public function signatures are stable across that
migration.

## ACL

Every endpoint is workspace-scoped via the bearer token. Cross-workspace
alert ids return `alert_not_found` (404) — the same response a missing
alert would produce — so workspace existence is never leaked. This
mirrors the `resolve_incident` posture documented in
`docs/api/resolve-incident.md`.

## Sample runbooks

Reference runbooks live under
[`tests/fixtures/runbook/`](../../tests/fixtures/runbook/) and double as
the integration-test corpus. They are a good starting point when bringing
up a new team's runbook collection.
