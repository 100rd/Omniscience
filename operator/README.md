# omniscience-operator

In-cluster Kubernetes operator that watches resources and emits change events
to Omniscience over NATS JetStream. See [ADR-0007](../docs/decisions/0007-k8s-operator-architecture.md)
for the architecture decisions and the threat model.

**Status**: scaffold only (issue [#101](https://github.com/100rd/Omniscience/issues/101));
single `Pod` watcher. Coverage expansion and reconciliation worker land in
follow-up issues under epic [#98](https://github.com/100rd/Omniscience/issues/98).

## What it does, today

- Watches every `Pod` add / update / delete in every namespace it has access to.
- Maps each event to an Omniscience entity with `external_id` of the form
  `k8s_resource/Pod/{namespace}/{name}` and stamps a `workspace_id` from a
  Secret-mounted token.
- Publishes the resulting JSON event to NATS JetStream subject
  `ingest.changes.k8s.{workspace_id}` for the Omniscience ingestion worker
  to consume.

## What it does NOT do, today

- No CRDs (see ADR-0007 §API model).
- No reconciliation worker — observation only.
- No coverage of resources other than `Pod`.
- No automatic dedup against the agentic K8s connector. Run **one or the
  other** during the deprecation window. Tracked in epic #98.

## Layout

```
operator/
├── cmd/manager/         # entry point — wires controller-runtime + NATS
├── internal/
│   ├── config/          # env-driven config (workspace_id, NATS URL, etc.)
│   ├── controller/      # Pod reconciler
│   ├── entity/          # pure mapping: corev1.Pod -> Omniscience Event
│   └── publisher/       # NATS JetStream client
├── Dockerfile           # multi-stage; final image is distroless static
├── Makefile             # build / test / vet / lint / kind-up / kind-down
├── go.mod
└── README.md            # this file
```

## Configuration

All knobs are environment variables (read once at startup; see
[`internal/config/config.go`](internal/config/config.go)):

| Variable | Required | Notes |
|---|---|---|
| `OMNISCIENCE_WORKSPACE_ID` | **yes** | UUID. **Must come from a Secret.** Stamped on every event. |
| `OMNISCIENCE_CLUSTER_NAME` | **yes** | Human-readable label included in entity metadata. |
| `OMNISCIENCE_NATS_URL` | **yes** | e.g. `nats://omniscience-nats:4222`. Single endpoint. |
| `OMNISCIENCE_NATS_CREDS_FILE` | non-dev | Path to a NATS user-credentials file mounted from a Secret. |
| `OMNISCIENCE_NATS_SUBJECT` | no | Subject prefix; default `ingest.changes.k8s`. |
| `OMNISCIENCE_RESYNC_PERIOD_SECONDS` | no | Informer resync window in seconds; default 600. |
| `OMNISCIENCE_METRICS_ADDR` | no | controller-runtime metrics bind address; default `:8080`. |
| `OMNISCIENCE_PROBE_ADDR` | no | health/readiness probe bind address; default `:8081`. |
| `OMNISCIENCE_LEADER_ELECT` | no | `true` to enable leader election (multi-replica); default off. |

The operator **fails closed** at startup if `OMNISCIENCE_WORKSPACE_ID` is
missing or malformed. This is deliberate — see ADR-0007 §ACL.

## Threat model (short version)

The operator's `ServiceAccount` is cluster-scoped read on Pods. Any namespace
admin can write arbitrary labels and annotations on those Pods. Therefore:

1. The operator **never** reads `workspace_id` from a label, annotation, or
   any field on a watched resource. It comes exclusively from the Secret
   mount (`OMNISCIENCE_WORKSPACE_ID`).
2. The `Metadata` map on each emitted event is a closed allow-list (cluster,
   namespace, name, kind, node, phase, emitter). User-controlled labels are
   **not** copied through.
3. NATS subject hierarchy includes the `workspace_id` so subscriber-side
   permissions can pin to a specific tenant prefix.
4. Server-side adapters (`Neo4jGraphStore`, `QdrantVectorStore`) reject any
   event without a valid `workspace_id`. This is the third defence layer.

The full threat model is in ADR-0007 §ACL.

## Local dev loop

Prerequisites: Go 1.22+, Docker, [`kind`](https://kind.sigs.k8s.io/),
`kubectl`, `helm`.

```bash
# 1. Bring up a kind cluster
make kind-up

# 2. Install Omniscience (graph + vector + NATS) — see helm/omniscience
helm install omniscience ../helm/omniscience \
  --create-namespace --namespace omniscience \
  --set neo4j.password=devpass \
  --set qdrant.apiKey=devkey \
  --set secrets.postgresPassword=devpass \
  --set secrets.apiToken=devtoken

# 3. Build the operator image and load it into kind
make kind-load

# 4. Install the operator chart
helm install omniscience-operator ../helm/omniscience-operator \
  --create-namespace --namespace omniscience-operator \
  --set image.tag=dev \
  --set image.pullPolicy=IfNotPresent \
  --set workspaceId=11111111-2222-3333-4444-555555555555 \
  --set clusterName=dev \
  --set nats.url=nats://omniscience-nats.omniscience.svc.cluster.local:4222

# 5. Create a Pod and watch it become an entity in Omniscience
kubectl run hello --image=nginx:1.27 --namespace=default
# Within ~5 seconds, query the search MCP/REST surface for
# k8s_resource/Pod/default/hello

# 6. Tear down
make kind-down
```

## Build, test, lint

```bash
make build      # compile cmd/manager into bin/manager
make test       # go test -race -count=1 ./...
make vet        # go vet ./...
make lint       # golangci-lint run ./...
make helm-lint  # helm lint ../helm/omniscience-operator
```

## CI

A Go-specific CI job runs on changes under `operator/` or
`helm/omniscience-operator/`. See `.github/workflows/operator.yml`.
