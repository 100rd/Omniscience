# Migrating from `k8s-agentic` to `omniscience-operator`

> **Status**: deprecation announced in **v0.3**.  Default-disabled in **v0.4**.
> Connector code removed in **v0.5**.  See
> [ADR-0011](../decisions/0011-k8s-agentic-deprecation-schedule.md) for the
> schedule and [issue #168](https://github.com/100rd/Omniscience/issues/168)
> for the tracking issue.

This guide walks operators from the legacy `k8s-agentic` connector
(`packages/connectors/src/omniscience_connectors/agentic/k8s.py`) to the
in-cluster `omniscience-operator` Go controller at `operator/`.  The
operator is the path forward for every Kubernetes-shaped signal in
Omniscience — see [ADR-0007](../decisions/0007-k8s-operator-architecture.md)
for the architectural rationale.

**Read this if you have:** a deployed Omniscience server with a
`k8s-agentic` connector configured, and you want to switch to the
operator before the v0.5 removal.

**Reference parity matrix:** every kind the agentic connector emits is
catalogued against the operator's coverage at
[`k8s-agentic-vs-operator-parity.md`](k8s-agentic-vs-operator-parity.md).
**Read it before starting** — there are kinds that the agentic connector
emits which the operator does *not* yet cover; if your deployment relies
on them, the migration is gated on the gap-closure issues listed there.

---

## TL;DR

1. Install the operator chart in your cluster
   (`helm install omniscience-operator ./helm/omniscience-operator …`).
2. Set the operator's `workspaceId` and `clusterName` to match the
   workspace your existing `k8s-agentic` connector publishes into.
3. Run both paths in parallel for **1–2 weeks**.  The server-side dedup
   gate ([ADR-0010](../decisions/0010-server-side-emitter-dedup.md))
   silently drops the agentic events for any kind the operator has
   started emitting.
4. Watch the
   `omniscience_ingestion_dedup_drop_total{authority_emitter="k8s-operator"}`
   counter rise.  When the drop rate plateaus (i.e. the operator is
   emitting for every kind the agentic connector was previously
   emitting), disable the agentic connector entirely.
5. Keep the agentic connector available as a **rollback path** for the
   first 24 hours after disabling it.  See [Rollback](#rollback).

---

## Why migrate

The agentic connector was the bridge between v0.1's REST-poll model and
v0.2's streaming-ingest model.  It served well as a bootstrap.  It is
not the right substrate for the Living Semantic Core direction:

- **Freshness budget.**  Agentic polls on a configurable interval; the
  operator watches the API server and emits events with sub-second
  latency.  The vision §6 freshness SLO is unreachable from the
  poll path.
- **Causal-edge fidelity.**  The agentic path observes steady-state
  snapshots and infers transitions; the operator sees every
  `ADDED`/`MODIFIED`/`DELETED` directly, including short-lived ones
  that matter during incident timelines.
- **Network and credential surface.**  The agentic path needs a
  kubeconfig with cluster-wide read on the Omniscience server side; the
  operator runs in-cluster with a narrowly-scoped `ServiceAccount` and
  emits outbound to NATS only.
- **Multi-cluster reach.**  The operator establishes a stable
  per-cluster `cluster_id` (issue #167) so the same workload across
  clusters does not collide on `external_id`; the agentic path's
  `cluster_name` is a free-form string and offers no equivalent guarantee.
- **Workspace tenancy.**  The operator reads `workspace_id` from a
  Secret mounted at chart install time and stamps it on every event;
  no cluster-side state can forge it.  The agentic path resolves the
  workspace from `Source.tenant_id` server-side — correct, but one
  server-side hop further from the emit point.

---

## Side-by-side comparison

| Dimension | `k8s-agentic` (legacy) | `omniscience-operator` (replacement) |
|---|---|---|
| **Deployment shape** | Runs on the Omniscience server; one Source row per cluster, one kubeconfig per Source. | Runs **inside** the customer cluster as a `Deployment` in its own namespace. |
| **Control-plane reach** | Outbound from Omniscience server to each cluster's API server (typically the public endpoint). | Inbound from operator to NATS only. No path *into* the cluster from outside. |
| **Authentication** | Bearer token or kubeconfig stored as a Source secret in Omniscience. | In-cluster `ServiceAccount`; token rotation handled by Kubernetes. |
| **RBAC** | Cluster-wide read on whatever the kubeconfig grants (typically `cluster-admin`-equivalent for discovery). | `ClusterRole` listing the exact kinds the operator watches; no wildcards. |
| **Network surface** | One outbound TLS connection from Omniscience server to each cluster's API server (port 6443). Cluster must accept the connection. | One outbound NATS connection from each operator to the Omniscience NATS endpoint (port 4222 default; mTLS-wrappable). No inbound to the cluster. |
| **Event freshness** | Poll-based; freshness floor = poll interval (default hourly).  Bursts of churn produce list races and missed transitions. | Watch-based; sub-second freshness in steady state. Reconciliation worker (issue #163) closes the gap on operator restarts on a 15-minute cycle. |
| **Schema fidelity** | Stores raw `application/json` payloads from the API server; downstream pipeline must re-parse. | Per-kind structured entity mappers (`operator/internal/entity/*.go`) emit pre-shaped graph entities with named edges. |
| **CRD support** | Only kinds the LLM picks plus a hard-coded core/apps set; no CRD-aware enumeration. | First-class CRD support for ArgoCD, Argo Rollouts, and DRA, gated by API-server discovery. |
| **Multi-cluster identity** | `cluster_name` free-form; collisions across clusters silently merge entities. | `cluster_id` UUID Secret-mounted (issue #167); collisions impossible across clusters. |
| **Server-side authority** | None; both paths emit independently into the graph store. | Operator-wins dedup (ADR-0010) — the operator is authoritative for kinds it covers. |
| **Resource cost** | One Omniscience-server-side worker per cluster; CPU/memory scales with cluster size on the server. | One in-cluster pod per cluster; idle ~10 MB RAM, single-digit-MB image (distroless Go). |
| **Restart durability** | Loses any events that fired during an Omniscience-server-side restart of the connector worker. | Watch reconnect on operator restart; reconciliation worker (issue #163) closes the gap. |

---

## Step-by-step migration

### Step 0 — Read the parity matrix

Open
[`k8s-agentic-vs-operator-parity.md`](k8s-agentic-vs-operator-parity.md)
and confirm the operator covers every kind your existing pipelines
consume.  If a kind shows **NOT-COVERED** or **PARTIAL** and you depend
on it, **stop here** and follow that row's gap-closure issue.

### Step 1 — Install the operator chart

Per-cluster prerequisites:

- Kubernetes 1.27+ (informer reflector dependencies).
- A `Secret`-grade source for the workspace UUID (the same UUID your
  existing `k8s-agentic` Source publishes into; query it from your
  Omniscience admin if you don't have it handy).
- Outbound network egress to the Omniscience NATS endpoint
  (port 4222 default; configurable via `nats.url`).

```bash
# In the cluster being observed:
kubectl create namespace omniscience-operator

# Workspace UUID is the load-bearing tenancy identifier; fetch it from
# your Omniscience admin and pass it in via --set or a values file.
helm install omniscience-operator ./helm/omniscience-operator \
  --namespace omniscience-operator \
  --set workspaceId="00000000-0000-0000-0000-000000000000" \
  --set clusterName="prod-eu-west-1" \
  --set nats.url="nats://omniscience-nats.example.com:4222" \
  --set nats.credentials="$(cat ./nats.creds)"
```

The chart's `values.yaml` lists every override; the **required**
values are `workspaceId`, `clusterName`, and `nats.url`.

### Step 2 — Verify the operator is publishing

In the operator pod:

```bash
kubectl -n omniscience-operator logs -f deploy/omniscience-operator | grep "publish.success"
```

On the Omniscience server side:

```bash
# Operator emit rate, by kind
curl -s http://omniscience-server/metrics | grep '^omniscience_operator_events_emitted_total'
```

If the operator emit rate is non-zero, the path is alive.

### Step 3 — Watch the dedup metric

The server-side dedup gate (issue #164, ADR-0010) is the during-cutover
health signal.  As the operator starts emitting for kinds the agentic
connector was previously emitting, the gate transitions authority to
the operator and silently drops the agentic events:

```promql
# Headline: agentic events being dropped because operator now authoritative.
sum by (kind) (
  rate(omniscience_ingestion_dedup_drop_total{
    dropped_emitter="k8s-agentic",
    authority_emitter="k8s-operator"
  }[5m])
)
```

```promql
# Authority transitions (per-kind cutover progress).
sum by (kind) (
  increase(omniscience_ingestion_dedup_authority_transitions_total{
    from_emitter="k8s-agentic",
    to_emitter="k8s-operator"
  }[1h])
)
```

A **non-zero drop rate is expected and healthy** — it is the visible
signal that the operator has taken authority.  A **zero drop rate
during the parallel window** is a misconfiguration symptom; check that
both paths are actually emitting and that `OMNISCIENCE_DEDUP_ENABLED`
is not unset to `false`.

### Step 4 — Run both paths in parallel for 1–2 weeks

This is the recommended bake-in window.  During this window:

- Both paths emit; the dedup gate ensures exactly one wins per
  `(workspace_id, external_id)`.
- The operator is authoritative for the kinds it covers
  (per the parity matrix).
- The agentic connector remains authoritative for any kind the
  operator does not cover (see the parity matrix's NOT-COVERED rows).

Watch for:

- Operator emit-rate match against the agentic baseline.  The two are
  not strictly equal — the operator emits per-event whereas the agentic
  path emits per-poll — but the *unique-entity* counts should converge.
- Reconcile loop health
  (`omniscience_operator_reconciler_runs_total{result="success"}`)
  rising steadily.
- Zero unexpected error spikes on
  `omniscience_operator_publisher_errors_total`.

### Step 5 — Disable the agentic connector

When you are confident the operator is covering every kind your
pipelines need:

1. Update the agentic Source in your Omniscience admin:
   - **Set `enabled: false`** on the Source row, OR
   - **Remove the Source entirely** if you don't intend to keep it as a
     rollback option.

2. Confirm the dedup-drop metric trails off to zero — once the agentic
   connector stops emitting, there are no events for the gate to drop.

3. **Do not** uninstall the agentic connector module (the module is
   still shipped in v0.3 — its removal is scheduled for v0.5).

### Step 6 — (v0.4 only) Set the server-side feature flag

In v0.4 the server gains the `OMNISCIENCE_K8S_AGENTIC_ALLOWED` env var.
Default is `false` in v0.4; setting it to `true` re-enables agentic
ingestion as the rollback path.  In v0.3 this flag does not yet exist
— use the Source-level `enabled` flag instead.

---

## Troubleshooting

### NATS connectivity

```text
operator startup logs:
  publisher.connect.error: dial tcp: i/o timeout
```

- Confirm `nats.url` resolves from inside the cluster
  (`kubectl run debug --rm -it --image=alpine -- nslookup
  omniscience-nats.example.com`).
- Confirm outbound port (4222 default) is allowed by your
  `NetworkPolicy` and any cluster-egress firewall.
- If using mTLS, confirm `nats.credentials` is a non-empty string and
  the corresponding NATS user has `publish` permission on
  `ingest.changes.k8s.>`.
- The chart enables a `NetworkPolicy` by default; if your cluster's
  CNI is missing or your egress rules are stricter, set
  `networkPolicy.enabled=false` and rely on cluster-level egress
  controls.

### RBAC denied

```text
operator startup logs:
  reflector.go: Failed to watch *v1.Pod: forbidden
```

- The chart provisions a `ClusterRole` and `ClusterRoleBinding`
  scoped to the kinds the operator watches.  If you set `rbac.create=false`
  to use a pre-provisioned RBAC, confirm the role grants
  `get`, `list`, `watch` on every kind in the
  [parity matrix](k8s-agentic-vs-operator-parity.md) row marked
  `operator.emits=yes`.
- For ArgoCD/Argo Rollouts/DRA CRDs, the operator's discovery probe
  fails-soft: it logs `argocd.crd.absent` (or equivalent) and
  continues; you do not need to grant permissions on CRDs that aren't
  installed.

### Pod Security Admission (PSA) violations

```text
admission webhook denied the pod:
  ... violates PodSecurity "restricted:v1.27" ...
```

The operator chart's `podSecurityContext` and `securityContext`
defaults satisfy the `restricted` PSA profile (issue #166).  If your
namespace's PSA label is `restricted` and the chart still fails:

- Confirm `image.repository` is the published `ghcr.io/100rd/omniscience-operator`
  image.  Custom images that don't run as non-root will fail.
- Confirm `seccompProfile.type=RuntimeDefault` is not overridden in
  your values.

### NetworkPolicy egress blocking

If you've enabled the chart's `NetworkPolicy` and the operator's
publisher times out, check the egress rule allows traffic to the
NATS endpoint.  The chart's default policy whitelists port 4222 to the
NATS service; for a non-default port or external NATS, override
`networkPolicy.egress`.

### Kubernetes API version skew

The operator targets Kubernetes 1.27+.  On older clusters:

- `informers/v1beta1` types are unavailable; the operator fails-fast
  at startup with `unable to construct informer: no kind ... in version`.
- DRA CRDs (`resource.k8s.io/v1beta1`) are 1.30+; the operator's
  discovery gate logs `dra.crd.absent` and skips them on older
  clusters.

If you're stuck on a pre-1.27 cluster, the agentic connector remains
the supported path — the deprecation timeline assumes you will upgrade
*or* migrate before v0.5.

### Operator pod crashlooping on `workspace_id`

```text
operator startup logs:
  config: workspaceId is required and must be a valid UUID
```

The `workspaceId` Helm value is required and must parse as a UUID.
A blank string or a non-UUID value triggers fail-fast
(per [ADR-0007 §ACL](../decisions/0007-k8s-operator-architecture.md#5-acl-invariant--workspace-from-mounted-token-never-from-cluster)
fail-closed posture).

### Dedup gate not dropping agentic events

```promql
# Should be non-zero during the parallel window:
rate(omniscience_ingestion_dedup_drop_total[5m])
```

If this is zero while both paths are running:

- Confirm `OMNISCIENCE_DEDUP_ENABLED` is not set to `false` on the
  Omniscience server.
- Confirm `OMNISCIENCE_INGEST_DEDUP_TTL_HOURS` is not set to `-1`
  (which is the disable sentinel).
- Confirm the operator and the agentic connector are publishing into
  the **same** `workspace_id`.  The dedup table is keyed on
  `(workspace_id, external_id)`; cross-workspace events are not
  deduped (and should not be — that would be a tenant-isolation
  violation).
- Confirm `external_id` shapes match.  The operator's
  `external_id = "k8s_resource/<cluster_id>/<Kind>/<namespace>/<name>"`;
  the agentic connector's
  `external_id = "k8s:<cluster_name>:kind:<Kind>"` (a coarser shape).
  **They will not collide on `external_id` alone** — the dedup table's
  authority pointers are established per `external_id`, so the two
  paths' events for the same Kubernetes object end up in different
  rows.  This is documented as a known v0.3 gap; the v0.4 release line
  pins the `OMNISCIENCE_K8S_AGENTIC_ALLOWED=false` server-side flag as
  the primary cutover mechanism, not dedup-only deduplication.

---

## Rollback

The dedup TTL is the rollback budget.  The default
`OMNISCIENCE_INGEST_DEDUP_TTL_HOURS=24` means: if the operator stops
emitting for **more than 24 hours**, the agentic connector's events
re-take authority for any `(workspace_id, external_id)` pair the
operator was authoritative on.

**Within the TTL window** (the first 24 h after the operator stops
emitting):

- Re-enable the agentic Source in your Omniscience admin
  (`enabled: true`).
- The agentic connector resumes polling on its configured cadence.
- The dedup gate continues to drop agentic events for kinds where the
  operator's `last_emit_at` is still within the TTL — but the operator
  isn't emitting anymore, so on the next agentic poll **after** the TTL
  expires, authority flips back to agentic.  This is the
  `ttl_reassign_to_agentic` action in the dedup metrics.

**After the TTL window**, the agentic path is the authority again;
re-installing the operator at this point is a fresh cutover, not a
rollback.

**To rollback faster than the TTL allows** (operational urgency,
e.g. operator emit produces incorrect entities):

1. Set `OMNISCIENCE_DEDUP_ENABLED=false` on the Omniscience server
   and restart the worker.  Both paths now emit in parallel into the
   graph store.  This is the v0.2 fallback posture.
2. Disable the operator chart (`helm uninstall omniscience-operator`).
3. Investigate and fix.
4. Re-enable dedup once the issue is resolved.  The dedup table's
   authority pointers persist; you may want to manually clear stale
   pointers for affected `(workspace_id, external_id)` pairs.

**Do not** mix this rollback with a workspace_id rotation.  The
`workspace_id` is the tenancy boundary; rotating it during a rollback
would mean the previously-emitted entities are unreachable from the
new `workspace_id`'s read scope.

---

## Future versions

| Version | What changes | Customer action |
|---|---|---|
| **v0.3** (current) | Deprecation announced.  Both paths run in parallel.  `DeprecationWarning` on import. | Optional: start migrating now. |
| **v0.4** | Agentic moves to opt-in extra (`omniscience-connectors[k8s-agentic]`).  `OMNISCIENCE_K8S_AGENTIC_ALLOWED=false` default — server drops agentic events server-side. | **Required**: complete migration before upgrade. |
| **v0.5** | Agentic module deleted.  Helm chart references removed.  Importing `omniscience_connectors.agentic.k8s` raises `ImportError`. | **Required**: be on the operator path. |

---

## References

- [Issue #168](https://github.com/100rd/Omniscience/issues/168) — this deprecation plan
- [ADR-0007](../decisions/0007-k8s-operator-architecture.md) — operator architecture (the destination)
- [ADR-0007 §6](../decisions/0007-k8s-operator-architecture.md#6-cross-document-consequences) — original commitment to set deprecation date once parity reached
- [ADR-0010](../decisions/0010-server-side-emitter-dedup.md) — server-side dedup (the during-cutover gate)
- [ADR-0011](../decisions/0011-k8s-agentic-deprecation-schedule.md) — this deprecation schedule
- [Parity matrix](k8s-agentic-vs-operator-parity.md) — kind-by-kind coverage status
- [Operator chart README](../../helm/omniscience-operator/README.md) — installation reference
