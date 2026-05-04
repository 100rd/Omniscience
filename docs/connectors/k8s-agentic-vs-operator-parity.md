# Parity matrix — `k8s-agentic` connector vs `omniscience-operator`

> Reference document for [issue #168](https://github.com/100rd/Omniscience/issues/168)
> (deprecation schedule).  Every kind the legacy `k8s-agentic` connector
> can emit has a row here, paired with the operator's coverage status.

This matrix is the parity reference for the v0.3 → v0.4 → v0.5
migration described in the
[migration guide](k8s-agentic-deprecation.md) and pinned in
[ADR-0011](../decisions/0011-k8s-agentic-deprecation-schedule.md).

## How the columns are derived

- **`agentic.emits`** — derived by inspection of
  `packages/connectors/src/omniscience_connectors/agentic/k8s.py`:
  - `_KIND_CORE`, `_KIND_APPS`, `_KIND_BATCH`, `_KIND_NETWORKING`,
    `_KIND_RBAC`, `_KIND_AUTOSCALING` are the explicit REST-path mappers
    the connector knows about — every kind in those tables is reachable
    by the connector's `fetch()`.
  - Subtract `_ALWAYS_EXCLUDE = {Secret, Event, TokenReview,
    SubjectAccessReview, SelfSubjectAccessReview, SelfSubjectRulesReview,
    LocalSubjectAccessReview}` — the connector enforces these as never-emit.
  - The LLM discovery loop can in principle pick *any* kind from
    `/api`+`/apis`, but the `fetch()` mapper has a fallback to a
    lowercase-plural REST path under `/apis/` — undeclared kinds will
    be attempted with best-effort.  We do **not** treat best-effort
    fallback as guaranteed coverage; this matrix only lists kinds
    with explicit `_KIND_*` mapping plus the always-excluded set
    flagged.

- **`operator.emits`** — derived by inspection of
  `operator/internal/entity/*.go` (the entity mappers) cross-checked
  against `operator/internal/controller/*.go` (the wired controllers
  registered in `operator/cmd/manager/main.go`).  A kind is `yes` only
  if there is **both** an entity mapper *and* a wired controller (or,
  for ArgoCD/DRA CRDs, a discovery-gated wiring through
  `argocd_setup.go` / `dra_setup.go`).

- **`status`** —
  - `COVERED` — agentic emits *and* operator emits.  Migration is
    seamless for this kind.
  - `PARTIAL` — operator emits but with caveats (e.g. CRD discovery
    gated; agentic field-level fidelity differs).
  - `NOT-COVERED` — agentic emits but operator does not.  **Release-blocker
    for v0.4 default-disable.** A new operator controller is required.
  - `OPERATOR-ONLY` — operator emits but agentic does not.  Listed
    here for completeness; not a parity concern for the deprecation.
  - `NEITHER` — agentic's `_ALWAYS_EXCLUDE` blocks emission; operator
    has its own posture.  Documented for transparency, not a migration
    concern.

---

## Core API group (`/api/v1`)

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `ConfigMap` | yes | yes (`configmap_controller.go`, `configmap.go`) | **COVERED** | Operator emits via watch; agentic via list. Operator data is fresher. |
| `Endpoints` | yes | yes (`endpoints_controller.go`, `endpoints.go`) | **COVERED** | Agentic includes `Endpoints` in `_KIND_CORE` despite `_DEFAULT_EXCLUDE_KINDS` listing it (the LLM can re-include it). Operator covers explicitly. |
| `Event` | **no** (`_ALWAYS_EXCLUDE`) | no | NEITHER | Both paths exclude; not relevant to deprecation. |
| `LimitRange` | yes | yes (`limitrange_controller.go`, `limitrange_mapper.go`) | **COVERED** | Operator emits via watch (issue #202). Mapper renders `spec.limits` as deterministic JSON in metadata; namespace appears only in `external_id` and base metadata, never as a metric label. |
| `Namespace` | yes | yes (`namespace_controller.go`, `namespace.go`) | **COVERED** | Operator additionally emits a synthetic `Cluster` anchor (issue #159) — strict superset. |
| `Node` | yes | yes (`node_controller.go`, `node.go`) | **COVERED** | |
| `PersistentVolume` | yes | yes (`persistentvolume_controller.go`, `persistentvolume.go`) | **COVERED** | |
| `PersistentVolumeClaim` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Operator covers `PersistentVolume` but not `PersistentVolumeClaim`. Listed in agentic's `_DEFAULT_INCLUDE_KINDS`. |
| `Pod` | yes | yes (`pod_controller.go`, `entity.go::PodToEvent`) | **COVERED** | Note: agentic's `_DEFAULT_EXCLUDE_KINDS` lists Pod, but the LLM is allowed to override and the explicit `_KIND_CORE` mapping is present. Operator covers Pod as a first-class kind. |
| `ReplicationController` | yes (`_KIND_CORE`) | **no** | **NOT-COVERED** (low priority) | Legacy kind; superseded by `ReplicaSet` upstream. Most clusters have zero `ReplicationController` resources. **Acceptable v0.5 gap** if customer telemetry confirms zero usage; flagged for explicit confirmation before v0.4 cut. |
| `ResourceQuota` | yes | yes (`resourcequota_controller.go`, `resourcequota_mapper.go`) | **COVERED** | Operator emits via watch (issue #204). Mapper renders `spec.hard`, `spec.scopes`, and `spec.scopeSelector` as deterministic JSON in metadata; namespace appears only in `external_id` and base metadata, never as a metric label. |
| `Secret` | **no** (`_ALWAYS_EXCLUDE`) | yes (`secret_controller.go`, `secret.go`, **redacted-by-default per issue #166**) | OPERATOR-ONLY | Agentic refuses to emit Secret on security grounds. Operator emits a redacted-by-default mapping (issue #166); strictly safer. Not a deprecation concern. |
| `Service` | yes | yes (`service_controller.go`, `service.go`) | **COVERED** | |
| `ServiceAccount` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Listed in agentic's `_DEFAULT_INCLUDE_KINDS`. |

---

## apps/v1

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `Deployment` | yes | yes (`deployment_controller.go`, `deployment.go`) | **COVERED** | |
| `DaemonSet` | yes | yes (`daemonset_controller.go`, `daemonset.go`) | **COVERED** | |
| `ReplicaSet` | yes | yes (`replicaset_controller.go`, `replicaset.go`) | **COVERED** | Agentic's `_DEFAULT_EXCLUDE_KINDS` lists ReplicaSet; LLM can re-include. Operator covers as first-class. |
| `StatefulSet` | yes | yes (`statefulset_controller.go`, `statefulset.go`) | **COVERED** | |

---

## batch/v1

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `Job` | yes | yes (`job_controller.go`, `job.go`) | **COVERED** | |
| `CronJob` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Operator covers `Job` but not `CronJob`. Listed in agentic's `_DEFAULT_INCLUDE_KINDS`. |

---

## networking.k8s.io/v1

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `Ingress` | yes | yes (`ingress_controller.go`, `ingress.go`) | **COVERED** | |
| `NetworkPolicy` | yes | yes (`networkpolicy_controller.go`, `networkpolicy.go`) | **COVERED** | |

---

## rbac.authorization.k8s.io/v1

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `Role` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Listed in agentic's `_DEFAULT_INCLUDE_KINDS`. RBAC indexing is a security-posture-relevant capability. |
| `RoleBinding` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Same as Role. |
| `ClusterRole` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Same as Role. |
| `ClusterRoleBinding` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Same as Role. |

---

## autoscaling/v2

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `HorizontalPodAutoscaler` | yes | **no** | **NOT-COVERED** | **Release-blocker for v0.4.** Listed in agentic's `_DEFAULT_INCLUDE_KINDS`. Customers using autoscaling rely on HPA indexing for capacity-investigation MCP queries. |

---

## storage.k8s.io/v1

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `StorageClass` | best-effort fallback (no explicit `_KIND_*` mapping; LLM can pick) | yes (`storageclass_controller.go`, `storageclass.go`) | OPERATOR-ONLY | Agentic has no explicit mapper; operator covers first-class. Strict superset for storage indexing. |

---

## CRDs (discovery-gated on the operator)

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `argoproj.io/Rollout` (Argo Rollouts) | best-effort fallback | yes (`argo_rollout_controller.go`, `argo_rollout.go`) — gated on CRD presence (issue #161) | **PARTIAL** (operator authoritative when CRD installed) | Agentic has no explicit mapper; if the LLM picks it, the fallback REST path may or may not work. Operator's coverage is gated on cluster CRD presence. |
| `argoproj.io/Application` (ArgoCD) | best-effort fallback | yes (`argocd_application_controller.go`, `argocd_application.go`) — gated on CRD presence (issue #160) | **PARTIAL** (operator authoritative when CRD installed) | Same caveat. |
| `argoproj.io/ApplicationSet` (ArgoCD) | best-effort fallback | yes (`argocd_applicationset_controller.go`, `argocd_applicationset.go`) — gated on CRD presence | **PARTIAL** (operator authoritative when CRD installed) | Same caveat. |
| `resource.k8s.io/DeviceClass` (DRA) | best-effort fallback | yes (`dra_deviceclass_controller.go`, `dra_deviceclass.go`) — gated on CRD presence (issue #190) | **PARTIAL** (operator authoritative when CRD installed) | DRA is K8s 1.30+. |
| `resource.k8s.io/ResourceClaim` (DRA) | best-effort fallback | yes (`dra_resourceclaim_controller.go`, `dra_resourceclaim.go`) — gated on CRD presence (issue #162) | **PARTIAL** (operator authoritative when CRD installed) | |
| `resource.k8s.io/ResourceClaimTemplate` (DRA) | best-effort fallback | yes (`dra_resourceclaimtemplate_controller.go`, `dra_resourceclaimtemplate.go`) | **PARTIAL** (operator authoritative when CRD installed) | |
| `resource.k8s.io/ResourceSlice` (DRA) | best-effort fallback | yes (`dra_resourceslice_controller.go`, `dra_resourceslice.go`) | **PARTIAL** (operator authoritative when CRD installed) | |

---

## Synthetic kinds (operator-only)

| Kind | agentic.emits | operator.emits | Status | Notes |
|---|---|---|---|---|
| `Cluster` (synthetic anchor) | no | yes (`cluster.go`, anchor publisher in `cluster_anchor.go`, issue #159) | OPERATOR-ONLY | The operator emits a `Cluster` anchor that ties every other entity to a cluster identity — there is no agentic equivalent. |

---

## Summary

**Total kinds catalogued**: 36

| Status | Count | Kinds |
|---|---|---|
| **COVERED** | 16 | ConfigMap, Endpoints, LimitRange, Namespace, Node, PersistentVolume, Pod, Service, Deployment, DaemonSet, ReplicaSet, StatefulSet, Job, Ingress, NetworkPolicy, ResourceQuota |
| **PARTIAL** | 7 | Argo Rollouts (Rollout), ArgoCD (Application, ApplicationSet), DRA (DeviceClass, ResourceClaim, ResourceClaimTemplate, ResourceSlice) — all gated on customer cluster CRD installation |
| **NOT-COVERED** | 9 | PersistentVolumeClaim, ReplicationController (low priority), ServiceAccount, CronJob, Role, RoleBinding, ClusterRole, ClusterRoleBinding, HorizontalPodAutoscaler |
| **OPERATOR-ONLY** | 3 | Secret (redacted), StorageClass, Cluster (synthetic) |
| **NEITHER** | 1 | Event |

Total: 16 + 7 + 9 + 3 + 1 = **36 rows**.

### Release-blocker summary for v0.4 default-disable

The following 8 kinds are **NOT-COVERED** by the operator and emit
from the agentic connector's `_DEFAULT_INCLUDE_KINDS`. Each must be
addressed before the v0.4 release flips
`OMNISCIENCE_K8S_AGENTIC_ALLOWED` to `false` by default:

1. ~~`LimitRange`~~ — **DONE** in issue #202 (namespace resource caps).
2. `PersistentVolumeClaim` — persistent storage requests
3. ~~`ResourceQuota`~~ — **DONE** in issue #204 (namespace quota enforcement).
4. `ServiceAccount` — workload identity
5. `CronJob` — scheduled batch workloads
6. `Role` — namespaced RBAC
7. `RoleBinding` — namespaced RBAC binding
8. `ClusterRole` — cluster-scoped RBAC
9. `ClusterRoleBinding` — cluster-scoped RBAC binding
10. `HorizontalPodAutoscaler` — workload autoscaling

`ReplicationController` is also NOT-COVERED but acceptable as a v0.5
gap pending customer-telemetry confirmation that no production cluster
relies on it.

**Each item above must become an issue under epic #98** (or a new
gap-closure epic for v0.4) and ship before the v0.4 default-flip.
The deprecation announcement landed by issue #168 is **independent**
of these gaps — the announcement, ADR-0011, and the migration guide
all stand even with the gaps documented; what they *gate* is the v0.4
release-time flip of the server-side feature flag.

---

## Verification methodology

This matrix was built by:

1. Enumerating every kind in the agentic connector's
   `_KIND_*` REST-path tables (file:
   `packages/connectors/src/omniscience_connectors/agentic/k8s.py`).
2. Subtracting `_ALWAYS_EXCLUDE` (the never-emit set).
3. Cross-referencing against the operator's entity mappers
   (`operator/internal/entity/*.go`, count: 24 mapper functions of
   shape `XxxToEvent`).
4. Cross-referencing against the operator's wired controllers
   (`operator/cmd/manager/main.go`, count: 21
   `controller.NewXxxReconciler` call sites plus discovery-gated
   `SetupArgoCDWatchers` and `SetupDRAWatchers`).
5. Checking the discovery-gated CRD coverage in
   `operator/internal/controller/argocd_setup.go` and
   `operator/internal/controller/dra_setup.go`.

Re-run this audit before the v0.4 cut to confirm the gap list is
unchanged or has been closed.
