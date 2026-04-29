# Alerts connector (PagerDuty + Datadog)

The `alerts` connector ingests SRE incident webhooks from PagerDuty and Datadog
and turns them into first-class entities in the Omniscience graph so the
`resolve_incident` MCP tool (Wave 2 of epic [#99]) can traverse from an alert
to the resource it fires against.

> Source: [`packages/connectors/src/omniscience_connectors/alerts/`](../../packages/connectors/src/omniscience_connectors/alerts/)

## What it does

- **Push-only**. `discover()` is a no-op, `fetch()` raises
  `NotImplementedError`. Alerts arrive exclusively through signed HTTP
  webhooks delivered to `POST /api/v1/ingest/webhook/{source_name}`.
- **Two providers**: `pagerduty` and `datadog`. The provider is fixed per
  source; one `Source` row per `(tenant, provider)` pair so signature
  verification is unambiguous.
- **Emits one `alert` entity per upstream alert** with canonical name
  `alert://{provider}/{provider_alert_id}`. The ID is the upstream stable
  identifier (PagerDuty `incident.id`, Datadog `event.id`) so the entity is
  stable across the alert lifecycle (`triggered` -> `acknowledged` ->
  `resolved`).
- **Emits cross_ref target refs** for every named entity mentioned in the
  payload — AWS ARN, Kubernetes pod, service, OTel trace id. The
  `EntityLinker` resolves these via `exact_name`/`arn_match` and creates
  `[:FIRES_AGAINST]` cross_ref edges automatically.

## ACL invariant — workspace_id is ALWAYS from `Source.tenant_id` (P0)

This is the single most important rule in this connector. State it
explicitly to anyone touching the code.

Webhook payloads are **tenant-writable upstream content**. PagerDuty and
Datadog payloads can be sent by any caller who possesses the per-source
signing secret — but the **secret authenticates the source, not the
payload**. The connector and webhook handler MUST derive workspace identity
solely from the `Source.tenant_id` of the row resolved by the URL-path
`{source_name}`. They MUST NOT read any of the following payload fields for
tenancy decisions, even as a fallback or convenience:

- `tenant_id`
- `customer_id`
- `org_id`
- `routing_key`
- `account_id`
- `team`
- PagerDuty `incident.service.id`
- Datadog `event.tags`
- Custom details / tags / extensions of any kind

Any code path that reads any of those fields to influence workspace
identity is a P0 ACL leak. The cross-workspace regression test in
[`tests/test_alerts_webhook.py::test_cross_workspace_acl_payload_tenant_id_is_ignored`](../../tests/test_alerts_webhook.py)
plants forged tenant identifiers in every reasonable field and asserts the
alert lands in the workspace bound to `Source.tenant_id`.

## Signature verification (mandatory)

| Provider | Header | Algorithm | Replay protection |
|----------|--------|-----------|-------------------|
| PagerDuty | `X-PagerDuty-Signature: v1=<hex>[,v1=<hex>...]` | HMAC-SHA256 of raw body | Receiver-side delivery tracker keyed on `X-PagerDuty-Webhook-Subscription-Id` |
| Datadog | `X-Datadog-Signature: <hex>` + `X-Datadog-Signature-Timestamp: <epoch_seconds>` | HMAC-SHA256 of `<timestamp>:<raw_body>` | Skew check: rejects if `\|now - timestamp\| > replay_window_seconds` (default 300s) |

- All comparisons go through `hmac.compare_digest` (constant-time).
- Failed verification returns 400 from the FastAPI receiver and emits a
  structured `webhook_signature_invalid` log line. No payload is parsed,
  no entities are emitted, no telemetry is updated.
- Missing or unrecognised signature header -> 400. There is no
  "skip verification" mode in production code.

### Webhook secret rotation

Operators may pre-stage a rotated secret with **zero downtime** by
configuring `webhook_secret` in the source's secret store as a
**newline-separated** list of currently-valid secrets:

```text
old-secret-being-retired
new-secret-being-introduced
```

Verification accepts a request as authentic if **any** listed secret yields
a matching HMAC. After confirming the upstream provider is sending with the
new secret, remove the old line from the secret store. Empty lines and
surrounding whitespace are ignored.

## Configuration

```python
from omniscience_connectors.alerts import AlertsConfig

config = AlertsConfig(
    provider="pagerduty",            # or "datadog"
    signature_algorithm="hmac-sha256",
    replay_window_seconds=300,        # Datadog skew tolerance
)
```

`webhook_secret` is supplied via the source's secrets dict, **never** in
config. PagerDuty does not transmit a timestamp, so `replay_window_seconds`
applies only to Datadog; PagerDuty replay protection relies on the
receiver's delivery tracker.

## NormalizedAlert

Both providers' payloads are normalised into a single Pydantic model:

| Field | Type | Notes |
|-------|------|-------|
| `provider` | `Literal["pagerduty", "datadog"]` | Fixed per source. |
| `provider_alert_id` | `str` | Upstream stable id. |
| `severity` | `Literal["info", "warning", "error", "critical"]` | PagerDuty severity / Datadog priority (`P1`->`critical`, `P2`->`error`, ...). |
| `status` | `Literal["triggered", "acknowledged", "resolved"]` | Lifecycle state. |
| `fired_at` | `datetime` (aware UTC) | When the alert fired upstream. |
| `summary` | `str` | Human-readable title. |
| `target_arn` | `str \| None` | First ARN extracted from payload. |
| `target_pod` | `str \| None` | Kubernetes pod (`pod/<name>` or `<ns>/<name>`). |
| `target_service` | `str \| None` | PagerDuty `service.summary` or Datadog `service:` tag. |
| `trace_id` | `str \| None` | OTel trace id (bare hex; `trace://` is added at edge time). |
| `raw` | `dict[str, Any]` | Original payload, retained for forensics. **Never** read for tenancy. |

## Cross-ref edge extraction

`extract_cross_refs(alert)` returns a list of `DocumentRef`s where the
first entry is the alert entity itself and each subsequent entry is a
named-entity target carrying a canonical name in `uri` that the
`EntityLinker` resolves:

| Target | Canonical name form |
|--------|---------------------|
| AWS ARN | `arn:aws:<service>:<region>:<account>:<resource>` |
| Kubernetes pod | `pod/<name>` (or `<ns>/<name>`) |
| Service | `service://<name>` |
| OTel trace | `trace://<hex>` (matches the canonical form emitted by [#152]) |

The linker creates `[:FIRES_AGAINST]` cross_ref edges automatically; this
connector's job is to surface the canonical strings.

## Non-goals

- No providers beyond PagerDuty + Datadog.
- No alert routing, on-call escalation, or auto-acknowledgement.
- No deduplication across providers — the same incident in both PagerDuty
  and Datadog produces two `alert` entities; the composer reasons across
  them via the shared target resource.
- No log / metric ingestion — only **events / alerts** through the alerting
  webhook.

[#99]: https://github.com/100rd/Omniscience/issues/99
[#152]: https://github.com/100rd/Omniscience/issues/152
