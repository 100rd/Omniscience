# Omniscience Operator — Alert Runbook

Stub runbook for the alerts shipped in `helm/omniscience-operator/templates/prometheusrule.yaml` (issue #165). Each section documents:

- What the alert is telling you.
- The most likely root causes, in priority order.
- The first three diagnostic commands.
- The escalation path.

> Status: stub. Sections marked **TODO** will be filled in as we accumulate
> production observations on the alert. Do not remove the TODO markers
> without a paired postmortem reference.

## Table of contents

- [OperatorPublishStalled](#operatorpublishstalled)
- [OperatorPublishErrorBurst](#operatorpublisherrorburst)
- [OperatorReconcileFailing](#operatorreconcilefailing)
- [OperatorEmitLagHigh](#operatoremitlaghigh)
- [OperatorReconcileDriftHigh](#operatorreconciledrifthigh)

---

## OperatorPublishStalled

**Fires when**: `time() - omniscience_operator_last_publish_unix_seconds > 300`
for at least 5 minutes, **gated on** `last_publish_unix_seconds > 0` (cold-start
safe — a freshly-started operator that has not yet emitted does not page).

**Severity**: warning (paging in PagerDuty parlance: P2).

**Most likely root causes**:

1. NATS JetStream is unreachable. Check: `kubectl logs deploy/omniscience-operator | grep nats`.
2. The operator's NATS credentials Secret was rotated and the operator hasn't been restarted to pick up the new file.
3. RBAC was tightened and the operator can no longer LIST any of the watched kinds (no events to publish → counter never advances).

**TODO**: add the command sequence used in the first real incident.

**Escalation**: TODO

---

## OperatorPublishErrorBurst

**Fires when**: `sum(rate(omniscience_operator_publisher_errors_total[5m])) by (error_type) > 0.5`
for at least 10 minutes.

**Severity**: warning.

**Most likely root causes** (use the `error_type` label to disambiguate):

- `error_type="connect"`: NATS endpoint unreachable from the operator pod. Check NetworkPolicy and DNS.
- `error_type="publish"`: JetStream stream missing or its retention is exhausted. Check the `OMNISCIENCE_NATS_SUBJECT` configuration matches a configured stream.
- `error_type="ack_timeout"`: JetStream is slow / overloaded. Check stream metrics on the NATS side.
- `error_type="nats_no_responders"`: nobody is bound to the subject. Most likely an ingestion-worker outage on the consumer side.

**TODO**: add the command sequence used in the first real incident.

---

## OperatorReconcileFailing

**Fires when**: `increase(omniscience_operator_reconcile_runs_total{result="error"}[1h]) > 2`.

**Severity**: info (P3).

**Most likely root causes**:

1. Omniscience server's read API is unreachable. Check `OMNISCIENCE_API_BASE_URL` from the operator pod.
2. The operator's API bearer token was rotated.
3. The server's read endpoint is returning 5xx for a specific kind (look at the `kind` label).

**TODO**: list the API endpoints the reconciler depends on.

---

## OperatorEmitLagHigh

**Fires when**: `histogram_quantile(0.99, …event_lag_seconds_bucket…) > 30s`
for at least 15 minutes.

**Severity**: info. Freshness SLO breach.

**Most likely root causes**:

1. Informer cache pressure (memory or watch resync storms) — look at `omniscience_operator_informer_cache_objects` for the kind.
2. Publisher backlog (`omniscience_operator_publisher_inflight` non-zero for sustained periods).
3. NATS RTT inflation under load.

**TODO**: define the freshness SLO in the runbook (currently 30s p99; product-side SLO TBD).

---

## OperatorReconcileDriftHigh

**Fires when**: `rate(omniscience_operator_reconcile_drift_total[1h]) > 1/h`.

**Severity**: info.

**Most likely root causes**:

1. A watch was lost and the informer cache is stale (operator should re-list on next resync; verify by checking the cache size before/after).
2. Server-side ingestion is dropping or rejecting events for this `kind` + `direction`.
3. A network partition between operator and NATS that recovered but left the buffer cleared (check NATS DLQ if configured).

**TODO**: link the server-side ingestion runbook for cross-correlation.

---

## Maintenance

- New alerts should be added with both:
  - A rule entry in `helm/omniscience-operator/templates/prometheusrule.yaml`.
  - A section in this runbook with the `runbook_url` anchor matching the alert name (lowercase, no spaces).
- The cold-start gate pattern (`and on() X > 0`) is mandatory for any
  rule that can fire during a fresh operator boot.
