// persistentvolumeclaim_mapper.go: corev1.PersistentVolumeClaim -> Event mapper (issue #208).
//
// PersistentVolumeClaim is a namespaced storage request. The mapper emits
// ONLY the closed allow-list of fields from baseMetadata plus a flat
// representation of the spec essentials (access modes, storage class,
// bound PV name, volume mode, requests/limits storage) and the status
// phase.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through.
//   - `namespace` appears in baseMetadata + external_id ONLY.
//   - `selector` (label selector) is NEVER emitted — it would leak labels.
//   - `dataSource` / `dataSourceRef` are NEVER emitted — they reference
//     other resources (snapshots, PVCs) and are graph-linking concerns
//     better expressed via separate edges if needed.
package entity

import (
	"encoding/json"
	"sort"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindPersistentVolumeClaim is the K8s kind segment in PVC external_ids.
const EntityKindPersistentVolumeClaim = "PersistentVolumeClaim"

// PersistentVolumeClaimToEvent maps a corev1.PersistentVolumeClaim plus an
// action into an Event. Pure function — no I/O, no clients, no clock beyond
// `now`.
//
// Emitted metadata:
//
//   - Base allow-list: cluster, cluster_id, namespace, name, kind, emitter
//   - "access_modes_json":   sorted JSON array of access mode strings
//                            (e.g. ["ReadWriteOnce"]). Absent when empty.
//   - "storage_class_name":  string; empty when nil. The empty string is
//                            distinct from a literal "" storage class name
//                            (cluster-default storage class) — be aware.
//   - "volume_name":         the bound PV name; empty when unbound.
//   - "volume_mode":         "Filesystem" / "Block"; empty when nil.
//   - "requests_storage":    storage request quantity (.String()); empty
//                            when not set.
//   - "limits_storage":      storage limit quantity (.String()); empty
//                            when not set.
//   - "status_phase":        "Pending" / "Bound" / "Lost"; empty when not set.
func PersistentVolumeClaimToEvent(pvc *corev1.PersistentVolumeClaim, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(pvc.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindPersistentVolumeClaim, pvc.Name)

	if len(pvc.Spec.AccessModes) > 0 {
		raw, err := marshalAccessModes(pvc.Spec.AccessModes)
		if err != nil {
			meta["access_modes_json"] = ""
		} else {
			meta["access_modes_json"] = raw
		}
	}

	if pvc.Spec.StorageClassName != nil {
		meta["storage_class_name"] = *pvc.Spec.StorageClassName
	} else {
		meta["storage_class_name"] = ""
	}

	meta["volume_name"] = pvc.Spec.VolumeName

	if pvc.Spec.VolumeMode != nil {
		meta["volume_mode"] = string(*pvc.Spec.VolumeMode)
	} else {
		meta["volume_mode"] = ""
	}

	if q, ok := pvc.Spec.Resources.Requests[corev1.ResourceStorage]; ok {
		meta["requests_storage"] = q.String()
	} else {
		meta["requests_storage"] = ""
	}

	if q, ok := pvc.Spec.Resources.Limits[corev1.ResourceStorage]; ok {
		meta["limits_storage"] = q.String()
	} else {
		meta["limits_storage"] = ""
	}

	meta["status_phase"] = string(pvc.Status.Phase)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindPersistentVolumeClaim, namespace, pvc.Name),
		URI:         uriFor(clusterName, namespace, EntityKindPersistentVolumeClaim, pvc.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// marshalAccessModes renders []PersistentVolumeAccessMode as a sorted JSON
// array of strings. Sorting keeps the output stable across operator
// restarts.
func marshalAccessModes(modes []corev1.PersistentVolumeAccessMode) (string, error) {
	out := make([]string, 0, len(modes))
	for _, m := range modes {
		out = append(out, string(m))
	}
	sort.Strings(out)
	b, err := json.Marshal(out)
	if err != nil {
		return "", err
	}
	return string(b), nil
}
