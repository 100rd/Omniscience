// Mapper for appsv1.Deployment.
//
// A Deployment owns ReplicaSets (the K8s control plane writes
// metadata.ownerReferences on the child ReplicaSet, NOT on the parent
// Deployment). Therefore this mapper emits NO outbound OWNS edges from a
// Deployment object: the ReplicaSet watcher emits the Deployment→ReplicaSet
// edge from the child side, where the data actually lives. ADR-0007
// §causal-fidelity is explicit that edges come from K8s-populated
// ownerReferences and never from name-pattern inference.
package entity

import (
	"time"

	"github.com/google/uuid"
	appsv1 "k8s.io/api/apps/v1"
)

// kindDeployment is the K8s API Kind string used in external-id and metadata.
// Mixed case to match the Pod controller's existing convention (entity.go:
// "k8s_resource/Pod/...").
const kindDeployment = "Deployment"

// DeploymentToEvent maps an appsv1.Deployment plus an action into an Event.
//
// Pure function — no clients, no clock injection beyond `now`. The
// `workspaceID` parameter is the operator's configured tenant boundary;
// per ADR-0007 §ACL it MUST come from operator config and never from any
// field on the Deployment object (labels, annotations, namespace name).
//
// On a "deleted" reconcile the controller passes a stub Deployment with
// only namespace + name set; the mapper handles that defensively the same
// way PodToEvent does.
func DeploymentToEvent(
	d *appsv1.Deployment,
	action Action,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	ev := newWorkloadEvent(kindDeployment, d.Namespace, d.Name, action, workspaceID, clusterID, clusterName, now)

	// Replicas counts are status fields populated by the K8s control plane,
	// not user input — safe to copy. spec.replicas is *int32 (nil → server
	// default 1); status.availableReplicas is int32 (zero is meaningful).
	if d.Spec.Replicas != nil {
		ev.Metadata["replicas"] = formatReplicas(*d.Spec.Replicas)
	}
	ev.Metadata["available_replicas"] = formatReplicas(d.Status.AvailableReplicas)

	// Edges from ownerReferences. A Deployment is usually top-of-chain
	// (no owner) — but it CAN be owned by e.g. a Helm-managed CRD or by
	// a higher-level operator (ArgoCD Application). Honour whatever K8s
	// populates; do not invent.
	ev.Edges = ownerEdges(clusterID, d.OwnerReferences, defaultedNamespace(d.Namespace), ev.ExternalID)

	return ev
}
