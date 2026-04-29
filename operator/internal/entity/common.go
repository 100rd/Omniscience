// Common helpers shared by every workload-kind mapper.
//
// Lives in its own file (per ADR-0007 §scope-guardrail "do not edit entity.go
// for shared helpers — extract into a sibling file") so the Pod-only entry
// point stays unchanged while the workload watchers (#157) reuse a single
// implementation of external-id, URI, ownership-edge, and metadata builders.
package entity

import (
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
// by the operator: "k8s_resource/{Kind}/{namespace}/{name}".
//
// kind is passed as the K8s API Kind verbatim (mixed case, e.g. "Deployment")
// to match the Pod controller's existing convention — see entity.go::PodToEvent.
func externalID(kind, namespace, name string) string {
	return EntityKindPrefix + "/" + kind + "/" + namespace + "/" + name
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
func ownerEdges(refs []metav1.OwnerReference, childNamespace, childExternalID string) []OwnerEdge {
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
			FromExternalID: externalID(ref.Kind, childNamespace, ref.Name),
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
func baseMetadata(clusterName, namespace, kind, name string) map[string]string {
	return map[string]string{
		"cluster":   clusterName,
		"namespace": namespace,
		"name":      name,
		"kind":      kind,
		"emitter":   EmitterLabel,
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
	clusterName string,
	now time.Time,
) *Event {
	ns := defaultedNamespace(namespace)
	extID := externalID(kind, ns, name)
	return &Event{
		SourceID:    deriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  extID,
		URI:         resourceURI(clusterName, ns, kind, name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    baseMetadata(clusterName, ns, kind, name),
	}
}

// formatReplicas renders an int32 replica count as decimal. Centralised so
// every workload mapper formats the same way (the Pydantic ingester treats
// the value as a string for forward compatibility with non-integer phases).
func formatReplicas(n int32) string {
	return strconv.FormatInt(int64(n), 10)
}
