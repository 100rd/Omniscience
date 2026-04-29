// Package entity — StorageClass mapping.
//
// StorageClasses are cluster-scoped under storage.k8s.io/v1. The external_id
// is k8s_resource/StorageClass/{name}.
//
// ACL allow-list (issue #159, ADR-0007 §ACL): cluster, name, kind,
// provisioner, reclaim_policy, emitter. The user-supplied parameters map
// (e.g. encryption keys, throughput tiers) is intentionally NOT copied —
// it can carry secret-grade configuration in some CSI drivers and is
// tenant-writable on the cluster side.
package entity

import (
	"time"

	"github.com/google/uuid"
	storagev1 "k8s.io/api/storage/v1"
)

// StorageClassToEvent maps a storagev1.StorageClass into an Event.
func StorageClassToEvent(sc *storagev1.StorageClass, action Action, workspaceID uuid.UUID, clusterName string, now time.Time) *Event {
	externalID := EntityKindK8sResource + "/StorageClass/" + sc.Name
	uri := "kube://" + clusterName + "/StorageClass/" + sc.Name

	meta := map[string]string{
		"cluster":     clusterName,
		"name":        sc.Name,
		"kind":        "StorageClass",
		"provisioner": sc.Provisioner,
		"emitter":     "k8s-operator",
	}
	if sc.ReclaimPolicy != nil {
		meta["reclaim_policy"] = string(*sc.ReclaimPolicy)
	}

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
