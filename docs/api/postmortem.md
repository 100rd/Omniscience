# Post-mortem generator API (issue #232)

Composes a templated post-mortem document from the bitemporal graph
timeline.  Each timeline row is **cited back to the entity it came
from** with the `as_of` timestamp that was used to read it; action items
are extracted into `FollowUp` entity nodes for tracking.

Two transports, identical semantics:

* **MCP tool** — `generate_postmortem(incident_id, alert_id, template, format, incident_start?, incident_end?)`
* **REST** — `GET /api/v1/incidents/{incident_id}/postmortem?alert_id=...&template=...&format=...`

Both require a workspace-scoped bearer token with the `search` scope.
Cross-workspace alerts return `404 incident_not_found` to preserve the
"no existence leak" invariant (ADR-0005 / #117).

## Templates

Three default templates ship in
`apps/server/src/omniscience_server/postmortem/templates/`:

| id | display name | When to use |
| --- | --- | --- |
| `blameless` | Blameless post-mortem | Default; Google-SRE style, focused on systemic causes. |
| `five_whys` | Five-whys analysis | The proximate cause is obvious but the latent cause is unclear. |
| `coe` | Correction of Errors (COE) | AWS-style document; explicit detection/mitigation/resolution buckets + leadership sign-off. |

Add a new template by:

1. Defining a `PostmortemTemplate` constant in `templates/__init__.py`.
2. Extending the `PostmortemTemplateId` `Literal`.
3. Adding it to `SUPPORTED_TEMPLATES` and `_REGISTRY`.

The module-level `assert set(SUPPORTED_TEMPLATES) == set(_REGISTRY.keys())`
will fail at import time if the three are out of sync.

## Bitemporal anchoring

The generator reads the timeline with `as_of=incident_end` (defaults to
"now").  This means the graph snapshot reflects the bitemporal state at
incident close — late-arriving connector data that landed _after_
`incident_end` is filtered out, so reports are reproducible.

The same `[incident_start, incident_end)` window is applied to the
event-time filter on the timeline projection.

## Citations

Every `timeline` row in the structured report carries a `Citation`
block:

```json
{
  "citation": {
    "entity_id": "pod/api-7f9b9d4c-xj4kp",
    "entity_type": "k8s_pod",
    "as_of": "2026-04-12T20:30:00+00:00",
    "source": "src-k8s-ws-pm"
  }
}
```

In markdown the citation is rendered inline as ``` `k8s_pod/pod/api-7f9b9d4c-xj4kp` @ 2026-04-12T20:30:00+00:00 ```
inside the timeline table.

## FollowUp extraction

Action items are extracted into `FollowUp` Pydantic models (kind
`followup` when promoted to the graph) using three v0.1 heuristics:

1. **Runbook step outcomes** — every recorded step with outcome
   `failed` or `rolled_back` promotes to a FollowUp.
2. **Trigger phrases** — `TODO`, `follow-up`, `investigate`, `flaky`,
   `known issue`, `tech debt` matched word-bounded against
   `after_state_summary` / `before_state_summary`.
3. **Baseline** — every post-mortem includes a "Schedule post-incident
   review meeting" P3 FollowUp so the section is never empty.

Each FollowUp carries a canonical name `followup://{incident_id}/{slug}`
so it slots into the bitemporal graph the same way runbook steps do.

A future PR (#232 v2) will plug an LLM-based extractor behind the same
interface; the heuristic version is intentionally deterministic and
dependency-free.

## REST example

```http
GET /api/v1/incidents/INC-2026-04-12-001/postmortem
    ?alert_id=alert%3A%2F%2Fpagerduty%2FINC-2026-04-12-001
    &template=blameless
    &format=markdown
    &incident_start=2026-04-12T19:25:00%2B00:00
    &incident_end=2026-04-12T20:30:00%2B00:00
Authorization: Bearer <workspace-scoped-token>
```

Returns `text/markdown` (or `text/html` / `application/json` depending
on `format`).

## MCP example

```json
{
  "tool": "generate_postmortem",
  "arguments": {
    "incident_id": "INC-2026-04-12-001",
    "alert_id": "alert://pagerduty/INC-2026-04-12-001",
    "template": "five_whys",
    "format": "markdown"
  }
}
```

Returns `{ incident_id, alert_id, template, format, rendered, report }`
where `rendered` is the format-specific string and `report` is the raw
`PostmortemReport` JSON for clients that want both views.

## Errors

| Code | HTTP | Cause |
| --- | --- | --- |
| `invalid_template` | 400 | Unknown template id. |
| `invalid_format` | 400 | Format not in `markdown`, `html`, `json`. |
| `invalid_incident_id` | 400 | Empty / oversized incident_id, or `start > end`. |
| `invalid_alert_id` | 400 | alert_id failed the `alert://` regex. |
| `invalid_timezone` | 400 | Naive or non-UTC datetime supplied. |
| `invalid_window` | 400 | `incident_start > incident_end`. |
| `forbidden` | 403 | Token lacks workspace scope. |
| `incident_not_found` | 404 | Alert not visible to the calling workspace. |

## Sample output (excerpt)

```markdown
# Post-mortem: INC-2026-04-12-001

_Template: **Blameless post-mortem** (blameless)_

| Field | Value |
| --- | --- |
| Alert | `alert://pagerduty/INC-2026-04-12-001` |
| Incident start | 2026-04-12T19:25:00+00:00 |
| Incident end | 2026-04-12T20:30:00+00:00 |
| Timeline events | 6 |
| Follow-ups extracted | 3 |

## Timeline

| # | Timestamp (UTC) | Change | Entity | Citation | Summary |
| --- | --- | --- | --- | --- | --- |
| 1 | 2026-04-12T19:28:00+00:00 | created | `k8s_pod/pod/api-7f9b9d4c-xj4kp` | `k8s_pod/pod/api-7f9b9d4c-xj4kp` @ 2026-04-12T19:28:00+00:00 | Pod entered Ready state |
| 2 | 2026-04-12T19:29:55+00:00 | created | `otel_trace/trace://4bf92f3...` | `otel_trace/trace://...` @ 2026-04-12T19:29:55+00:00 | Trace recorded p99=2.3s |
| ... | ... | ... | ... | ... | ... |

## Action items

- [ ] **[P2]** Investigate failed step 'step-2-restart-pool' from runbook://acme-ops/db-connections-exhausted.md
  _FollowUp entity: `followup://inc-2026-04-12-001/step-step-2-restart-pool-failed`_
- [ ] **[P3]** Schedule post-incident review meeting
  _FollowUp entity: `followup://inc-2026-04-12-001/schedule-review`_
```
