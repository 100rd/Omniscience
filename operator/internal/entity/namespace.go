// Package entity — Namespace mapping.
//
// Namespaces are themselves cluster-scoped (a Namespace has no parent
// namespace). The external_id format is k8s_resource/Namespace/{name} —
// note the absence of a leading // that a naive concat would produce.
//
// ACL invariant (issue #159, ADR-0007 §ACL): a Namespace's labels are
// tenant-writable. They are NOT in the metadata allow-list. The closed
// list is: cluster, name, kind, phase, emitter.
package entity

import (
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// NamespaceToEvent maps a corev1.Namespace into an Event. Pure.
func NamespaceToEvent(ns *corev1.Namespace, action Action, workspaceID uuid.UUID, clusterName string, now time.Time) *Event {
	externalID := EntityKindK8sResource + "/Namespace/" + ns.Name
	uri := "kube://" + clusterName + "/Namespace/" + ns.Name

	meta := map[string]string{
		"cluster": clusterName,
		"name":    ns.Name,
		"kind":    "Namespace",
		"phase":   string(ns.Status.Phase),
		"emitter": "k8s-operator",
	}

	// IN_CLUSTER edge — the inverse-direction CONTAINS view is emitted
	// from the startup cluster anchor publish.
	edges := []EdgeRef{inClusterEdge(clusterName)}

	return &Event{
		SourceID:    deriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalID,
		URI:         uri,
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
		TopologyEdges:       edges,
	}
}
