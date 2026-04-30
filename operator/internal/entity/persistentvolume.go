// Package entity — PersistentVolume mapping.
//
// PVs are cluster-scoped. The external_id is k8s_resource/PersistentVolume/{name}.
//
// PVs carry an OF_CLASS edge to their StorageClass when spec.storageClassName
// is set. The empty-string case is the deprecated implicit-class form (PVs
// provisioned by a default StorageClass before the field was required); the
// mapper emits no OF_CLASS edge in that case rather than a dangling edge to
// a non-existent class. A reconciliation pass (#163) will compute the
// implicit class at drift time if needed.
//
// ACL allow-list (issue #159, ADR-0007 §ACL): cluster, name, kind, capacity,
// access_modes, reclaim_policy, storage_class, phase, emitter. Labels and
// annotations are NOT included.
package entity

import (
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// PersistentVolumeToEvent maps a corev1.PersistentVolume into an Event.
func PersistentVolumeToEvent(pv *corev1.PersistentVolume, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	externalID := externalIDForClusterScoped(clusterID, "PersistentVolume", pv.Name)
	uri := "kube://" + clusterName + "/PersistentVolume/" + pv.Name

	meta := map[string]string{
		"cluster":    clusterName,
		"cluster_id": clusterID.String(),
		"name":       pv.Name,
		"kind":       "PersistentVolume",
		"phase":      string(pv.Status.Phase),
		"emitter":    "k8s-operator",
	}

	// capacity: the canonical value lives under ResourceStorage. Stringify
	// rather than serialise the resource.Quantity object so the consumer
	// doesn't need to depend on apimachinery to parse metadata. The
	// resource.Quantity String() form ("10Gi") is the same form a human
	// reads in `kubectl describe pv`.
	if storage, ok := pv.Spec.Capacity[corev1.ResourceStorage]; ok {
		meta["capacity"] = storage.String()
	}

	// access_modes: sorted, comma-joined for stable serialisation. Sorted
	// because K8s preserves slice order from the user-applied YAML and we
	// don't want metadata churn from a YAML re-order.
	if len(pv.Spec.AccessModes) > 0 {
		modes := make([]string, 0, len(pv.Spec.AccessModes))
		for _, m := range pv.Spec.AccessModes {
			modes = append(modes, string(m))
		}
		sort.Strings(modes)
		meta["access_modes"] = strings.Join(modes, ",")
	}

	if pv.Spec.PersistentVolumeReclaimPolicy != "" {
		meta["reclaim_policy"] = string(pv.Spec.PersistentVolumeReclaimPolicy)
	}

	if pv.Spec.StorageClassName != "" {
		meta["storage_class"] = pv.Spec.StorageClassName
	}

	edges := []EdgeRef{inClusterEdge(clusterID, clusterName)}

	// OF_CLASS edge — only emitted when storageClassName is set. A blank
	// string represents the deprecated implicit-default case; we don't
	// guess the default class here because that's a cluster-state read
	// and would couple this pure mapper to a live API client.
	if pv.Spec.StorageClassName != "" {
		edges = append(edges, EdgeRef{Kind: RelationOfClass, TargetExternalID: externalIDForClusterScoped(clusterID, "StorageClass", pv.Spec.StorageClassName)})
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
		TopologyEdges:       edges,
	}
}
