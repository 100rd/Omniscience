// Mapper for appsv1.DaemonSet.
//
// A DaemonSet owns Pods (one per node, modulo nodeSelector). Pod-side
// reconciliation emits the DaemonSet→Pod edges from the child side via
// ownerReferences. This mapper emits the DaemonSet's own owner edges only.
// See ADR-0007 §causal-fidelity.
package entity

import (
	"time"

	"github.com/google/uuid"
	appsv1 "k8s.io/api/apps/v1"
)

const kindDaemonSet = "DaemonSet"

// DaemonSetToEvent maps an appsv1.DaemonSet plus an action into an Event.
//
// Replicas semantics differ from Deployment / ReplicaSet / StatefulSet:
// DaemonSet has no spec.replicas (the count is determined by node
// selection). We surface the equivalent status fields:
//
//   - desired_number_scheduled — how many nodes match the DaemonSet
//   - number_available — how many of those have an Available Pod
//
// These are renamed from the K8s field names to fit the workload metadata
// vocabulary: "replicas" = desired, "available_replicas" = available.
// Renaming keeps the consumer-side allow-list flat across all five kinds.
func DaemonSetToEvent(
	ds *appsv1.DaemonSet,
	action Action,
	workspaceID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	ev := newWorkloadEvent(kindDaemonSet, ds.Namespace, ds.Name, action, workspaceID, clusterName, now)

	ev.Metadata["replicas"] = formatReplicas(ds.Status.DesiredNumberScheduled)
	ev.Metadata["available_replicas"] = formatReplicas(ds.Status.NumberAvailable)

	ev.Edges = ownerEdges(ds.OwnerReferences, defaultedNamespace(ds.Namespace), ev.ExternalID)

	return ev
}
