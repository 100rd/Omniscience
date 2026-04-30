// Package entity — DRA DeviceClass mapping.
//
// DeviceClass (resource.k8s.io/v1beta1) is a cluster-scoped object that
// administrators create to gate which device types may be requested by a
// ResourceClaim. A class carries selectors that the scheduler evaluates
// against advertised devices on ResourceSlices, and configuration parameters
// that are passed through to the driver at allocation time.
//
// ACL allow-list (issue #162, ADR-0007 §ACL): cluster, name, kind,
// selector_count, emitter. Selector *values* are tenant-mutable cluster
// state and intentionally NOT copied through; only the count is reported,
// matching the same pattern Service uses for selectors (formatLabelSelector
// is opt-in for namespaced label selectors only).
//
// Topology edges:
//   - IN_CLUSTER -> Cluster anchor (mirrors Node, PV, StorageClass,
//     ResourceSlice).
//
// DeviceClass has no outbound topology edges in v1beta1 — drivers are
// referenced only inside DeviceConfiguration blocks (and only for
// configuration), and node selection is per-class but evaluated at
// allocation time, not at class identity time.
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	resourcev1beta1 "k8s.io/api/resource/v1beta1"
)

// DeviceClassToEvent maps a v1beta1.DeviceClass into an Event. Pure function
// — no client, no clock injection beyond `now`.
//
// DeviceClass is cluster-scoped, so the external_id has no namespace
// component: k8s_resource/DeviceClass/{name}.
func DeviceClassToEvent(dc *resourcev1beta1.DeviceClass, action Action, workspaceID uuid.UUID, clusterName string, now time.Time) *Event {
	externalID := EntityKindPrefix + "/" + EntityKindDeviceClass + "/" + dc.Name
	uri := "kube://" + clusterName + "/" + EntityKindDeviceClass + "/" + dc.Name

	meta := map[string]string{
		"cluster":        clusterName,
		"name":           dc.Name,
		"kind":           EntityKindDeviceClass,
		"selector_count": strconv.Itoa(len(dc.Spec.Selectors)),
		"emitter":        EmitterLabel,
	}

	edges := []EdgeRef{inClusterEdge(clusterName)}

	return &Event{
		SourceID:      deriveSourceID(workspaceID, clusterName),
		SourceType:    SourceType,
		ExternalID:    externalID,
		URI:           uri,
		Action:        action,
		WorkspaceID:   workspaceID,
		EmittedAt:     now.UTC(),
		Metadata:      meta,
		TopologyEdges: edges,
	}
}
