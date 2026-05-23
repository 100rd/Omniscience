# PagerDuty connector

Promotes the existing push-only PagerDuty webhook receiver
(`alerts/pagerduty`) into a full read-only source connector that also
pulls catalog and incident history via the PagerDuty REST API.

Bi-directional write-back (ack / snooze / resolve) is **deferred to v2
Action Mode** — tracked by issue [#230](https://github.com/100rd/Omniscience/issues/230).
v1 is read-only by design.

## What gets ingested

| Entity                  | Source endpoint                                  |
|-------------------------|--------------------------------------------------|
| `PagerDutyTeam`         | `GET /teams`                                     |
| `PagerDutyEscalation`   | `GET /escalation_policies`                       |
| `PagerDutyService`      | `GET /services`                                  |
| `OnCallShift`           | `GET /oncalls`                                   |
| `PagerDutyIncident`     | `GET /incidents` + `GET /incidents/{id}/log_entries` |

Edges emitted via `DocumentRef.metadata["edges"]`:

- `incident -> service` (`FILED_AGAINST`)
- `service  -> team`    (`OWNED_BY`)
- `service  -> escalation` (`USES_ESCALATION`)
- `escalation -> team`  (`OWNED_BY`)
- `on-call shift -> user` (`ON_CALL`)
- `incident -> escalation`, `incident -> assignee user`, `incident -> acknowledger user`

## Required API token scopes

PagerDuty supports two token kinds:

| Token kind                | Recommended scopes |
|---------------------------|---------------------|
| **General Access Key** (account-scoped, REST API key) | Use a **read-only key** — sufficient for v1. |
| **OAuth scopes** (if you mint a user-OAuth token instead) | `incidents.read`, `services.read`, `teams.read`, `escalation_policies.read`, `schedules.read`, `oncalls.read`, `users.read`, `abilities.read` |

The connector only issues `GET` requests in v1 — no write scopes are
required.  When v2 Action Mode lands the additional scopes
(`incidents.write`) will be requested separately and gated by a config
flag.

## Configuration

```yaml
sources:
  - name: pagerduty-prod
    connector: pagerduty
    config:
      api_base_url: https://api.pagerduty.com   # or https://api.eu.pagerduty.com
      team_ids: [PTEAM01]                       # optional filter
      service_ids: [PSVC001]                    # optional filter for incidents
      incident_lookback_days: 30
      include_resolved_incidents: true
    secrets:
      api_token: ${PAGERDUTY_API_TOKEN}
      # Inherited from the existing alerts/pagerduty webhook receiver:
      webhook_secret: ${PAGERDUTY_WEBHOOK_SECRET}
```

## Webhooks

The webhook receiver is **shared verbatim** with
`alerts/pagerduty` — same signature verification, same secret rotation,
same replay protection.  See [docs/connectors/alerts.md](alerts.md) for the
webhook endpoint shape; nothing changes from the operator's perspective.

## Rate limiting

PagerDuty's REST API limits each token to **960 req/min** (account-scoped).
The connector honours `Retry-After` on HTTP 429 with up to 5 retries and a
60s cap per sleep.  Pagination uses `offset` + `more` with a page size of
100 (the documented max).

## ACL invariant

Workspace identity is **always** derived from the owning `Source.tenant_id`
row.  The connector never reads `tenant_id`, `account_id`, custom-detail
keys, or any other payload field for tenancy decisions.

## Tests

See `tests/test_pagerduty_connector.py` for unit + integration coverage.
HTTP responses are recorded as JSON fixtures under
`tests/fixtures/pagerduty/` and replayed via `respx` — running the test
suite twice produces byte-identical output (no live network).

## Future work

- [ ] v2 Action Mode: ack / snooze / resolve (#230)
- [ ] Webhook-driven incremental sync (today: webhook payload normalised
      separately by the alerts receiver; a future change can route
      webhook deltas back into the catalog connector for hot-path updates)
