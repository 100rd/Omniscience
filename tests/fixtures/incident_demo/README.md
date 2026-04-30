# Incident Demo Fixture (issue #154)

Deterministic in-process fixture for the headline `resolve_incident` demo
path of epic #99.  Lives separately from the test code so reviewers can
read the planted incident without parsing Python.

## Layout

* `workspace_a.json` — the incident under test.  All canonical names
  here are what `mcp_resolve_incident(alert_id, workspace_id=ws_a)`
  must return.
* `workspace_b.json` — a parallel dataset with **overlapping entity
  names** (same pod name, same PR-URL form).  None of these may appear
  in workspace_a's response.  The fixture exists to harden the
  workspace boundary contract from #117 / ADR-0005.

## Anchored timestamps

`alert_fired_at = 2026-04-12T19:30:00+00:00` is the anchor.  All other
times are derived from it so the fixture is deterministic across CI
runs.

| Entity | Offset from alert | Purpose |
|---|---|---|
| Alert | 0 | Seed |
| Responsible PR | -4h | Inside `PR_RECENCY_WINDOW_SECONDS` (24h) → high confidence |
| Older PR | -7d | Outside window → must NOT be the top recommendation |

## Canonical name forms (per Wave-1 connector contracts)

| Kind | Name | Source |
|---|---|---|
| Alert | `alert://pagerduty/INC-2026-04-12-001` | #151 |
| Pod | `pod/api-7f9b9d4c-xj4kp` | #157 / #151 cross_ref |
| Responsible PR | `https://github.com/acme/api/pull/4242` | #150 |
| Older PR | `https://github.com/acme/api/pull/4099` | #150 |
| Related Slack thread | `slack://channel/C0INCIDENTS/thread/1744486200.001100` | #149 |
| Unrelated Slack thread | `slack://channel/C0RANDOM/thread/1744000000.000700` | #149 |
| Trace | `trace://4bf92f3577b34da6a3ce929d0e0e4736` | #152 |

## ACL invariant

`workspace_b.json` plants entities whose canonical names **collide**
with workspace_a's:

* Same pod name (`pod/api-7f9b9d4c-xj4kp`) under a different alert
* Same PR URL form against a parallel repo
* A Slack thread that mentions workspace_a's pod by name

A correctly-implemented `mcp_resolve_incident` must never surface any
of those workspace_b nodes when called with `workspace_id=ws_a`,
regardless of name overlap, because the store call is
`workspace_id`-scoped (#117 / ADR-0005).
