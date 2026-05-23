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
Symptoms include 5xx error rate spike + Datadog monitor
`db.connections.exhausted` firing for more than 5 minutes.

## Triage

1. Open the [pool dashboard](https://app.datadoghq.com/dashboard/abc).
2. Confirm the saturation is real (some monitors flap on cold-start).

```bash
kubectl -n payments top pods --selector app=payments-api
```

## Mitigate

Scale the pool out by 50% — this is reversible.

```bash
kubectl -n payments scale deploy/payments-api --replicas=8
```

## Resolve

Wait for the pool gauge to drop below 70% then scale back to baseline.
