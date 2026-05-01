# omniscience-operator Helm chart

In-cluster Kubernetes operator for Omniscience.  Watches K8s resources
and publishes entity events to Omniscience over NATS JetStream.  See
[ADR-0007](../../docs/decisions/0007-k8s-operator-architecture.md) for
the architecture.

## Quick start

```bash
helm install omniscience-operator ./helm/omniscience-operator \
  --namespace omniscience-operator --create-namespace \
  --set workspaceId="00000000-0000-0000-0000-000000000000" \
  --set clusterName="prod-eu-west-1" \
  --set nats.url="nats://omniscience-nats.example.com:4222" \
  --set nats.credentials="$(cat ./nats.creds)"
```

`workspaceId`, `clusterName`, and `nats.url` are **required**.  The
chart fail-fasts at install time if any are missing or invalid.  See
`values.yaml` for every override.

## Migrating from `k8s-agentic`?

If you currently run the legacy `k8s-agentic` connector
(`packages/connectors/src/omniscience_connectors/agentic/k8s.py`) on
the Omniscience server side, this operator is the replacement.  The
agentic connector is **deprecated as of v0.3** and scheduled for
removal in **v0.5** — see
[ADR-0011](../../docs/decisions/0011-k8s-agentic-deprecation-schedule.md).

The detailed migration playbook — including the side-by-side
comparison, the parallel-running window, the dedup metrics to watch
during cutover, troubleshooting (NATS, RBAC, PSA, NetworkPolicy), and
the rollback procedure — lives at:

> [`docs/connectors/k8s-agentic-deprecation.md`](../../docs/connectors/k8s-agentic-deprecation.md)

The kind-by-kind parity status (which K8s kinds the operator covers
versus the agentic connector) lives at:

> [`docs/connectors/k8s-agentic-vs-operator-parity.md`](../../docs/connectors/k8s-agentic-vs-operator-parity.md)

**Read both before starting your migration.**  In particular, the
parity matrix lists kinds where the operator does **not yet** cover
what the agentic connector does — if your deployment relies on those
kinds, the migration is gated on the gap-closure issues listed in the
matrix.

## Operational references

- Architecture: [ADR-0007](../../docs/decisions/0007-k8s-operator-architecture.md)
- Server-side dedup (during-cutover gate): [ADR-0010](../../docs/decisions/0010-server-side-emitter-dedup.md)
- Deprecation schedule: [ADR-0011](../../docs/decisions/0011-k8s-agentic-deprecation-schedule.md)
- Alerts runbook: [`docs/runbooks/operator-alerts.md`](../../docs/runbooks/operator-alerts.md)
