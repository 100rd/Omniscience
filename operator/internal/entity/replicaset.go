// Mapper for appsv1.ReplicaSet.
//
// A ReplicaSet is owned by its parent Deployment (the K8s control plane
// populates metadata.ownerReferences on the child) and itself owns Pods
// (those edges are emitted by the Pod watcher when each Pod is reconciled,
// also from the child side). This mapper therefore emits ONE direction of
// the chain: parent → ReplicaSet, sourced exclusively from
// ownerReferences[*]. See ADR-0007 §causal-fidelity.
package entity

import (
	"time"

	"github.com/google/uuid"
	appsv1 "k8s.io/api/apps/v1"
)

const kindReplicaSet = "ReplicaSet"

// ReplicaSetToEvent maps an appsv1.ReplicaSet plus an action into an Event.
//
// Same shape and ACL invariant as DeploymentToEvent. The `workspaceID`
// parameter MUST come from operator config (ADR-0007 §ACL). User-controlled
// labels and annotations are NOT copied through.
func ReplicaSetToEvent(
	rs *appsv1.ReplicaSet,
	action Action,
	workspaceID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	ev := newWorkloadEvent(kindReplicaSet, rs.Namespace, rs.Name, action, workspaceID, clusterName, now)

	if rs.Spec.Replicas != nil {
		ev.Metadata["replicas"] = formatReplicas(*rs.Spec.Replicas)
	}
	ev.Metadata["available_replicas"] = formatReplicas(rs.Status.AvailableReplicas)

	ev.Edges = ownerEdges(rs.OwnerReferences, defaultedNamespace(rs.Namespace), ev.ExternalID)

	return ev
}
