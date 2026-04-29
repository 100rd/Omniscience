// Package entity — cluster anchor mapping.
//
// The Cluster anchor is the per-cluster top-level entity emitted once at
// operator startup. It is the root of the cluster's topology subgraph (every
// Node, Namespace, PersistentVolume, StorageClass carries an
// IN_CLUSTER edge to it) and the foundation #167 grows up to be the
// multi-cluster ClusterID identity model.
//
// The anchor entity's external_id is k8s_resource/Cluster/{cluster_name}
// (issue #159) and it carries a deterministic cluster_id (UUIDv5 derived
// from the workspace + cluster name) in metadata for #167 to reason about
// collisions. The mapping is pure — no informer, no client — so the test
// suite can assert idempotence without an envtest cluster.
package entity

import (
	"time"

	"github.com/google/uuid"
)

// ClusterToEvent builds the Cluster anchor Event. It is a pure function so
// callers can drive idempotence with the same input and assert byte-equal
// output (modulo emitted_at, which is the only injected clock).
//
// Optional cluster facts (kubernetesVersion, clusterEndpoint) are accepted
// rather than discovered, so this function can be unit-tested without a
// running API server. The startup hook in cmd/manager passes empty strings
// when the values aren't yet known; the consumer treats missing metadata
// keys as "unknown" rather than mis-stamped.
func ClusterToEvent(
	action Action,
	workspaceID uuid.UUID,
	clusterName string,
	kubernetesVersion string,
	clusterEndpoint string,
	now time.Time,
) *Event {
	externalID := ClusterExternalID(clusterName)
	uri := "kube://" + clusterName

	clusterID := DeriveClusterID(workspaceID, clusterName)

	// ACL invariant: cluster_name is operator config (Helm value), not
	// cluster-side state. cluster_id is derived deterministically from
	// (workspace_id, cluster_name); two workspaces sharing a cluster_name
	// emit two distinct cluster_ids. See ADR-0007 §ACL.
	meta := map[string]string{
		"cluster":    clusterName,
		"name":       clusterName,
		"kind":       "Cluster",
		"cluster_id": clusterID.String(),
		"emitter":    "k8s-operator",
	}
	if kubernetesVersion != "" {
		meta["kubernetes_version"] = kubernetesVersion
	}
	if clusterEndpoint != "" {
		meta["cluster_endpoint"] = clusterEndpoint
	}

	return &Event{
		SourceID:    deriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalID,
		URI:         uri,
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// inClusterEdge returns the single IN_CLUSTER edge every cluster-scoped or
// namespaced resource carries to the per-cluster anchor. Built once and
// reused by every mapper to avoid allocation churn during a Pod storm.
func inClusterEdge(clusterName string) EdgeRef {
	return EdgeRef{Kind: RelationInCluster, TargetExternalID: ClusterExternalID(clusterName)}
}
