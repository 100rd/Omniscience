# K8s operator — Secret & ConfigMap handling

This document explains why the omniscience-operator's networking + config
watchers (issue #158) drop Secret values unconditionally and ConfigMap
values by default, and how to opt a specific ConfigMap into value indexing.

## Hard rule — Secret values

**The operator NEVER emits Secret `data`, `stringData`, `binaryData`, or any
value bytes — ever, under any condition.**

This is enforced at three layers:

1. **Mapper** — `operator/internal/entity/secret.go::SecretToEvent` reads
   only metadata fields (namespace, name, type, data key *names*). The raw
   values are never read off the `corev1.Secret`, so they cannot reach the
   `Event` payload by accident.
2. **Structural test** — `operator/internal/entity/secret_test.go::
   TestSecretToEvent_NeverLeaksValues` constructs a Secret carrying canary
   strings (`CANARY-PWD-XYZZY-1`, etc.) in `data` and `stringData`,
   marshals the emitted `Event` to JSON, and substring-searches for the
   canaries (raw and base64-encoded forms). The test fails if a single byte
   of a value appears anywhere in the payload.
3. **RBAC** — the operator's `ClusterRole` requests only `get/list/watch`
   on `secrets`. There are no write verbs anywhere in the chart. The
   `get/list` permission is unavoidable for the watcher to function; the
   value-drop in the mapper is the security cap.

The hard rule extends to the per-resource `omniscience.io/index-data`
annotation: **the Secret mapper does not honour the opt-in.** That
annotation only opts ConfigMaps into value indexing.

### Why values are forbidden

- Secrets routinely carry credentials (database passwords, API tokens, TLS
  private keys). Indexing them puts every downstream component (graph
  store, vector store, retrieval API, audit logs, replication) in the
  credential blast radius.
- Kubernetes itself protects Secret values via etcd encryption-at-rest;
  pulling them through Omniscience would route them into stores that may
  have different at-rest guarantees.
- The retrieval signal from a Secret name (e.g. `stripe-prod-key`) is
  already strong enough for the incident-resolution use case. Knowing
  which Pods consume which Secret-name (Wave 3 follow-up: Pod-side
  CONSUMES edges) closes the loop without ever reading the value.

### What IS emitted for a Secret

The closed allow-list (`operator/internal/entity/secret.go`):

| Key            | Source                                      |
|----------------|---------------------------------------------|
| `cluster`      | operator config                             |
| `namespace`    | `metadata.namespace`                        |
| `name`         | `metadata.name`                             |
| `kind`         | constant `"Secret"`                         |
| `secret_type`  | `type` (e.g. `Opaque`, `kubernetes.io/tls`) |
| `data_keys`    | sorted comma-joined list of *names* in `data` ∪ `stringData` |
| `data_count`   | total key count as string                   |
| `emitter`      | constant `"k8s-operator"`                   |

Notably absent:

- `data.*` values (the secret content)
- `stringData.*` values (the cleartext form)
- `binaryData.*` values (the binary form)
- User-supplied labels (tenant-writable, not safe to copy verbatim)
- User-supplied annotations (same)

Tenant-writable labels and annotations are dropped because they are an
ACL-leak surface: a malicious workload could plant a label like
`omniscience.io/workspace-id=<other-workspace>` and, if we copied it into
Event metadata, expose itself to the wrong tenant. See ADR-0007 §ACL.

## ConfigMap — metadata only by default, opt-in for values

ConfigMaps follow the same posture as Secrets *by default*: only the
allow-list of metadata fields and the sorted list of key *names* is
emitted. The values are dropped.

To allow a *specific* ConfigMap to emit its `data` values, set this
annotation on the ConfigMap:

```yaml
metadata:
  annotations:
    omniscience.io/index-data: "true"
```

The annotation must be exactly the literal string `"true"`. Any other
value (`"True"`, `"1"`, `"yes"`) is treated as a no-opt — the gate is
deliberately conservative, mirroring the strict matching in
`isIndexDataOptIn`.

When the opt-in is present:

- `data` values are emitted as a JSON-encoded map under
  `metadata.data_values_json`.
- `binaryData` values are STILL dropped (binary blobs are almost never
  useful for retrieval and bypass the human-reviewable surface).
- `metadata.indexed = "true"` so consumers can distinguish opt-in payloads
  from default ones.

When the opt-in is absent (default):

- No values appear anywhere in the payload.
- `metadata.indexed = "false"`.

The opt-in is per-ConfigMap and namespace-scoped; the operator does not
expose a chart-level switch that would flip the default for all
ConfigMaps. Forcing per-resource opt-in keeps the explicit-consent
boundary visible to the ConfigMap author.

### Why opt-in for ConfigMap (and not for Secret)

ConfigMaps frequently carry application configuration that *is* useful to
index — feature flags, internal hostnames, knob values. The cost of
indexing them is real but bounded: the values are not credentials.

Secrets, by definition, are credentials. The cost of indexing them is
unbounded (a leak compromises the whole credential). The asymmetry in
posture matches the asymmetry in risk.

## Topology edges

The networking + config watchers also emit topology edges where K8s
itself has authoritative pointers:

- `Service` -[`SELECTS`]-> `Pod` — owned by the **Endpoints** controller,
  resolved via `Endpoints.subsets[].addresses[].targetRef`. The Service
  controller does NOT re-evaluate selectors locally.
- `Ingress` -[`ROUTES_TO`]-> `Service` — from `spec.rules[].http.paths[]
  .backend.service.name` and `spec.defaultBackend.service.name`.
- `NetworkPolicy` -[`APPLIES_TO`]-> `Pod` — NOT emitted by the operator
  (deferred to the reconciliation worker, #163, which has the cross-stream
  label index). The NetworkPolicy entity carries the rendered selector
  string in `metadata.pod_selector` so the resolution can happen
  server-side without the operator re-emitting.
- `Pod` -[`CONSUMES`]-> `ConfigMap` and `Pod` -[`CONSUMES`]-> `Secret`
  — owned by the **Pod controller**. Tracked as a Wave-3 follow-up; this
  PR does not touch the Pod controller.

## ACL invariant — workspace_id

The operator's `workspace_id` is fixed at startup, sourced from a
secret-mounted token. **No watched resource — Service annotation,
ConfigMap label, Secret type, NetworkPolicy podSelector — can change the
workspace_id at runtime.** This invariant is asserted by the existing Pod
controller test suite and is preserved by every new mapper added in
issue #158.

A label like `omniscience.io/workspace-id=<other>` planted on a Service
is silently ignored.

## Roll-out

The chart enables all networking + config watchers by default. There is no
per-kind opt-out toggle in `values.yaml` — the chart's posture is "all
watchers on, security cap in the mapper". Removing kinds is a chart-fork
exercise rather than a config knob.

## Verification recipe

In a `kind` cluster:

```bash
# Create a Secret with a canary value.
kubectl create secret generic stripe-prod-key \
  --from-literal=token=devtoken-CANARY-XYZZY

# Capture the operator's NATS publish stream (assuming nats CLI configured).
nats sub "k8s.operator.events.${WORKSPACE_ID}" > /tmp/events.log &

# Wait a few seconds, then assert no canary leak.
grep -F 'CANARY-XYZZY' /tmp/events.log && echo LEAK || echo OK
```

Expected output: `OK`. The structural test
`TestSecretToEvent_NeverLeaksValues` makes the same assertion at the
mapper boundary in CI; the kind-cluster recipe is the end-to-end
confirmation.
