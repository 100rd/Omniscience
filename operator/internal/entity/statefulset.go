// Mapper for appsv1.StatefulSet.
//
// A StatefulSet owns Pods (the Pod watcher emits StatefulSet→Pod edges
// from the child side); it can itself be owned by a higher-level CRD
// (Argo Rollout, OperatorHub operator). Edges this mapper emits are
// strictly the StatefulSet's own ownerReferences, never the StatefulSet→Pod
// direction (that lives on the Pod). See ADR-0007 §causal-fidelity.
package entity

import (
	"time"

	"github.com/google/uuid"
	appsv1 "k8s.io/api/apps/v1"
)

const kindStatefulSet = "StatefulSet"

// StatefulSetToEvent maps an appsv1.StatefulSet plus an action into an Event.
//
// Same shape and ACL invariant as DeploymentToEvent. The `workspaceID`
// parameter MUST come from operator config (ADR-0007 §ACL).
func StatefulSetToEvent(
	ss *appsv1.StatefulSet,
	action Action,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	ev := newWorkloadEvent(kindStatefulSet, ss.Namespace, ss.Name, action, workspaceID, clusterID, clusterName, now)

	if ss.Spec.Replicas != nil {
		ev.Metadata["replicas"] = formatReplicas(*ss.Spec.Replicas)
	}
	ev.Metadata["available_replicas"] = formatReplicas(ss.Status.AvailableReplicas)

	ev.Edges = ownerEdges(clusterID, ss.OwnerReferences, defaultedNamespace(ss.Namespace), ev.ExternalID)

	return ev
}
