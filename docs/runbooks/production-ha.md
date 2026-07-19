# Omniscience Production-HA Qualification Runbook

Runbook for the production-HA profile in `helm/omniscience` (issue #350,
[docs/specs/gh-issue-350-production-ha.md](../specs/gh-issue-350-production-ha.md)).
Covers how to supply evidence, what the posture model means, how to run the
qualification checks, and what remains a decision-return outside this chart's
scope.

## Table of contents

- [Posture model](#posture-model)
- [Supplying production evidence](#supplying-production-evidence)
- [Scheduling policy (AC-HA-2)](#scheduling-policy-ac-ha-2)
- [Running the qualification checks](#running-the-qualification-checks)
- [Known pre-existing chart defects](#known-pre-existing-chart-defects)
- [Blocked on external — decision returns](#blocked-on-external--decision-returns)

## Posture model

The chart computes `omniscience.io/posture` (rendered on every object) —
it is **not** a value you set directly. This is the REQ-OPS-6 posture
mapping (`specs/SPEC-OPS-operational-evidence.md`):

| Posture | How it's reached |
|---|---|
| `evaluation` | REQ-OPS-6 fallback: `production.enabled=false` and no `production.evidence.*` field populated (the chart's own default) |
| `governed` | `production.enabled=false`, but at least one `production.evidence.*` field has been populated by hand (external stateful dependency wired ahead of a full production cutover) |
| `production-ha` | `production.enabled=true` **and** every guard below passed, including the REQ-OPS-6 guard that retention and reconciliation both stay enabled |

A caller cannot `--set omniscience.io/posture=production-ha` — the label is
derived, and `production.enabled=true` only renders anything at all if the
fail-closed guard in `helm/omniscience/templates/_production_guards.tpl`
passes. See [docs/architecture.md](../architecture.md#production-ha-posture-helm)
for the architectural summary.

## Supplying production evidence

Set `production.enabled=true` and populate every field under
`production.evidence` (AC-HA-1, AC-HA-3). All fields are **required** when
`production.enabled=true`; an empty/invalid one aborts the render and names
the exact missing key:

```yaml
production:
  enabled: true
  evidence:
    account: "111111111111"          # AWS account id — dedicated-account boundary
    cluster: "omniscience-prod-eks"  # EKS cluster name/ARN
    region: "us-east-1"
    zones: ["us-east-1a", "us-east-1b", "us-east-1c"]   # exactly 3
    rds:
      endpoint: "omniscience-prod.xxxxx.us-east-1.rds.amazonaws.com"
      multiAz: true                   # must be true
    jetstream:
      externalUrl: "nats://jetstream-prod.internal:4222"
      replicas: 3                     # must be >= 3
    neo4j:
      topology: "causal-cluster"      # or "aura-managed"
      entitlementRef: "secret/neo4j-license#key"   # REFERENCE ONLY, never a literal license
    qdrant:
      topology: "distributed"
      nodes: 3                        # must be >= 3
```

`entitlementRef` fields are pointers (a Kubernetes Secret key, a
managed-service plan id, a procurement ticket URL) — the chart never accepts
or renders a literal license key or credential in values.

You must also disable all four bundled subcharts when going to production
(AC-HA-5 — bundled stateful charts are evaluation-only):

```bash
--set postgresql.enabled=false --set neo4j.enabled=false \
--set qdrant.enabled=false --set nats.enabled=false
```

## Scheduling policy (AC-HA-2)

Enforced and rendered only when `production.enabled=true`:

- `replicaCount >= 3` (guard-enforced; also `production.autoscaling.minReplicas >= 3`)
- `topologySpreadConstraints`: `maxSkew: 1`, `topologyKey: topology.kubernetes.io/zone`,
  `whenUnsatisfiable: DoNotSchedule` (hard — pods that can't satisfy the spread
  are left `Pending`, never bin-packed into one zone)
- `PodDisruptionBudget`: `maxUnavailable: 1`
- `HorizontalPodAutoscaler` (`autoscaling/v2`): `minReplicas: 3`, scaling on CPU utilization
- `PriorityClass` (`<release>-critical-read`): assigned to the Deployment via `priorityClassName`

All four are additive net-new templates (`templates/{pdb,hpa,priorityclass}.yaml`
plus the Deployment's `topologySpreadConstraints`/`priorityClassName` blocks)
and render exclusively under `production.enabled=true` — the default
evaluation install is unaffected.

**PriorityClass blast radius.** `<release>-critical-read` sets
`value: 100000` with `globalDefault: false` (`values.yaml`
`production.priorityClass.value`) — it will never silently become the
cluster's default priority, and it only renders under the explicit
`production.enabled=true` opt-in. But `PriorityClass` is a cluster-scoped
object: 100000 sits well above the implicit `0` most workloads run at in a
shared cluster, so any pod carrying this class can preempt lower-priority
pods from *other* namespaces/teams on the same cluster, not just within this
release. Before enabling `production.enabled=true` on a shared (non-dedicated)
cluster, the operator should confirm 100000 doesn't collide with another
team's priority-class numbering and is consistent with that cluster's overall
priority/preemption policy.

## Running the qualification checks

```bash
# 1. Structural lint
helm lint helm/omniscience --set secrets.postgresPassword=x \
  --set serviceAccount.create=false --set postgres.enabled=false --set retention.enabled=true \
  --set production.enabled=true --set postgresql.enabled=false --set neo4j.enabled=false \
  --set qdrant.enabled=false --set nats.enabled=false --set replicaCount=3 \
  --set production.evidence.account=111111111111 --set production.evidence.cluster=c \
  --set production.evidence.region=us-east-1 \
  --set 'production.evidence.zones[0]=us-east-1a' \
  --set 'production.evidence.zones[1]=us-east-1b' \
  --set 'production.evidence.zones[2]=us-east-1c' \
  --set production.evidence.rds.endpoint=x --set production.evidence.rds.multiAz=true \
  --set production.evidence.jetstream.externalUrl=x --set production.evidence.jetstream.replicas=3 \
  --set production.evidence.neo4j.topology=x --set production.evidence.neo4j.entitlementRef=x \
  --set production.evidence.qdrant.topology=x --set production.evidence.qdrant.nodes=3

# 2. Rendered-object + posture assertions
uv run pytest tests/test_production_ha_render.py tests/test_helm_posture.py -v
```

`.github/workflows/ha-qualification.yml` runs both on every PR/push touching
`helm/omniscience/**` or the test files.

## Known pre-existing chart defects

Discovered while building this profile. The dead-subchart-dependency defect
below is **resolved**; the remaining app-wiring defect is **not fixed here**
— out of this task's scope (`docs/specs/gh-issue-350-production-ha.md` scope
excludes `apps/**`/`packages/**`, and it is a separate wiring defect, not
part of the production-HA acceptance criteria):

- **Resolved: dead subchart dependencies removed.** `Chart.yaml` previously
  declared `postgresql`/`neo4j`/`qdrant`/`nats` subchart dependencies; two of
  the four repository URLs (`neo4j`, `qdrant`) returned HTTP 404 from the
  public internet, which blocked `helm template`/`helm install` entirely
  (Helm requires every declared dependency archive under `charts/` before it
  renders anything, regardless of `condition:`) unless `helm dependency
  build` had succeeded. The `dependencies:` block has been removed from
  `Chart.yaml` and the four stateful services are now modeled purely via
  `values.yaml` toggles (`postgresql.enabled`, `neo4j.enabled`,
  `qdrant.enabled`, `nats.enabled`, all default `false` — see AC-HA-5). `helm
  template`/`helm install`/`helm lint` all render directly with no
  dependency-build step, and the qualification test suite's rendered-object
  assertions run unconditionally (they only skip if the `helm` binary itself
  is missing from PATH).
- **Resolved: nil-pointer crash on a bare render.** An earlier draft of this
  runbook reported that bare `helm lint`/`helm template` (no `--set` at all)
  crashed with `nil pointer evaluating interface {}.create` on
  `templates/serviceaccount.yaml` (`.Values.serviceAccount` had no default)
  and similarly for `.Values.postgres.enabled` (`templates/pvc.yaml`). That
  was accurate against the tree at the time it was written but predates the
  values-default fix: `values.yaml` now ships `serviceAccount: {create:
  false, ...}` and `postgres: {enabled: false, storageSize: "10Gi", ...}` as
  defaults. Verified empirically: `helm template helm/omniscience --set
  secrets.postgresPassword=x` (the one remaining required value, gated by
  `templates/secret.yaml`'s `required`) renders cleanly with no nil-pointer
  and no other `--set` overrides needed. The `serviceAccount.create=false` /
  `postgres.enabled=false` overrides still shown in the qualification
  commands above are harmless no-ops against the current defaults, kept for
  explicitness rather than because they route around a live crash.
- **Remaining, out of scope: bundled-toggle app wiring is incomplete.**
  `postgresql.enabled`/`neo4j.enabled`/`qdrant.enabled`/`nats.enabled` are
  plain value toggles with no subchart behind them — enabling one does not
  deploy an in-cluster instance of that service; the container only talks to
  Postgres/NATS via the separate `postgres.*`/`nats.*` connection settings,
  and Neo4j/Qdrant have no env-var wiring in `configmap.yaml`/`secret.yaml`
  at all — a container claiming `neo4j.enabled`/`qdrant.enabled` still has no
  way to actually reach a Neo4j/Qdrant instance through this chart. Same
  pre-existing `postgres`/`postgresql` key-naming mismatch noted above (one
  "l" vs two) also affects `retention`/`config.retention` (see
  `values.yaml` comments). This is a separate app-wiring defect, not part of
  the production-HA acceptance criteria for issue #350, and is not fixed
  here.
- **Resolved: the real `postgres.enabled` toggle is now guarded under
  production.** `_production_guards.tpl` previously only rejected the dead
  bitnami-style `postgresql.enabled` (double "l", unused since the subchart
  `dependencies:` block was removed) and never checked the toggle actually
  consumed by `templates/pvc.yaml` — `postgres.enabled` (single "l"). That
  gap let `production.enabled=true` render successfully alongside a live,
  non-Multi-AZ, PVC-backed local Postgres, contradicting AC-HA-1/AC-HA-5's
  "RDS is the Multi-AZ authority" requirement. The guard now also fails
  closed when `postgres.enabled=true` under `production.enabled=true`.

## Blocked on external — decision returns

Per the task-spec's execution order, the following cannot be completed from
this repository checkout and are named here rather than defaulted or waived:

- **AC-HA-4 (live fault-domain probe).** Surviving one pod/node/AZ fault with
  a measured SLO and PostgreSQL ledger-hash/JetStream-ack evidence requires a
  real, disposable-or-approved 3-AZ Kubernetes cluster with live traffic —
  categorically unavailable from this control-plane checkout, and explicitly
  out of scope ("destructive stateful failover against a production
  environment"; no AWS account/EKS mutation authority here). The schema,
  scheduling-policy, and evidence guards in this chart are the portable,
  repo-local half of AC-HA-4's prerequisites; the fault injection and SLO
  measurement itself must run against a real qualification environment.
- **Neo4j license entitlement / Qdrant managed-plan selection.** Explicitly
  out of scope ("selecting or purchasing Neo4j/Qdrant licenses or
  managed-service plans"). The chart only models a reference slot
  (`production.evidence.neo4j.entitlementRef`) that a human populates after
  procuring — it does not and must not default to Community-vs-Enterprise
  Neo4j or auto-select a Qdrant Cloud plan.
- **Real account/cluster/RDS/JetStream endpoint identity.** The chart
  requires these be supplied (fail-closed if empty) but cannot generate or
  verify them against a real AWS account from this environment; populating
  them with real values is itself out of scope here.
