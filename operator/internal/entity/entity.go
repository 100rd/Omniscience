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
	// nsOmniscienceOperator is a fixed UUIDv4 picked once for this purpose.
	// Defining it here (not in config) makes the derivation reproducible
	// from any operator instance without coordination.
	nsOmniscienceOperator := uuid.MustParse("d6c4a5b1-3e7f-4a92-9c2d-7e1f8b6c4a5b")
	name := workspaceID.String() + "/" + clusterName
	return uuid.NewSHA1(nsOmniscienceOperator, []byte(name))
}
