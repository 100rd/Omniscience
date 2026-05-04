# ADR-0011 — Deprecation schedule for the `k8s-agentic` connector

- **Status**: Accepted
- **Date**: 2026-04-30
- **Deciders**: dev-team Lead, Architect, Security Engineer, QA Engineer
- **Issue**: [#168](https://github.com/100rd/Omniscience/issues/168)
- **Implements**: the commitment in
  [ADR-0007 §6](0007-k8s-operator-architecture.md#6-cross-document-consequences)
  to set the deprecation date once operator parity is reached.
- **Builds on**: [ADR-0010](0010-server-side-emitter-dedup.md) (server-side
  emitter dedup; the during-cutover gate that makes parallel running safe).
- **Closes** epic [#98](https://github.com/100rd/Omniscience/issues/98) — K8s
  operator GA.

## Context

The legacy agentic Kubernetes connector at
`packages/connectors/src/omniscience_connectors/agentic/k8s.py` was the v0.1
bridge between Omniscience and customer Kubernetes clusters.  It uses an
LLM agent to decide which `kind`s to index and a polling REST loop to
fetch them.

[ADR-0007](0007-k8s-operator-architecture.md) committed to replacing
this with an in-cluster Go operator
(`omniscience-operator`, `operator/`) for three structural reasons:
freshness budget, causal-edge fidelity, and per-cluster reach.
ADR-0007 §6 explicitly deferred the deprecation date:

> `packages/connectors/src/omniscience_connectors/agentic/k8s.py` is
> **not deprecated by this ADR**.  The operator runs in parallel through
> GA.  A follow-up issue in epic #98 will set the deprecation date once
> the operator's coverage matches the connector's.

This ADR is that follow-up.  By the time issue #168 is picked up, the
operator stack is in place:

| Capability | Issue | Status |
|---|---|---|
| Operator scaffold + Pod watcher | #101 | merged |
| Workload watchers (Deployment, RS, SS, DS, Job) | #157 | merged |
| Network + Config watchers (Service, Endpoints, Ingress, NetworkPolicy, ConfigMap, Secret) | #158 | merged |
| Cluster-scoped watchers (Namespace, Node, PV, StorageClass, Cluster anchor) | #159 | merged |
| ArgoCD CRD coverage (Application, ApplicationSet) | #160 | merged |
| Argo Rollouts CRD coverage | #161 | merged |
| DRA CRD coverage | #162, #190 | merged |
| Reconciliation worker (drift correction) | #163 | merged |
| Server-side dedup (operator-wins) | #164 / ADR-0010 | merged (#195) |
| Operator metrics + Grafana + alerts | #165 | merged (#194) |
| Helm chart hardening (PSA, NetworkPolicy, image scanning) | #166 | merged |
| Multi-cluster identity (`cluster_id` UUID) | #167 | merged |

The remaining open work for full v0.4 default-disable parity is the
**NOT-COVERED** rows in
[`docs/connectors/k8s-agentic-vs-operator-parity.md`](../connectors/k8s-agentic-vs-operator-parity.md).
Those gaps are **out of scope for issue #168** — this issue announces
the deprecation and pins the schedule; the gaps gate the v0.4 cut, not
the v0.3 announcement.

## Decision

We pin a three-version deprecation schedule for the `k8s-agentic`
connector:

### v0.3 — deprecation announcement (this issue)

- The agentic connector remains **fully shipped and functional**.  No
  removal, no behaviour change, no extras flip.
- Importing
  `omniscience_connectors.agentic.k8s` raises a single
  `DeprecationWarning` at module-import time.  The warning carries:
  - the removal version string `v0.5.0`
  - the migration target name `omniscience-operator`
  - a URL to the migration guide
    (`docs/connectors/k8s-agentic-deprecation.md`)
- The migration guide and the parity matrix ship in
  `docs/connectors/`.
- The CHANGELOG's **`[0.3.0] → Deprecated`** section announces the
  schedule and links to the guide.
- The `pyproject.toml` extras for the connector are documented but
  **not yet flipped** — `omniscience-connectors[k8s-agentic]` resolves
  to the same module set as the base install.
- The Helm chart README for the operator
  (`helm/omniscience-operator/README.md`) gets a "Migrating from
  k8s-agentic?" pointer to the migration guide.
- **Server-side dedup** ([ADR-0010](0010-server-side-emitter-dedup.md))
  is the during-cutover gate.  It already lives on `main` from #164/#195
  and silently drops agentic events for any pair the operator is
  authoritative on.  A non-zero rate on
  `omniscience_ingestion_dedup_drop_total{authority_emitter="k8s-operator"}`
  is the visible "operator is taking over" signal customers watch
  during the parallel window.

### v0.4 — opt-in default-disabled

- The agentic module **moves out** of the base install.  Customers who
  want to keep using it must explicitly install
  `omniscience-connectors[k8s-agentic]`.
- The server-side feature flag `OMNISCIENCE_K8S_AGENTIC_ALLOWED` is
  **already wired** in the dedup gate as of issue #216 (v0.3).  The
  v0.4 cut only flips the operator-shipped default from `true` to
  `false`; no code change is required at v0.4 release time.  When
  `false`, the gate short-circuits every event with
  `source_type="k8s-agentic"` BEFORE any DB lookup; the existing
  `omniscience_ingestion_dedup_total{action="agentic_flag_disabled"}`
  counter increments per drop.  (The ADR originally named a separate
  `omniscience_ingest_emitter_disallowed_total` counter; #216 chose
  to reuse the existing dedup-action counter with a new action label
  to avoid extra metric cardinality and keep dashboards consistent.)
- Customers still on the agentic path at v0.4 have a single Helm /
  env-var change to roll back: `OMNISCIENCE_K8S_AGENTIC_ALLOWED=true`.
  This is the **rollback switch**.
- **The v0.4 cut is gated on closing every NOT-COVERED row in the
  parity matrix.**  See the [Verification gates](#verification-gates)
  section.
- A v0.4 changelog entry announces the opt-in default-disable and
  re-cites the migration guide.

### v0.5 — removal

- `packages/connectors/src/omniscience_connectors/agentic/k8s.py` and
  its tests, fixtures, Helm references, and CHANGELOG dedup-during-deprecation
  test are deleted.
- The `[k8s-agentic]` extra is removed from `pyproject.toml`.
- The connector registry entry is removed from
  `omniscience_connectors/__init__.py`.
- A v0.5 changelog entry under **Removed** announces the deletion.
- A one-final-warning is logged on Omniscience server start if any
  Source row in the database still has `connector_type="k8s-agentic"`
  — pointing at the migration guide URL with the explicit "this Source
  is now non-functional" wording.

## Why operator-wins dedup is the right gate (cross-link to ADR-0010)

The deprecation schedule depends on **server-side dedup** being the
"both paths can run safely" mechanism during the v0.3 → v0.4 window.
ADR-0010 details the persistent-state operator-wins state machine; the
load-bearing properties for *this* schedule are:

- **No undefined ordering.**  When both paths emit for the same
  `(workspace_id, external_id)`, exactly one wins.  No graph-store
  corruption.
- **Operator is authoritative for its covered kinds.**  ADR-0010's
  state machine pins the operator as the winner once it emits, so
  customers can safely run both paths during cutover without worrying
  about agentic clobbering operator state.
- **24-hour TTL for rollback.**  The `OMNISCIENCE_INGEST_DEDUP_TTL_HOURS`
  default of 24 means the agentic path can re-take authority within
  24 h of an operator outage — this is the rollback budget customers
  rely on if the operator misbehaves during the cutover window.
- **Sticky mode for paranoid customers.**  Setting
  `OMNISCIENCE_INGEST_DEDUP_TTL_HOURS=0` makes operator authority
  permanent; useful when the operator is the only K8s emitter and a
  misconfigured agentic instance must never reclaim a row.

The v0.3 → v0.4 transition reuses this gate; v0.4 adds the
`OMNISCIENCE_K8S_AGENTIC_ALLOWED=false` posture which short-circuits
even before dedup runs.

## Why we don't shorten the v0.3-to-v0.5 timeline

A faster timeline (e.g. v0.4 = removal) would be operationally
hostile:

- Customers on the v0.3 line need at least one minor-version cycle to
  observe the deprecation warning, plan migration, and execute it.
  Skipping straight from "announcement" to "removal" violates the
  contract that deprecation warnings provide a *grace period*.
- The opt-in extra in v0.4 is the customer-facing rollback path.
  Removing the connector entirely without that step would leave
  customers with stuck pipelines and no in-version remediation.
- The v0.4 server-side feature flag is the "default-disabled" surface;
  it is the cheapest possible enforcement mechanism (one env var, no
  module deletion).  Shipping it before the removal lets us gather
  a release of telemetry on customer flag-flips before pulling the
  module.

A slower timeline (e.g. v0.6 = removal) is acceptable in principle;
we pin v0.5 as the **earliest** removal release.  The actual timing
will be confirmed at v0.4 cut against customer telemetry.

## Verification gates

Each release-line transition is gated on:

### v0.3 (this issue)

- [x] `DeprecationWarning` emitted on import; tested in
      `tests/test_agentic_deprecation.py`.
- [x] Migration guide at
      `docs/connectors/k8s-agentic-deprecation.md`.
- [x] Parity matrix at
      `docs/connectors/k8s-agentic-vs-operator-parity.md`.
- [x] CHANGELOG `[0.3.0] → Deprecated` entry citing the guide.
- [x] Connector code unchanged except the warning.
- [x] All existing pytest, mypy, ruff gates green.

### v0.4 default-disable (future, not part of this issue)

- [ ] **All NOT-COVERED kinds in the parity matrix have closed gap
      issues.**  Specifically: LimitRange, PersistentVolumeClaim,
      ResourceQuota, ServiceAccount, CronJob, Role, RoleBinding,
      ClusterRole, ClusterRoleBinding, HorizontalPodAutoscaler.
      `ReplicationController` is acceptable as a v0.5 gap pending
      telemetry confirmation.
- [ ] Customer telemetry confirms operator emit-rate match against the
      agentic baseline for at least 30 days on at least 3 distinct
      production clusters.
- [ ] Server-side feature flag
      `OMNISCIENCE_K8S_AGENTIC_ALLOWED` wired with closed-posture
      default (`false`) and emitter-disallowed metric.
- [ ] Helm chart `values.yaml` documents the v0.3 → v0.4 upgrade
      flow with a config snippet.

### v0.5 removal (future, separate issue)

- [ ] No production customer reports agentic-as-only-source.
- [ ] All v0.4 customers have either migrated or explicitly opted into
      `omniscience-connectors[k8s-agentic]`.
- [ ] Removal PR deletes the module, its tests, fixtures, registry
      entry, and Helm references in a single change with a clear
      rollback path (revert the PR).

## Alternatives considered

### Alternative A — single-release removal (v0.3 = remove)

Rejected.  Removes the grace period customers rely on; violates the
implicit contract of a deprecation warning ("you have at least one
minor-version cycle to migrate").

### Alternative B — five-release removal (v0.3 announce, v0.7 remove)

Rejected as too long.  The agentic path costs operational and security
complexity to keep alive (kubeconfig handling on the server, broad
RBAC, polling load).  Three minor versions is the right balance
between customer migration time and platform-side maintenance.

### Alternative C — keep agentic indefinitely as a fallback for clusters where the operator can't run

Rejected per ADR-0007's commitment to the operator as **the** K8s
pathway.  Maintaining a second pathway forever doubles the
ongoing cost and re-creates the very dual-write problem ADR-0010
exists to fix.  Customers stuck on pre-1.27 K8s can stay on v0.3 / v0.4
until they upgrade; this is documented in the migration guide's
"Kubernetes API version skew" troubleshooting section.

### Alternative D — back-compat shim that proxies agentic output through operator-shaped emit semantics

Rejected.  A shim hides the cutover instead of forcing it; customers
would never feel pressure to actually move.  The shim's existence
would also block future operator-side schema evolution because the
shim has to remain semantically faithful to the agentic shape.

## Consequences

### Positive

- A clear, dated migration timeline customers can plan against.
- A working dedup gate (ADR-0010) means the parallel-running window is
  safe; customers can run both paths during cutover without graph
  corruption.
- The v0.4 server-side feature flag is the rollback switch — operational
  remediation in a single env-var change, no code rollback needed.
- The parity matrix is a forcing function for closing the operator's
  remaining coverage gaps before v0.4.

### Negative

- The 10 NOT-COVERED kinds are real gaps that block the v0.4 cut.
  They are individually small but collectively non-trivial; closing
  them is at least 10 issues' worth of work in epic #98 (or its
  successor).
- Customer migration is operational work for them: install operator
  chart, configure NATS, run parallel, monitor, disable agentic.  We
  cannot automate this server-side because the operator runs in the
  customer's cluster.

### Risks

- **A customer skips v0.4 and upgrades v0.3 → v0.5 directly.**  At v0.5,
  their agentic Source becomes non-functional with no in-version
  remediation.  Mitigation: the v0.5 changelog under **Breaking
  changes** will be loud, and the v0.5 server start-up logs a
  prominent warning when an agentic Source row is found in the DB.
- **A customer's cluster runs a kind we mis-identified as covered.**
  Mitigation: the parity matrix lists every kind explicitly with a
  cross-reference to the operator's mapper file; the matrix is
  re-audited at v0.4 cut, and customers can challenge it via issue
  before the cut.
- **The dedup gate misbehaves during the parallel window.**  Mitigation:
  ADR-0010's `OMNISCIENCE_DEDUP_ENABLED=false` kill switch reverts to
  v0.2 dual-write posture; the migration guide's "Rollback faster than
  the TTL allows" section documents the runbook.

## Revisit triggers

- Customer telemetry at v0.4 cut shows fewer than expected migrations
  → extend v0.4 by one minor (v0.5 stays announce-only, v0.6 = removal).
- A NOT-COVERED kind closure is delayed past the v0.4 release schedule
  → drop the v0.4 default-flip until the gap closes; deprecation
  warning continues to fire.
- A new operator-only feature creates a customer-visible quality gap
  in the agentic path (e.g. an MCP query that needs operator-side data)
  → the v0.4 cut becomes more urgent; customers feel the pressure
  organically.

## References

- [Issue #168](https://github.com/100rd/Omniscience/issues/168) — this issue
- [ADR-0007](0007-k8s-operator-architecture.md) — operator architecture
- [ADR-0007 §6](0007-k8s-operator-architecture.md#6-cross-document-consequences) — original deprecation-date commitment
- [ADR-0010](0010-server-side-emitter-dedup.md) — server-side dedup gate
- [Migration guide](../connectors/k8s-agentic-deprecation.md)
- [Parity matrix](../connectors/k8s-agentic-vs-operator-parity.md)
