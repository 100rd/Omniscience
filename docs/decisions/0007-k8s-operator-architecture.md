# ADR 0007 — Kubernetes operator architecture

- **Status**: Proposed
- **Date**: 2026-04-24
- **Tracks epic**: [#98](https://github.com/100rd/Omniscience/issues/98) — K8s operator GA
- **This issue**: [#101](https://github.com/100rd/Omniscience/issues/101) — operator kickoff (ADR + scaffold)
- **Replaces (eventually)**: the agentic K8s connector at `packages/connectors/src/omniscience_connectors/agentic/k8s.py`. The two paths run in parallel through the operator's GA.

## Context

The K8s connector that ships in v0.2.0 is an [agentic discovery + REST poll](../../packages/connectors/src/omniscience_connectors/agentic/k8s.py) loop. An LLM is asked which `kind`s to index, the connector enumerates `/api` and `/apis` and pulls full lists periodically. That shape was correct for the v0.1 retrieval baseline — pulling once an hour against a small set of clusters — but it does not survive contact with the Living Semantic Core direction in `docs/vision.md`.

Three forces make a poll-based connector the wrong substrate going forward:

1. **Freshness budget.** Vision §5.3 (temporal graph) and §6 (freshness SLO) make staleness a first-class quality dimension. A list-everything poll has a freshness floor equal to the polling interval; for a cluster of any size, hourly polling is the only realistic envelope, which is two-to-three orders of magnitude worse than the SLO target. A **watch**-based pathway can deliver per-resource events with sub-second latency at near-zero steady-state cost, because the K8s API server already maintains the watch state.

2. **Causal edge fidelity.** §5.1 (hybrid knowledge graph) requires causal/ownership edges between Kubernetes resources (`Deployment` owns `ReplicaSet` owns `Pod`, `Service` selects `Pod`s). With list-polling we observe steady-state snapshots and have to infer "what changed since last list" — the diff is necessarily lossy across `Pod` recreates because list polling against a churning workload misses transitions. A watch sees every `ADDED` / `MODIFIED` / `DELETED`, including the short-lived ones that are causally important during incident timelines.

3. **Per-cluster reach.** §5.5 names `resolve_incident` as a flagship MCP tool. A useful incident-resolution graph requires **co-located** event capture in the customer cluster, not a remote agent reaching in over kubeconfig with broad credentials. The connector's deployment shape (Omniscience server with kubeconfig per cluster) does not scale to multi-tenant SaaS or on-prem self-hosted with multiple clusters per tenant. An **operator running in the cluster being observed** with a narrowly-scoped service account is the right reach pattern; the operator emits events outward to Omniscience.

The ACL invariant from [#117](https://github.com/100rd/Omniscience/issues/117) and the storage cutover in [#127](https://github.com/100rd/Omniscience/issues/127) (post-ADR-0005 / ADR-0006) demand that **every entity carry a non-null `workspace_id`**. The operator must establish the workspace at the point of emission — there is no later layer that can correctly back-fill it without trusting cluster-side labels, which is a tenant-isolation foot-gun (see ACL section below).

This ADR scopes the architecture for the operator track. Issue #101 lands the ADR plus a minimal scaffold (single `Pod` watcher, Helm sub-chart skeleton, Makefile, unit test). Subsequent issues in epic #98 expand the watcher coverage, add reconciliation, and run the parallel-deprecation period for the agentic connector.

## Decision

We build **`omniscience-operator`** — a Kubernetes operator written in **Go** using **`controller-runtime` + `kubebuilder`**, packaged as a **Helm sub-chart** under `helm/omniscience/charts/operator/`, **co-located in the Omniscience monorepo** at `operator/`. It runs **inside** the customer cluster on a least-privilege `ServiceAccount`, **watches** Kubernetes resources via the API server's watch endpoint, and **publishes** entity events to Omniscience over **NATS JetStream** on the existing `INGEST_CHANGES` stream (ADR-0006, [#127](https://github.com/100rd/Omniscience/issues/127)). The **`workspace_id`** is read from a Kubernetes `Secret` mounted into the operator pod and is **never** derived from cluster-side state.

Concretely:

| Dimension | Decision |
|---|---|
| **Repository** | In-tree subdirectory `operator/` of `100rd/Omniscience` |
| **Language** | Go 1.22+ |
| **Framework** | `sigs.k8s.io/controller-runtime` + `kubebuilder` for scaffolding |
| **API model** | Pure API-client watcher (no CRDs) |
| **Ingestion pathway** | Direct **NATS JetStream** publish to `ingest.changes.k8s` |
| **Workspace establishment** | Token-mounted via `Secret`; operator never reads workspace from cluster state |
| **Helm packaging** | Sub-chart under the umbrella chart, off-by-default in v0.2 |
| **Reach pattern** | Operator runs in the observed cluster; Omniscience server is the consumer |

### 1. Repository — in-tree, not a separate repo

Keeping the operator under `operator/` in the same repository as the Omniscience server keeps the **event schema** (currently `omniscience_server.ingestion.events.DocumentChangeEvent`) under one CI lock. A separate repo would force schema versioning across two release cadences and would make breaking-change reviews cross-org rather than cross-package. The cost — Go and Python toolchains in one tree — is small: the existing `pyproject.toml` is unaffected, and CI gains a Go job that runs only when files under `operator/` change.

We retain the option to spin out into `omniscience-operator` later. The cost of the split, if we take it, is a `go.mod` move and a CI-job extraction; the cost of the merge — if we started separate and converged — would be reconciling divergent schema definitions and release tags, which is materially harder.

### 2. Language and framework — Go with controller-runtime

The choice is between **Go (`controller-runtime` + `kubebuilder`)** and **Python (`kopf`)**. We pick Go.

**Why Go wins for this workload:**
- **Ecosystem fit.** `controller-runtime` is the substrate the operator-SDK, `cluster-api`, the cert-manager, and effectively every credible Kubernetes operator runs on. Issues, examples, debugging help, and reviewer expertise are all targeted at that stack. `kopf` is a fine framework but its install base in production operators is small enough that hiring against it and finding upstream fixes both cost more.
- **Watch primitives.** The Go client-go watch and informer machinery is the canonical implementation; every other language client wraps it or reimplements parts of it. `controller-runtime` builds resync, work-queue, and leader-election on top of the same primitives the K8s control plane uses.
- **Image size and footprint.** A statically-linked Go binary on `gcr.io/distroless/static` lands at ~30 MB. The `kopf` Python image with all transitive deps is ~150 MB minimum on `python:3.12-slim`. For a per-cluster sidecar shipped to customers, this matters.
- **CRD evolution path.** When (not if) we add CRDs in a follow-up issue, `kubebuilder` already wires the entire conversion-webhook + generator path. Bolting CRDs onto `kopf` is doable but lossier.
- **Memory under steady state.** A watch-driven controller idles at single-digit MB in Go. The Python equivalent under `kopf` runs ~10x higher because the GIL-driven asyncio loop holds more state per object. At 10k+ objects per cluster the difference becomes visible.

**Cost we accept:**
- A Go toolchain in a Python-primary monorepo. The mitigation is strict isolation: the operator has its own `go.mod`, its own `Makefile` targets, its own CI job. Python developers do not need to install Go to work on the rest of the codebase.
- A second language for which we currently have no internal Cypher-cheatsheet equivalent. Mitigation: this ADR plus the operator README plus a follow-up `docs/contributing/operator.md` written when the second contributor joins the operator track.

We considered keeping it in Python because the rest of the codebase is Python and the existing K8s connector already speaks REST against the API server. We rejected that on the operator-specific grounds above. The K8s **connector** is a poller; a poller in Python is reasonable. The K8s **operator** is a watch-driven controller; a watch-driven controller in Python is fighting the language.

### 3. API model — pure watcher, no CRDs (in v0.2)

We do not introduce custom resources in this issue. The operator is a **read-only consumer** of standard Kubernetes resources (initially `Pod`; coverage expands in follow-up issues per epic #98). Each resource event maps to one Omniscience entity emitted on the NATS bus.

**Why no CRDs yet:**
- The operator's job in v0.2 is *observation*, not configuration. CRDs make sense when you want users to declare desired state in YAML; the user-facing configuration here is "deploy the chart and set `workspace_id`". A CRD would add a control plane the user doesn't need.
- CRDs introduce a versioning surface that must be supported across operator upgrades. We are not ready to commit to a stable user-visible API in v0.2; the Helm `values.yaml` is the public surface and is easier to evolve.
- Optional configuration (e.g. namespace allow-lists, label-selector overrides) lives in Helm values and is read into the manager at startup. The configuration footprint is small enough that a CRD would be over-fit.

We **revisit CRDs** when we have a real need — most likely either (a) per-cluster fine-grained ingest scope that needs to be GitOps-managed alongside the workloads it observes, or (b) Action Mode (vision §6) where the operator writes back. Both are post-GA and outside the epic #98 horizon.

### 4. Ingestion pathway — NATS JetStream, not REST

The operator publishes directly to NATS JetStream on `ingest.changes.k8s`, reusing the `DocumentChangeEvent` schema and the existing `INGEST_CHANGES` stream defined in `packages/core/src/omniscience_core/queue/streams.py`. The Omniscience server's ingestion worker (the same one wired in #126/#127) consumes these events alongside events from every other connector — the K8s operator becomes one more producer on the same bus.

**Why NATS over REST:**
- **Latency.** A `Pod` `ADDED` should produce an entity in Omniscience within seconds (issue acceptance: 5 s). REST-with-retry adds round-trip cost per event; JetStream's ack semantics deliver fire-and-confirm in a single network hop with no application-level retry loop in the operator.
- **Retry and durability.** JetStream provides `max_deliver`, ack timeouts, and DLQ routing in the broker. The REST path would require us to add a per-cluster spool, replay on outage, and an at-least-once guarantee in application code — all of which JetStream gives us for free. The agentic connector got away without this because it polls; an event-driven operator does not have that luxury.
- **Schema drift risk.** REST would require versioning the operator against the server's REST API surface. NATS subjects + Pydantic-validated payloads are decoupled in the same way Postgres and the application schema are decoupled — the broker is content-agnostic and the consumer validates on receipt.
- **Network surface in customer clusters.** A NATS client opens **one outbound TCP connection** (port 4222 default, mTLS-wrappable). A REST path would require either a public Omniscience URL (a customer firewall negotiation per cluster) or a per-cluster reverse tunnel. NATS is the smaller blast surface.
- **Backpressure.** During a cluster cold-start (`Pod` storm on operator install) the operator emits a burst. NATS JetStream absorbs this into the stream's storage-bounded buffer. A REST path would either drop events (data loss), spool locally (operational complexity in the customer cluster), or apply backpressure to the watch (harming freshness). None of these is a good answer.

**Why not REST:**
- Faster to prototype, but every prototype-to-production hardening cost (retry, spool, replay, throttle) ends up reimplementing what JetStream already does.
- REST does have one win — observability through an HTTP gateway is easier than peering into a NATS subject. We accept the trade because the operator is already emitting Prometheus metrics for publish success/failure, and the consumer-side metrics on the existing stream cover the rest.

**Configuration of the NATS endpoint** is a Helm value (`nats.url`), not a discovery mechanism. In SaaS-managed Omniscience the value is the Omniscience-tenanted NATS endpoint; in self-hosted the value is the customer's own NATS cluster. The operator does not perform DNS heroics.

### 5. ACL invariant — workspace from mounted token, never from cluster

This is the section that costs the most thought.

**The invariant** (from ADR-0005 §Negative-Security and #117): every entity Omniscience writes to Neo4j or Qdrant must carry a non-null `workspace_id`. The operator emits events that flow through the ingestion worker (#127) which expects to resolve `workspace_id` from `Source.tenant_id`. For the operator path, **`workspace_id` is established at the operator** — the worker treats the operator-emitted event as already-tenanted, and the resolver short-circuits when `event.workspace_id` is set.

**Where the workspace comes from**: a Kubernetes `Secret` mounted into the operator pod via `envFrom` or a file mount. The secret carries:

```
OMNISCIENCE_WORKSPACE_ID    # UUID — the tenant boundary
OMNISCIENCE_NATS_URL        # nats://… — endpoint, not a credential
OMNISCIENCE_NATS_CREDS      # NATS user-credentials file content (optional)
OMNISCIENCE_CLUSTER_NAME    # cosmetic — appears in entity metadata, not security
```

The operator reads `OMNISCIENCE_WORKSPACE_ID` once at startup and **stamps it on every published event**. The value is treated as opaque — the operator does not validate it against any cluster-side state. There is no codepath that derives the workspace from a label, an annotation, a namespace name, or any field on the watched resource.

**Why this is the right threat model:**

The operator's `ServiceAccount` has cluster-wide read access to `Pod` (and, in follow-ups, more). Any namespace's user with edit rights on those resources can write arbitrary labels and annotations onto them. If the workspace were derived from those labels, a tenant who owns Namespace A could trivially inject a label that makes their resources appear under Tenant B's workspace in Omniscience — a **read-side cross-tenant data leak** in the most public form. The threat is not exotic; it is the default path if we do not actively prevent it.

By contrast, the `Secret` is provisioned by the cluster operator (the customer's platform team) at chart install time. It lives in the operator's own `omniscience-operator` namespace, with `RoleBinding` only to the operator's `ServiceAccount`. The threat-actor model is that the cluster admin already has the keys to everything and is not modeled as an attacker against their own tenant boundary.

**Three-layer defence-in-depth:**

1. **Operator layer** (this ADR): `workspace_id` read once at startup from a mounted secret, stamped on every event, never read from cluster-side state.
2. **NATS layer**: subject hierarchy is `ingest.changes.k8s.{workspace_id}` so a misconfigured consumer cannot accidentally see the wrong tenant's traffic; NATS user permissions can pin to a workspace prefix.
3. **Server layer** (existing, #127): the ingestion worker rejects events without `workspace_id`; adapter `_extract_workspace_id` and `_workspace_from_entities` reject entities without one.

The threat model section in the operator README enumerates each of these explicitly.

### 6. Cross-document consequences

- **`packages/connectors/src/omniscience_connectors/agentic/k8s.py`** is **not deprecated by this ADR**. The operator runs in parallel through GA. A follow-up issue in epic #98 will set the deprecation date once the operator's coverage matches the connector's. We expect at least one v0.x release with both pathways enabled, gated by a server-side flag preventing duplicate-write of the same entity from both paths.
- **`apps/server/src/omniscience_server/ingestion/events.py`** — `DocumentChangeEvent` may need an optional `workspace_id` field added to support operator-tenanted events. Tracked as a follow-up; out of scope for this issue.
- **`docs/architecture.md`** gets an "Operator pathway" section pointing at this ADR.
- **`helm/omniscience/Chart.yaml`** gets an `operator` sub-chart dependency, **off by default** in v0.2.

## Alternatives rejected

### Python (`kopf`) operator

Rejected. `kopf` is the right answer when you want to write a controller in 200 lines for a small CRD. It is the wrong answer for a watch-heavy controller that will eventually carry conversion webhooks and reach 10k+ watched objects per cluster. The ecosystem-fit and image-footprint arguments above are decisive. We acknowledge the in-house Python expertise but the operator is naturally a long-running, low-event-rate, low-memory binary — Go is what that workload wants.

### Separate repository (`omniscience-operator`)

Rejected for v0.2. Keeping it in-tree wins on schema co-evolution and CI lock-step. We retain the option to split later; the reverse — converging two repos — is materially harder.

### CRD-based API model

Rejected for v0.2. The operator is observation-only; a CRD adds a control plane the user does not need. Revisit when we have a real configuration surface that benefits from GitOps management.

### REST POST to Omniscience server

Rejected. The retry, durability, schema-versioning, and customer-firewall arguments above all point the same way. The freshness budget would be hard to hit with REST at any non-trivial event rate, and reimplementing JetStream's ack semantics in application code is a known anti-pattern.

### Watching from outside the cluster (kubeconfig from Omniscience server)

This is what the agentic connector does. Rejected for the operator path. Cross-cluster watch over the public API server endpoint requires a credential with cluster-wide read; that credential lives on the Omniscience server and becomes a high-value target. An in-cluster operator with a narrowly-scoped `ServiceAccount` is the smaller blast radius.

### Sidecar to every workload

Rejected. A per-pod sidecar would multiply resource cost by the workload count, would not see cluster-scoped resources, and would require workload owners to opt-in. A single cluster-scoped operator is the right unit of deployment.

### Push from K8s admission webhook

Rejected. Admission webhooks fire on object creation; they do not see steady-state changes (e.g. `Pod` status transitions) and they are in the synchronous critical path for `kubectl apply`. Adding Omniscience publish to that path is a reliability anti-pattern — a NATS outage would block deployments.

## Consequences

### Positive

- Watch-driven freshness lets us hit the §6 SLO target (sub-second to seconds) without polling churn.
- Causal-edge fidelity improves: every transition is observed, not inferred.
- Network and credential surface in customer clusters shrinks: one outbound NATS connection per cluster, one cluster-scoped `ServiceAccount` per cluster.
- Schema co-evolution stays under one CI lock by keeping the operator in-tree.
- The path to CRD-driven configuration (post-GA) is paved by `kubebuilder` from day one.

### Negative — engineering

- **Two languages in the monorepo.** Python contributors must run `make build` from `operator/` to verify Go changes; Go-side contributors must respect the existing pre-commit (Python-only). Mitigated by isolation under `operator/` and CI jobs that run conditionally on file paths.
- **Operator lifecycle ops** (image build, image scan, version skew between operator and server) becomes a real concern. The Helm chart pins both versions in `appVersion` to keep them aligned per release.

### Negative — security

- **`Secret` provisioning model** depends on the cluster admin doing the right thing. We document the model in the README's threat-model section; we cannot enforce it from code. Mitigated by NATS-level subject permissions: even if the wrong workspace is configured, NATS user perms catch it as a publish denial rather than a silent cross-tenant write.
- **NATS user credentials** are now an artefact every customer cluster must hold. Rotation is a Helm-values change plus a pod restart. We add rotation to the operator runbook follow-up (out of scope for #101).

### Negative — coverage gap during deprecation

- The operator and the agentic connector will both be live during the deprecation window. We must prevent double-write. Tracked as a follow-up issue in epic #98 (server-side dedup on the `external_id` of operator-emitted entities).

### Risks

- **Customer kubeconfig diversity.** Some clusters block egress to anything but a curated allow-list. We mitigate by documenting the NATS endpoint allow-list pattern in the chart README; customers with strict egress will configure their NATS endpoint as the allow target.
- **Operator restart loop on bad workspace value.** A misconfigured `Secret` causes the operator to fail-fast at startup. We accept this — fail-closed is the correct response — and surface a clear error in `kubectl logs`.
- **CRD pressure.** Reviewers (customer platform engineers) sometimes expect operators to ship CRDs by default. We pre-empt by saying so in the README: this operator deliberately ships without CRDs in v0.2.

## Revisit triggers

- Operator coverage reaches GA (per epic #98) — at that point we set the deprecation date for the agentic connector and revisit whether REST as a *fallback* pathway adds value (probably not, but worth re-examining).
- Action Mode (vision §6) lands — at that point CRDs become the right answer for the write-back surface; reopen the API-model decision.
- A customer requests GitOps-managed scope — reopen the CRD decision.
- NATS proves to be an ingress-firewall blocker for >25 % of evaluated deployments — reopen the ingestion-pathway decision in favour of a websocket-or-gRPC-over-mTLS server-side gateway. Not expected.
- Per-cluster scale exceeds 50k watched objects — reopen the framework decision (Go remains right; the question becomes sharding vs leader-election scope).

## Consequences for related docs

- `docs/architecture.md` — add an "Operator pathway" section pointing here. Out of scope for #101; tracked in epic #98.
- `docs/decisions/README.md` (if introduced) — list ADR-0007.
- `helm/omniscience/Chart.yaml` — add `omniscience-operator` sub-chart dependency, off by default. Skeleton landed by this issue at `helm/omniscience-operator/`.
- `packages/connectors/src/omniscience_connectors/agentic/k8s.py` — no changes in this issue. Its docstring will be updated with a deprecation pointer in the issue that sets the deprecation date.

## Links

- Parent epic: [#98](https://github.com/100rd/Omniscience/issues/98)
- This issue: [#101](https://github.com/100rd/Omniscience/issues/101)
- ACL invariant: [#117](https://github.com/100rd/Omniscience/issues/117)
- Storage cutover precedents: [#127](https://github.com/100rd/Omniscience/issues/127), [ADR-0005](0005-neo4j-as-graph-store.md), [ADR-0006](0006-qdrant-as-vector-store.md)
- Existing K8s connector to be deprecated post-GA: `packages/connectors/src/omniscience_connectors/agentic/k8s.py`
- Vision sections that drive this decision: [`docs/vision.md`](../vision.md) §5.1, §5.3, §5.5, §6
