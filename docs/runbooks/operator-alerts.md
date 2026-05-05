# Omniscience Operator — Alert Runbook

Operational runbook for the alerts shipped in
`helm/omniscience-operator/templates/prometheusrule.yaml` (issue #165).
Each section documents:

- What the alert is telling you.
- The most likely root causes, in priority order.
- Concrete first-three diagnostic commands an on-call can paste.
- The escalation path when the first three commands don't resolve it.

> Anchor IDs (`#operatorpublishstalled`, etc.) are referenced from each
> alert's `runbook_url` annotation in `prometheusrule.yaml` — do not
> rename sections without updating the chart in lockstep.

## Table of contents

- [OperatorPublishStalled](#operatorpublishstalled)
- [OperatorPublishErrorBurst](#operatorpublisherrorburst)
- [OperatorReconcileFailing](#operatorreconcilefailing)
- [OperatorEmitLagHigh](#operatoremitlaghigh)
- [OperatorReconcileDriftHigh](#operatorreconciledrifthigh)

## Conventions

- Examples assume the operator is installed via the
  `omniscience-operator` Helm chart in the namespace `omniscience-system`
  with `--release-name omniscience-operator`. Adjust to your install.
- PromQL examples assume the operator's `omniscience_operator_*`
  metrics are scraped (`Values.serviceMonitor.enabled=true` plus
  `kube-prometheus-stack` or equivalent).
- Where commands take a kind label, substitute the kind from the
  alert's labels (e.g. `kind="Pod"`).

---

## OperatorPublishStalled

**Fires when**: `time() - omniscience_operator_last_publish_unix_seconds > 300`
for at least 5 minutes, **gated on** `last_publish_unix_seconds > 0` (cold-start
safe — a freshly-started operator that has not yet emitted does not page).

**Severity**: warning (paging in PagerDuty parlance: P2).

**Most likely root causes** (priority order):

1. **NATS JetStream is unreachable** — operator pod can't write events.
   By far the most common cause; usually a NetworkPolicy egress rule
   regression or a NATS pod outage.
2. **NATS credentials Secret was rotated** without restarting the
   operator. The operator reads the credfile at startup and does not
   reload it.
3. **No watch is producing events** — empty cluster, RBAC was
   tightened so `LIST`/`WATCH` is denied on every kind, or a custom
   admission webhook is rejecting all writes upstream.
4. **The `last_publish_unix_seconds` gauge is wedged** — extremely
   rare, indicates a bug in the publisher's success-path metric
   update; check operator logs for `metrics_set_failed` events.

**First three diagnostic commands**:

```bash
# 1. Confirm the operator pod is running and recent.
kubectl -n omniscience-system get pods -l app.kubernetes.io/name=omniscience-operator
kubectl -n omniscience-system describe pod -l app.kubernetes.io/name=omniscience-operator | grep -E "Status|Restart|LastState"

# 2. Tail the operator log for NATS / publish errors in the last 15m.
kubectl -n omniscience-system logs deploy/omniscience-operator --since=15m \
  | grep -E "nats|publish|connect|nack|no_responders" | tail -40

# 3. Confirm what the operator thinks the stream subject is, and that
#    something is bound to it on the NATS side.
kubectl -n omniscience-system exec deploy/omniscience-operator -- env \
  | grep -E "OMNISCIENCE_NATS|NATS_URL|NATS_SUBJECT"
# Then from a NATS-cli pod or jump host:
nats stream info OMNISCIENCE_INGEST   # adjust stream name to your install
nats stream subjects OMNISCIENCE_INGEST
```

If the stream lookup returns "stream not found" or the subjects don't
include the operator's configured subject, that's the root cause —
either the ingestion server hasn't created the stream yet (server
side: see ingestion-worker runbook) or the operator is misconfigured.

**Escalation path**:

- If commands 1–3 show NATS reachable + subject bound + fresh logs but
  the gauge still hasn't advanced: capture
  `kubectl -n omniscience-system logs deploy/omniscience-operator --since=1h > /tmp/op.log`,
  the output of `kubectl -n omniscience-system get events --sort-by=.lastTimestamp | tail -50`,
  and a snapshot of `omniscience_operator_publisher_inflight` and
  `omniscience_operator_events_emitted_total` over the past hour.
  Page the platform team that owns the operator (typically the SRE
  rotation owning #omniscience-operator).
- If NATS itself is down or unreachable: this is no longer an operator
  incident — page the messaging-platform on-call and follow the NATS
  runbook. The operator will recover automatically once NATS returns.

---

## OperatorPublishErrorBurst

**Fires when**: `sum(rate(omniscience_operator_publisher_errors_total[5m])) by (error_type) > 0.5`
for at least 10 minutes.

**Severity**: warning.

**Most likely root causes** — disambiguate via the `error_type` label
on the firing alert:

- `error_type="connect"`: NATS endpoint unreachable from the operator
  pod. NetworkPolicy egress, DNS, or a NATS pod outage.
- `error_type="publish"`: JetStream stream missing, subject not bound,
  or stream retention exhausted. The operator can connect but the
  write is rejected.
- `error_type="ack_timeout"`: JetStream is alive but slow — disk
  saturation, replication lag, or upstream backpressure on the
  consumer.
- `error_type="nats_no_responders"`: the subject has zero subscribers.
  Most common cause is an ingestion-worker outage on the consumer
  side (the operator publishes successfully but no one is reading).

**First three diagnostic commands**:

```bash
# 1. Which error_type is bursting? (run this first — drives the rest.)
kubectl -n monitoring exec -ti prometheus-kube-prometheus-stack-prometheus-0 -- \
  promtool query instant http://localhost:9090 \
  'sum(rate(omniscience_operator_publisher_errors_total[5m])) by (error_type)'

# 2. For "connect" or "nats_no_responders": check NATS reachability
#    and subscription state.
kubectl -n omniscience-system exec deploy/omniscience-operator -- nslookup nats.omniscience-system
nats consumer ls OMNISCIENCE_INGEST   # from a NATS-cli pod

# 3. For "publish" or "ack_timeout": tail operator logs scoped to the
#    publisher and capture the JetStream-side stream state.
kubectl -n omniscience-system logs deploy/omniscience-operator --since=10m \
  | grep -E "publish|jetstream|nack|ack_timeout" | tail -30
nats stream info OMNISCIENCE_INGEST
```

**Escalation path**:

- `connect` / `nats_no_responders` that doesn't resolve in 15m: page
  the messaging-platform on-call. The operator alone cannot fix
  NATS-side issues — it will recover automatically once the upstream
  is healthy.
- `publish` / `ack_timeout` that persists across a NATS restart:
  capture
  `nats stream report OMNISCIENCE_INGEST`, the operator log slice for
  the affected window, and any JetStream replication lag dashboards.
  Page the platform team that owns ingestion stream provisioning.

---

## OperatorReconcileFailing

**Fires when**: `increase(omniscience_operator_reconcile_runs_total{result="error"}[1h]) > 2`.

**Severity**: info (P3) — the reconciler is for drift detection; live
event publishing is unaffected.

**Most likely root causes**:

1. **Omniscience server's read API is unreachable** from the operator
   pod (NetworkPolicy egress, DNS, server outage). The reconciler
   calls
   `GET <OMNISCIENCE_API_BASE_URL>/api/v1/operator/entities?cluster_id=…&kind=…`
   — if this returns network error or 5xx, the reconcile run records
   `result="error"`.
2. **The operator's API bearer token was rotated** without restarting
   the operator. The token is mounted from a Secret at startup and
   not re-read. Every subsequent request returns 401.
3. **The server's read endpoint is returning 5xx for one specific
   `kind`** — partial outage. The `kind` label on the failing reconcile
   run narrows it down.
4. **Cluster-side `LIST` is failing** for the kind (e.g. RBAC
   regression, CRD removed mid-flight). The reconciler counts these
   as errors too.

**First three diagnostic commands**:

```bash
# 1. Which kinds are failing, and at what rate?
promtool query instant http://localhost:9090 \
  'sum(rate(omniscience_operator_reconcile_runs_total{result="error"}[15m])) by (kind)'

# 2. Confirm the server's read endpoint is reachable from the operator
#    pod (use the operator's actual base URL + bearer token).
kubectl -n omniscience-system exec deploy/omniscience-operator -- env \
  | grep -E "OMNISCIENCE_API_BASE_URL|OMNISCIENCE_API_BEARER"
kubectl -n omniscience-system exec deploy/omniscience-operator -- sh -c \
  'curl -sS -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Bearer $(cat /etc/omniscience/operator-api-token)" \
    "$OMNISCIENCE_API_BASE_URL/api/v1/operator/entities?cluster_id=$OMNISCIENCE_CLUSTER_ID&kind=Pod&limit=1"'
# 200 = healthy, 401 = token rotation, 403 = RBAC, 404/5xx = server side.

# 3. Reconciler-specific log slice.
kubectl -n omniscience-system logs deploy/omniscience-operator --since=1h \
  | grep -E "reconcile|drift|entities_client" | tail -40
```

**Escalation path**:

- `401`/`403` from step 2: rotate the bearer Secret AND restart the
  operator (`kubectl -n omniscience-system rollout restart deploy/omniscience-operator`).
  If the rollout doesn't fix it, capture the request/response and page
  the team that owns the Omniscience server's auth layer.
- `5xx` from step 2 for a specific kind: capture the kind, the
  request URL, and the server-side log slice for that endpoint. Page
  the team that owns ingestion / read-API.
- All-kinds failure from step 1 with a healthy step-2 response: the
  failure is cluster-side `LIST` (RBAC or CRD churn). Check
  `kubectl auth can-i list <kind> --as=system:serviceaccount:omniscience-system:omniscience-operator`
  for each affected kind, then page whoever owns cluster RBAC.

---

## OperatorEmitLagHigh

**Fires when**: `histogram_quantile(0.99, sum(rate(omniscience_operator_event_lag_seconds_bucket[5m])) by (le, kind)) > 30`
for at least 15 minutes.

**Severity**: info. Freshness SLO breach.

**Freshness SLO**: p99 event lag (publish wall-clock minus
`obj.CreationTimestamp` or last-applied annotation) ≤ 30s for any
single kind, sustained over a 15-minute window. This is an
**internal** SLO for graph freshness — not a customer-facing
contract; the customer-facing freshness SLO is set product-side and
typically allows up to 2 minutes for end-to-end visibility.

**Most likely root causes** (priority order):

1. **Informer cache pressure** — memory contention or watch resync
   storms. Look at `omniscience_operator_informer_cache_objects` for
   the affected kind: a sudden jump indicates a resync; sustained
   growth indicates a leak or unexpected cluster scale.
2. **Publisher backlog** —
   `omniscience_operator_publisher_inflight{kind="…"}` non-zero for
   sustained periods (>60s) means events are queued faster than NATS
   can ack.
3. **NATS RTT inflation under load** — slow JetStream acks bleed
   into the lag observation. Cross-check with
   `OperatorPublishErrorBurst{error_type="ack_timeout"}` — if both
   alerts fire, treat as a NATS-side incident.
4. **Cluster clock skew** — if a kind's lag suddenly jumps to
   negative or wildly large values it's a clock issue, not a real
   freshness regression. The mapper's `RecordEmit` clamps negative
   lag to 0, so this manifests as lag suddenly hitting the upper
   buckets.

**First three diagnostic commands**:

```bash
# 1. Which kind is dragging the p99 up?
promtool query instant http://localhost:9090 \
  'topk(5, histogram_quantile(0.99, sum(rate(omniscience_operator_event_lag_seconds_bucket[5m])) by (le, kind)))'

# 2. Is publisher backlog the culprit?
promtool query instant http://localhost:9090 \
  'topk(5, max_over_time(omniscience_operator_publisher_inflight[15m]))'

# 3. Is informer cache growing pathologically for this kind?
promtool query range http://localhost:9090 \
  'omniscience_operator_informer_cache_objects{kind="<KIND_FROM_ALERT>"}' \
  --start "$(date -u -d '1 hour ago' +%s)" --end "$(date -u +%s)" --step 60s
```

**Escalation path**:

- If publisher inflight is the cause and clears within 15m of NATS
  recovering: no escalation needed, alert auto-resolves.
- If informer cache is growing without bound: capture pod memory
  usage (`kubectl top pod -n omniscience-system`), the cache-objects
  graph for the past 24h, and the cluster-side object count for the
  kind (`kubectl get <kind> --all-namespaces | wc -l`). Page the
  operator team.
- If lag is high for ONE specific kind only and the publisher is
  healthy: likely a controller-runtime watch desync — restart the
  operator and watch the cache size. If it recurs, file an issue
  with the kind name and a 24h cache-size graph.

---

## OperatorReconcileDriftHigh

**Fires when**: `rate(omniscience_operator_reconcile_drift_total[1h]) > 0.000277`
(more than 1 drift detection per hour).

**Severity**: info. Drift detection is the reconciler's correctness
backstop — non-zero drift means live publish missed something.

**Most likely root causes**:

1. **A watch was lost and the informer cache is stale.** The
   controller-runtime watch will resync on its own (default 10m), but
   between drop and resync the operator emits no events for the
   affected kind. The reconciler catches the gap and emits the drift
   counter. Verify with `omniscience_operator_informer_cache_objects`
   for the kind around the drift event — a dip-and-recover pattern
   confirms a resync.
2. **Server-side ingestion dropped events** for this `kind` +
   `direction`. The dedup gate, an upstream NATS issue, or the
   ingestion worker rejecting events on a schema mismatch will all
   produce drift. Server-side this surfaces as
   `omniscience_ingestion_dedup_total{action="…dropped"}` spikes — see
   step 3 below for the cross-correlation query.
3. **A network partition between operator and NATS that recovered
   but left the in-flight buffer cleared.** Events in flight at
   partition time are lost; the reconciler picks up the gap.
4. **The `direction` label on the drift counter narrows it down**:
   `direction="missing_in_graph"` means the operator never emitted;
   `direction="extra_in_graph"` means the graph has stale entities the
   live cluster no longer has (operator missed a delete event).

**First three diagnostic commands**:

```bash
# 1. Which kind + direction is drifting?
promtool query instant http://localhost:9090 \
  'sum(increase(omniscience_operator_reconcile_drift_total[6h])) by (kind, direction)'

# 2. Was there a recent watch resync for this kind? (Look for cache
#    dip-and-recover.)
promtool query range http://localhost:9090 \
  'omniscience_operator_informer_cache_objects{kind="<KIND_FROM_ALERT>"}' \
  --start "$(date -u -d '6 hours ago' +%s)" --end "$(date -u +%s)" --step 60s

# 3. Cross-correlate with server-side ingestion drops in the same window.
promtool query range http://localhost:9090 \
  'sum(rate(omniscience_ingestion_dedup_total{action=~".*dropped.*"}[15m])) by (action, kind)' \
  --start "$(date -u -d '6 hours ago' +%s)" --end "$(date -u +%s)" --step 300s
```

**Escalation path**:

- One-shot drift event that auto-resolves (no further drift in the
  next hour): no escalation. The reconciler corrected it; this is
  the system working as designed.
- Sustained drift (>3 events in an hour) for a single kind: capture
  the kind, direction, the cache-size graph for the past 6h, and the
  server-side ingestion dedup graph. Page the operator team.
- Drift across many kinds simultaneously: very likely a NATS-side
  incident. Page the messaging-platform on-call AND the ingestion
  team; cross-correlate with `OperatorPublishStalled` /
  `OperatorPublishErrorBurst`.

---

## Maintenance

- New alerts must be added with both:
  - A rule entry in `helm/omniscience-operator/templates/prometheusrule.yaml`.
  - A section in this runbook with the `runbook_url` anchor matching
    the alert name (lowercase, no spaces, no `Omniscience` prefix).
- The cold-start gate pattern (`and on() X > 0`) is mandatory for any
  rule that can fire during a fresh operator boot.
- When postmortem evidence shows a root cause this runbook missed,
  add it to the **Most likely root causes** list with a
  `(<incident-id>)` reference so the order stays evidence-driven over
  time.
