// Common helpers shared by every workload-kind mapper.
//
// Lives in its own file (per ADR-0007 §scope-guardrail "do not edit entity.go
// for shared helpers — extract into a sibling file") so the Pod-only entry
// point stays unchanged while the workload watchers (#157) reuse a single
// implementation of external-id, URI, ownership-edge, and metadata builders.
package entity

import (
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// EntityKindPrefix is the entity-id namespace shared by every K8s resource
// emitted by the operator. The Pod path uses this same constant value via
// EntityKindPod; declaring it once here keeps the two in lockstep.
const EntityKindPrefix = "k8s_resource"

// EmitterLabel is the metadata.emitter value attached to every operator-
// emitted event. Distinct from the agentic connector's emitter so server-
// side dedup (#164) can disambiguate during the parallel-deprecation window.
const EmitterLabel = "k8s-operator"

// EdgeRelationOwns is the only ownership-edge predicate emitted by v0.2
// workload watchers. Server-side ingestion materialises this as an OWNS
// edge in the bitemporal graph (ADR-0008).
const EdgeRelationOwns = "OWNS"

// OwnerEdge describes a single ownership relationship the operator observed
// on a watched resource via metadata.ownerReferences. Edges are NEVER
// inferred from name patterns — only K8s-control-plane-populated owner
// references produce an edge (ADR-0007 §causal-fidelity).
//
// JSON tags use snake_case to match the server-side ingester contract.
type OwnerEdge struct {
	// Relation is the predicate. Always "OWNS" in v0.2.
	Relation string `json:"relation"`

	// FromExternalID is the external_id of the parent (the owner). Format:
	// "k8s_resource/{Kind}/{namespace}/{name}".
	FromExternalID string `json:"from_external_id"`

	// ToExternalID is the external_id of the child (the watched resource).
	ToExternalID string `json:"to_external_id"`

	// FromKind is the K8s Kind of the parent (e.g. "Deployment"). Sourced
	// from ownerReferences[*].Kind, which K8s itself populates.
	FromKind string `json:"from_kind"`

	// FromUID is the K8s UID of the parent. Useful for the server to
	// distinguish two same-name owners across recreate events.
	FromUID string `json:"from_uid"`
}

// externalID returns the canonical external-id for any K8s resource emitted
// by the operator. As of issue #167 the cluster_id is the second segment so
// the same Kind+namespace+name in two clusters under one workspace produce
// DISTINCT external_ids:
//
//	"k8s_resource/{cluster_id}/{Kind}/{namespace}/{name}"
//
// kind is passed as the K8s API Kind verbatim (mixed case, e.g. "Deployment")
// to match the Pod controller's existing convention — see entity.go::PodToEvent.
//
// clusterID is the operator-config UUID; pass uuid.Nil only in unit-test
// fixtures that don't care about the cluster axis (production callers MUST
// pass the resolved cfg.ClusterID).
func externalID(clusterID uuid.UUID, kind, namespace, name string) string {
	return EntityKindPrefix + "/" + clusterID.String() + "/" + kind + "/" + namespace + "/" + name
}

// externalIDClusterScoped returns the cluster-scoped external-id for resources
// without a namespace (Node, Namespace, PersistentVolume, StorageClass,
// Cluster anchor):
//
//	"k8s_resource/{cluster_id}/{Kind}/{name}"
func externalIDClusterScoped(clusterID uuid.UUID, kind, name string) string {
	return EntityKindPrefix + "/" + clusterID.String() + "/" + kind + "/" + name
}

// resourceURI returns the citation URI for any K8s resource:
// "kube://{cluster}/{namespace}/{Kind}/{name}". The kube:// scheme is
// non-fetchable on purpose — see entity.go::PodToEvent.
func resourceURI(clusterName, namespace, kind, name string) string {
	return "kube://" + clusterName + "/" + namespace + "/" + kind + "/" + name
}

// defaultedNamespace returns "default" when ns is empty. Watched workload
// kinds are namespaced (Deployment, ReplicaSet, StatefulSet, DaemonSet, Job),
// so an empty namespace is malformed input — but we are defensive rather
// than panicking, matching the Pod controller's behaviour.
func defaultedNamespace(ns string) string {
	if ns == "" {
		return "default"
	}
	return ns
}

// ownerEdges constructs the OWNS edges from a watched resource's
// metadata.ownerReferences. Each ownerReference yields exactly one edge
// pointing FROM the owner TO the watched resource (the owner OWNS the child).
//
// Edges are emitted only when ownerReferences is populated by K8s itself.
// Empty input returns nil — never invent edges from naming heuristics.
//
// childExternalID is the external-id of the watched (child) resource.
// childNamespace is the namespace of the child; ownerReferences point to
// objects in the SAME namespace per K8s API rules (cross-namespace owner
// references are rejected by the API server).
func ownerEdges(clusterID uuid.UUID, refs []metav1.OwnerReference, childNamespace, childExternalID string) []OwnerEdge {
	if len(refs) == 0 {
		return nil
	}
	out := make([]OwnerEdge, 0, len(refs))
	for _, ref := range refs {
		if ref.Kind == "" || ref.Name == "" {
			// Defensive: a malformed ownerReference cannot be turned into a
			// well-formed external-id. Skip it rather than emit garbage.
			continue
		}
		out = append(out, OwnerEdge{
			Relation:       EdgeRelationOwns,
			FromExternalID: externalID(clusterID, ref.Kind, childNamespace, ref.Name),
			ToExternalID:   childExternalID,
			FromKind:       ref.Kind,
			FromUID:        string(ref.UID),
		})
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// baseMetadata returns the closed allow-list of metadata fields shared by
// every workload kind. Per ADR-0007 §ACL the allow-list is exhaustive: user-
// supplied labels and annotations are NOT copied through. Kind-specific
// fields (replicas, available_replicas, …) are added by the per-kind mapper
// after calling this helper.
//
// Issue #167: cluster_id is included in the allow-list so consumers can
// filter / group by cluster identity without re-parsing the external_id.
func baseMetadata(clusterID uuid.UUID, clusterName, namespace, kind, name string) map[string]string {
	return map[string]string{
		"cluster":    clusterName,
		"cluster_id": clusterID.String(),
		"namespace":  namespace,
		"name":       name,
		"kind":       kind,
		"emitter":    EmitterLabel,
	}
}

// newWorkloadEvent constructs the Event skeleton common to every workload
// kind. Per-kind mappers fill in Metadata extensions (replicas etc.) and the
// Edges slice, then return the result.
//
// The function is deliberately small — it is the single point at which
// SourceID derivation, EmittedAt stamping, and the ACL invariant (workspace_id
// from operator config, never from cluster state) are applied. Tests
// exercise it indirectly via every per-kind mapper.
func newWorkloadEvent(
	kind, namespace, name string,
	action Action,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	ns := defaultedNamespace(namespace)
	extID := externalID(clusterID, kind, ns, name)
	return &Event{
		SourceID:    deriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  extID,
		URI:         resourceURI(clusterName, ns, kind, name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    baseMetadata(clusterID, clusterName, ns, kind, name),
	}
}

// formatReplicas renders an int32 replica count as decimal. Centralised so
// every workload mapper formats the same way (the Pydantic ingester treats
// the value as a string for forward compatibility with non-integer phases).
func formatReplicas(n int32) string {
	return strconv.FormatInt(int64(n), 10)
}

// ---------------------------------------------------------------------------
// #158 — networking + config watcher helpers (added on top of #157's base)
// ---------------------------------------------------------------------------

// Edge kinds emitted by operator mappers. Centralised to keep the controller
// graph contract reviewable in one place.
const (
	// EdgeKindSelects: Service -> Pod, resolved via Endpoints (NEVER by
	// re-evaluating selectors). The Endpoints controller owns this edge.
	EdgeKindSelects = "SELECTS"

	// EdgeKindRoutesTo: Ingress -> Service, from spec.rules[].http.paths[]
	// .backend.service.name.
	EdgeKindRoutesTo = "ROUTES_TO"

	// EdgeKindAppliesTo: NetworkPolicy -> Pod, derived from spec.podSelector.
	// The operator emits the *selector* on the NetworkPolicy entity; resolving
	// which pods match is left to the server side (label index lives there).
	EdgeKindAppliesTo = "APPLIES_TO"

	// EdgeKindConsumes: Pod -> ConfigMap | Secret, from spec.volumes[] and
	// spec.containers[].envFrom[]. The Pod controller owns these edges.
	EdgeKindConsumes = "CONSUMES"
)

// EdgeRef is a lightweight, JSON-safe edge descriptor emitted alongside an
// entity. Topology edges are derived from K8s-native pointers (spec.rules,
// spec.volumes, etc.) — we NEVER infer edges from labels or annotations.
type EdgeRef struct {
	Kind             string `json:"kind"`
	TargetExternalID string `json:"target_external_id"`
}

// DeriveSourceID returns the deterministic UUIDv5 source id for a
// (workspaceID, clusterName) pair. Exported so #158's mappers (Service,
// Ingress, etc.) share the same derivation as PodToEvent.
func DeriveSourceID(workspaceID uuid.UUID, clusterName string) uuid.UUID {
	return deriveSourceID(workspaceID, clusterName)
}

// resolveNamespace returns metaNamespace, falling back to "default" for
// resources that arrive with an empty namespace. Public alias of
// defaultedNamespace for the #158 mapper call sites.
func resolveNamespace(metaNamespace string) string {
	return defaultedNamespace(metaNamespace)
}

// externalIDFor returns the multi-cluster-aware external_id for a namespaced
// resource: "k8s_resource/{cluster_id}/{kind}/{namespace}/{name}". Public
// alias of externalID for the #158 mapper call sites.
func externalIDFor(clusterID uuid.UUID, kind, namespace, name string) string {
	return externalID(clusterID, kind, namespace, name)
}

// externalIDForClusterScoped returns the multi-cluster-aware external_id for
// a cluster-scoped resource: "k8s_resource/{cluster_id}/{kind}/{name}".
// Public alias of externalIDClusterScoped.
func externalIDForClusterScoped(clusterID uuid.UUID, kind, name string) string {
	return externalIDClusterScoped(clusterID, kind, name)
}

// uriFor returns "kube://{cluster}/{namespace}/{kind}/{name}". Public alias
// of resourceURI for the #158 mapper call sites.
func uriFor(clusterName, namespace, kind, name string) string {
	return resourceURI(clusterName, namespace, kind, name)
}

// sortedKeys returns the keys of m in lexicographic order. Used by
// ConfigMapToEvent / SecretToEvent to emit a stable `data_keys` list (the
// *names* of the keys, never their values — the security invariant).
func sortedKeys[V any](m map[string]V) []string {
	if len(m) == 0 {
		return nil
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
// formatLabelSelector renders a metav1.LabelSelector to a deterministic
// string form so it can be carried in metadata as a single value. The
// rendering is the standard Kubernetes label-selector syntax (matchLabels are
// emitted as `k=v` joined by `,`, in lexicographic key order, followed by
// matchExpressions in insertion order). Empty selector renders as the empty
// string.
//
// We carry the selector as opaque text because the operator does NOT resolve
// pods locally — see EdgeKindAppliesTo doc.
func formatLabelSelector(sel *metav1.LabelSelector) string {
	if sel == nil {
		return ""
	}
	out := ""
	keys := make([]string, 0, len(sel.MatchLabels))
	for k := range sel.MatchLabels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for i, k := range keys {
		if i > 0 {
			out += ","
		}
		out += k + "=" + sel.MatchLabels[k]
	}
	for _, expr := range sel.MatchExpressions {
		if out != "" {
			out += ","
		}
		out += expr.Key + " " + string(expr.Operator)
		if len(expr.Values) > 0 {
			out += " (" + joinSorted(expr.Values) + ")"
		}
	}
	return out
}

// joinSorted returns vs sorted lexicographically and joined with ",".
// Defined here (not inline) so the formatLabelSelector body stays small.
func joinSorted(vs []string) string {
	cp := make([]string, len(vs))
	copy(cp, vs)
	sort.Strings(cp)
	out := ""
	for i, v := range cp {
		if i > 0 {
			out += ","
		}
		out += v
	}
	return out
}

