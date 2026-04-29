// Package entity maps Kubernetes API objects to Omniscience entity events.
//
// The mapping is deliberately small and pure (no I/O, no clients) so it is
// trivially unit-testable without a live cluster. Every event carries the
// workspace_id stamped from operator config — see ADR-0007 §ACL.
package entity

import (
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// Action mirrors the action enum on omniscience_server.ingestion.events
// .DocumentChangeEvent ("created", "updated", "deleted").
type Action string

// Action values. Match the Python-side Literal exactly so JetStream
// consumers can validate against the Pydantic model without translation.
const (
	ActionCreated Action = "created"
	ActionUpdated Action = "updated"
	ActionDeleted Action = "deleted"
)

// SourceType is the connector-type string. Distinct from the agentic
// connector's "k8s-agentic" so server-side dedup can tell the two apart
// during the parallel-deprecation window (epic #98).
const SourceType = "k8s-operator"

// EntityKindPod identifies a Pod entity in the Omniscience graph. Future
// kinds (Deployment, Service, etc.) will be added in follow-up issues.
const EntityKindPod = "k8s_resource"

// EntityKindK8sResource is the canonical kind prefix for every Kubernetes
// resource entity emitted by the operator. The Pod-specific alias above is
// preserved for source-compatibility with the v0.2 scaffold; new kinds use
// this constant directly.
const EntityKindK8sResource = "k8s_resource"

// Edge relation types emitted by the operator. Kept as named constants so
// the consumer side can switch over a closed set rather than match strings
// at every call site. See ADR-0007 — edges are derived from K8s-native
// fields only and never inferred from labels.
const (
	// RelationInCluster connects a cluster-scoped or namespaced resource to
	// the per-cluster anchor entity (issue #159; #167's identity story
	// builds on this).
	RelationInCluster = "in_cluster"
	// RelationInNamespace connects a namespaced resource to its Namespace
	// anchor (added per-mapper in follow-up Pod/Deployment edits — this
	// issue lands the Namespace target side only).
	RelationInNamespace = "in_namespace"
	// RelationOfClass connects a PersistentVolume to its StorageClass.
	RelationOfClass = "of_class"
)

// Event is the operator-side payload published to NATS. Its JSON shape is a
// superset of omniscience_server.ingestion.events.DocumentChangeEvent: we
// add WorkspaceID (operator path is pre-tenanted; the worker accepts it
// without re-resolving) and Metadata (cluster name, kind, labels).
//
// Field tags use snake_case to match the Python schema verbatim.
type Event struct {
	// SourceID is a deterministic UUIDv5 derived from (workspace_id,
	// cluster_name) — there is no user-managed sources row for the
	// operator path. The server treats this as the logical Source.id.
	SourceID uuid.UUID `json:"source_id"`

	// SourceType is always SourceType (= "k8s-operator").
	SourceType string `json:"source_type"`

	// ExternalID is the stable, source-native id. For a Pod:
	//   "k8s_resource/Pod/{namespace}/{name}"
	// matches the entity-id format in issue #101.
	ExternalID string `json:"external_id"`

	// URI is a human-readable address for citation. We use a kube:// scheme
	// so it cannot be confused with a fetchable HTTP URL.
	URI string `json:"uri"`

	// Action is "created", "updated", or "deleted".
	Action Action `json:"action"`

	// WorkspaceID is the tenant boundary. Set from operator config; never
	// derived from cluster-side state on the watched resource.
	WorkspaceID uuid.UUID `json:"workspace_id"`

	// EmittedAt is the operator clock time at publish. Distinct from the
	// resource's CreationTimestamp; useful for end-to-end latency analysis.
	EmittedAt time.Time `json:"emitted_at"`

	// Metadata is a small, JSON-safe map for downstream filtering. Kept
	// flat (no nested maps beyond labels) so the Pydantic side can validate
	// without a discriminated union.
	Metadata map[string]string `json:"metadata"`

	// Edges carries OWNS relationships sourced from
	// metadata.ownerReferences on the watched resource. Optional and
	// omitempty for backward compatibility with v0.2 consumers. See ADR-0007
	// §causal-fidelity: edges are NEVER inferred from name patterns.
	Edges []OwnerEdge `json:"edges,omitempty"`

	// TopologyEdges is the (optional) topology edge list emitted alongside
	// this entity. Always derived from K8s-native pointers (spec.rules,
	// spec.volumes, spec.nodeName, spec.storageClassName, …) — never
	// inferred from labels. Pod and workload events leave this field nil;
	// consumers MUST treat absent and empty as identical. See common.go for
	// EdgeRef.
	TopologyEdges []EdgeRef `json:"topology_edges,omitempty"`
}

// PodToEvent maps a corev1.Pod plus an action into an Event. Pure function
// — takes no clients, no clock injection beyond `now`. The `now` argument is
// taken explicitly so unit tests can pin a deterministic timestamp.
func PodToEvent(pod *corev1.Pod, action Action, workspaceID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := pod.Namespace
	if namespace == "" {
		// Cluster-scoped Pod is malformed — but be defensive rather than
		// panic. Mark as "default" so the entity id is still well-formed
		// and the event is debuggable on the consumer side.
		namespace = "default"
	}

	externalID := EntityKindPod + "/Pod/" + namespace + "/" + pod.Name
	uri := "kube://" + clusterName + "/" + namespace + "/Pod/" + pod.Name

	// SourceID is deterministic per (workspace, cluster) so retries and
	// restarts produce stable lineage. UUIDv5 over the namespace
	// "omniscience-operator" keeps the value stable across restarts and
	// upgrades without coordination.
	sourceID := deriveSourceID(workspaceID, clusterName)

	meta := map[string]string{
		"cluster":   clusterName,
		"namespace": namespace,
		"name":      pod.Name,
		"kind":      "Pod",
		"node":      pod.Spec.NodeName,
		"phase":     string(pod.Status.Phase),
		"emitter":   "k8s-operator",
	}
	// Intentionally do NOT copy arbitrary user labels into metadata. Doing
	// so risks logging or storing tenant-supplied PII, and labels can carry
	// adversarial content (see ADR-0007 §ACL — labels are tenant-writable
	// state and must not flow into security decisions). Selectively-allowed
	// labels are a follow-up; v0.2 ships with a closed allow-list.

	return &Event{
		SourceID:    sourceID,
		SourceType:  SourceType,
		ExternalID:  externalID,
		URI:         uri,
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// deriveSourceID returns a stable UUIDv5 derived from a fixed namespace and
// the (workspaceID, clusterName) pair. The result is identical across
// operator restarts and pod identity changes, which gives the server a
// stable foreign key for the operator-emitted Source.
func deriveSourceID(workspaceID uuid.UUID, clusterName string) uuid.UUID {
	return uuid.NewSHA1(nsOmniscienceOperator, []byte(workspaceID.String()+"/"+clusterName))
}

// nsOmniscienceOperator is the fixed UUIDv4 namespace used to derive every
// operator-side UUIDv5. Sharing one namespace across SourceID and ClusterID
// keeps the derivation reproducible from any operator instance without
// coordination. Defined as a package var (computed once) so callers can use
// it without paying MustParse on every invocation.
var nsOmniscienceOperator = uuid.MustParse("d6c4a5b1-3e7f-4a92-9c2d-7e1f8b6c4a5b")

// DeriveClusterID returns a deterministic UUIDv5 identifying a cluster
// instance for #167's multi-cluster identity model. The derivation is:
//
//	uuid5(nsOmniscienceOperator, "cluster/" + workspaceID + "/" + clusterName)
//
// This collides with no SourceID derivation (different prefix path), is
// deterministic across operator restarts, and is the value the cluster
// anchor entity carries as metadata["cluster_id"]. Two workspaces with the
// same OMNISCIENCE_CLUSTER_NAME produce two distinct cluster_ids — that's
// the per-tenant isolation the ACL invariant requires.
func DeriveClusterID(workspaceID uuid.UUID, clusterName string) uuid.UUID {
	return uuid.NewSHA1(nsOmniscienceOperator, []byte("cluster/"+workspaceID.String()+"/"+clusterName))
}

// ClusterExternalID returns the canonical external_id for the per-cluster
// anchor entity. The format matches the issue body verbatim:
//
//	k8s_resource/Cluster/{cluster_name}
//
// The form deliberately omits the workspace_id — server-side uniqueness is
// the (workspace_id, external_id) composite, so two workspaces that share a
// cluster_name produce two distinct rows. #167 will flip a same-workspace
// collision into a structured error.
func ClusterExternalID(clusterName string) string {
	return EntityKindK8sResource + "/Cluster/" + clusterName
}
