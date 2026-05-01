# omniscience-operator Helm chart

Kubernetes operator for Omniscience — watches resources and publishes change
events to NATS JetStream. See [ADR-0007](../../docs/decisions/0007-k8s-operator-architecture.md).

This document describes the **enterprise hardening posture** introduced by
issue [#166](https://github.com/100rd/Omniscience/issues/166): Pod Security
Standards `restricted`, NetworkPolicy default-deny + named allow-list,
audited least-privilege RBAC, image signing via cosign keyless OIDC, defined
resource envelopes, and topology spread for HA.

---

## Quick install

```bash
helm install omniscience-operator helm/omniscience-operator \
  --namespace omniscience-system --create-namespace \
  --set workspaceId=<UUID> \
  --set clusterName=<name> \
  --set nats.url=nats://omniscience-nats.system:4222 \
  --set omniscience.serverUrl=https://api.omniscience.example.com:443
```

The three values that are **required** and have **no default**:

| Value | Why |
|---|---|
| `workspaceId` | UUID stamped on every emitted event — the tenant boundary (ADR-0007 §ACL). |
| `clusterName` | Human-readable cluster identifier; appears in entity metadata. |
| `nats.url`    | NATS JetStream endpoint the operator publishes to. |

`omniscience.serverUrl` is recommended in production; without it the
NetworkPolicy emits no egress rule for the Omniscience read API and the
reconciliation worker (#163) cannot reach the server.

---

## Hardening posture

### Pod Security Standards (PSA `restricted`)

- `runAsNonRoot: true`, `runAsUser: 65532` (the distroless `nonroot` UID)
- `readOnlyRootFilesystem: true` — the operator runs from `/manager` on a
  distroless static image and never writes to the rootfs.
- `allowPrivilegeEscalation: false`
- `capabilities.drop: ["ALL"]`
- `seccompProfile.type: RuntimeDefault`
- No `hostNetwork`, `hostPID`, `hostIPC`, or `hostPort` anywhere.

The chart **enforces a floor**: even if a user passes
`--set securityContext.allowPrivilegeEscalation=true`, the manifest re-stamps
the load-bearing fields so the floor cannot be relaxed. Customers MAY add
non-conflicting fields (e.g. additional `appArmorProfile`) via values.

To label the operator's namespace with PSA `restricted` enforcement:

```yaml
podSecurity:
  enforce: true
  manageNamespace: true   # default false; set true only if Helm owns the ns
  level: restricted
```

The default `manageNamespace: false` matches the typical enterprise workflow
where the platform team pre-creates the namespace. Apply the labels
out-of-band:

```bash
kubectl label namespace omniscience-system \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted
```

### NetworkPolicy default-deny

Fourth layer of operator defence-in-depth (ADR-0007 §ACL). Default `enabled: true`.

**Allow-list**:

| Direction | Port      | Peer                                                | Purpose            |
|-----------|-----------|-----------------------------------------------------|--------------------|
| Ingress   | 8080/TCP  | `networkPolicy.metricsAllowedNamespaceSelector`     | Prometheus scrape  |
| Ingress   | 8081/TCP  | `networkPolicy.probePortAllowedFrom` (default open) | Kubelet probes     |
| Egress    | 53/UDP+TCP| `networkPolicy.dnsNamespaceSelector` (kube-system)  | DNS                |
| Egress    | 443/TCP   | `networkPolicy.apiServerCIDR` (default port-only)   | kube-apiserver     |
| Egress    | nats port | (port-only)                                         | NATS JetStream     |
| Egress    | omni port | (port-only)                                         | Omniscience read API |

The NATS and Omniscience egress rules render port-only by default. Customers
running CNIs with FQDN/CIDR policy support (Cilium, Calico) can layer a
hostname/CIDR pin via a values overlay. The port restriction is the
load-bearing constraint.

**CNI assumptions**. Standard NetworkPolicy v1 semantics. Calico, Cilium,
kube-router, and AWS VPC CNI (with policy enforcement enabled) honour both
ingress AND egress rules. **Older Flannel without `flannel-policy-controller`
and Weave-Net without policy controller silently drop egress rules.** If
your CNI is one of those, this NetworkPolicy reduces to ingress-only
enforcement — adopt a CNI that honours egress, or set
`networkPolicy.enabled: false` and apply policy at a different layer.

To verify the policy at install time, deliberately point `nats.url` at a
foreign host and observe the operator's connection-refused at the policy
level:

```bash
helm upgrade omniscience-operator helm/omniscience-operator \
  --set nats.url=nats://attacker.example.com:9999 ...

# Watch operator logs — should see EGRESS DENIED, not a connection timeout
# to attacker.example.com (which would mean the policy did not engage).
```

### RBAC verb audit (read-only)

The operator's `ClusterRole` carries **only** `get`, `list`, `watch` verbs
across every resource. There is no `create`, `update`, `patch`, `delete`,
`deletecollection`, or `*` anywhere. CI enforces this via
`scripts/lint-helm-rbac-verbs.sh` — the script fails non-zero if a write
verb leaks in. The script also asserts:

- ClusterRole has no `aggregationRule`
- The leader-election `Role` (namespaced) is the only object permitted to
  carry write verbs, and only on `coordination.k8s.io/leases`

### Image signing via cosign keyless OIDC

CI signs each published image with `cosign` keyless mode (Sigstore Fulcio).
Customers verify with:

```bash
cosign verify \
  --certificate-identity-regexp 'https://github.com/100rd/Omniscience/.github/workflows/operator.yml@refs/(heads|tags)/.+' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/100rd/omniscience-operator@sha256:<digest>
```

A successful verification looks like:

```
Verification for ghcr.io/100rd/omniscience-operator@sha256:... --
The following checks were performed on each of these signatures:
  - The cosign claims were validated
  - Existence of the claims in the transparency log was verified offline
  - The code-signing certificate was verified using trusted certificate authority certificates
```

For maximum reproducibility, **pin by digest** in production:

```yaml
image:
  digest: sha256:<digest>
```

When `image.digest` is set, the chart renders `repository@digest` and ignores
`image.tag`. CI emits a digest-pinned values overlay alongside each release.

### Resource envelope

Empirical envelope from issues #157–#162 (single-digit MB idle, ~100 MiB
peak under heavy churn at ~10k watched objects). Defaults:

```yaml
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 512Mi
```

Tune for your cluster size: a 50k-pod cluster may want 256Mi / 1Gi.

### Topology spread (HA)

Default `replicaCount: 1`. Multi-replica posture requires
`leaderElection.enabled: true` (already supported per #101 — ONE leader
emits, others stand by).

When `replicaCount >= 2`, the chart renders a `topologySpreadConstraint`
across `topology.kubernetes.io/zone` with `whenUnsatisfiable: ScheduleAnyway`.
Single-zone clusters fall through; multi-zone deployments do not co-locate.

---

## Quality gates (CI)

| Gate | Tool | Status |
|------|------|--------|
| `helm lint` | helm 3.15.4 | clean |
| Schema validation | `kubeconform -strict` | clean |
| Best-practice audit | `polaris audit --format=json` (production profile) | zero violations |
| Score baseline | `kube-score score --config helm/omniscience-operator/.kube-score.yaml` | matches pinned baseline |
| RBAC verb allow-list | `scripts/lint-helm-rbac-verbs.sh` | exit 0 |
| Chart hardening assertions | `pytest helm/omniscience-operator/tests/assertions/` | 19 passed |
| Kyverno baseline | `kyverno apply tests/policies/ --resource <(helm template ...)` | all pass |
| Image signing | `cosign sign` keyless OIDC in CI; verify in README | signed |

---

## Customer-side override patterns

```yaml
# Pin Prometheus scrape to your monitoring namespace's actual label.
networkPolicy:
  metricsAllowedNamespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: kube-prometheus-stack

# Pin kube-apiserver egress to a CIDR (managed cluster).
networkPolicy:
  apiServerCIDR: "10.0.0.0/16"

# Tighten probe-port ingress to the cluster CIDR (kubelet sources).
networkPolicy:
  probePortAllowedFrom:
    - ipBlock:
        cidr: 10.0.0.0/8

# Production digest pin.
image:
  digest: sha256:abc123...
```

---

## Threat model & defence-in-depth

The operator's `ServiceAccount` has cluster-wide read access. Per ADR-0007 §ACL,
its blast radius is bounded by **four layers**:

1. **Workspace stamping at the operator** — `workspace_id` from a Secret-mounted
   file, never derived from cluster-side state.
2. **NATS subject scoping** — `ingest.changes.k8s.{workspace_id}`; misconfigured
   consumers cannot see other tenants' traffic.
3. **Server-side adapter rejection** — events without `workspace_id` are dropped.
4. **NetworkPolicy** (this chart) — egress is bounded; a compromised operator
   cannot exfiltrate to arbitrary endpoints.

The Secret carrying `OMNISCIENCE_WORKSPACE_ID` is mounted via `envFrom` (not
`valueFrom` on a ConfigMap that could leak via `kubectl describe pod`).
