// limitrange_mapper.go: corev1.LimitRange -> Event mapper (issue #202).
//
// LimitRange holds per-namespace defaults / min / max for CPU, memory,
// storage, and ephemeral-storage on Container, Pod, PersistentVolumeClaim,
// and similar object types. The mapper emits ONLY the closed allow-list of
// fields from baseMetadata (cluster, cluster_id, namespace, name, kind,
// emitter) plus a kind-specific limits payload.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through. The
//     ACL invariant (ADR-0007 §ACL) is that workspace_id flows from operator
//     config; nothing on the watched resource influences tenancy.
//
//   - `namespace` appears in baseMetadata + external_id ONLY. It is a graph-
//     linking field and MUST NOT become a metric label dimension (high
//     cardinality + tenancy leak). The corresponding metric (RecordEmit) is
//     wired in the controller, not here, and uses kind only — see #198/#201.
//
//   - LimitRange spec contains operational quantities (CPU/memory/storage
//     limits expressed as resource.Quantity strings). These are not secrets,
//     but the mapper still does NOT touch annotations / labels.
package entity

import (
	"encoding/json"
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindLimitRange is the K8s kind segment in LimitRange external_ids.
const EntityKindLimitRange = "LimitRange"

// LimitRangeToEvent maps a corev1.LimitRange plus an action into an Event.
// Pure function — no I/O, no clients, no clock beyond `now`.
//
// The emitted metadata is:
//
//   - Base allow-list: cluster, cluster_id, namespace, name, kind, emitter
//   - "limits_count": decimal string of len(spec.limits)
//   - "limits_json":  deterministic JSON encoding of spec.limits (see
//     marshalLimits below). Empty / absent when spec.limits is empty so the
//     consumer can distinguish "no limits configured" from "marshal failed".
//
// The JSON encoding sorts limit entries by `type` and resource keys within
// each map lexicographically; this keeps event payloads byte-identical
// across operator restarts and Go's non-deterministic map iteration order.
func LimitRangeToEvent(lr *corev1.LimitRange, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(lr.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindLimitRange, lr.Name)

	meta["limits_count"] = strconv.Itoa(len(lr.Spec.Limits))
	if len(lr.Spec.Limits) > 0 {
		raw, err := marshalLimits(lr.Spec.Limits)
		if err != nil {
			// resource.Quantity has a well-defined String() form and the
			// shape we marshal is map[string]string — encoding/json cannot
			// fail in practice. Emit empty marker rather than partial JSON
			// so the consumer can tell something went wrong.
			meta["limits_json"] = ""
		} else {
			meta["limits_json"] = raw
		}
	}

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindLimitRange, namespace, lr.Name),
		URI:         uriFor(clusterName, namespace, EntityKindLimitRange, lr.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// limitItemView is the deterministic JSON shape we emit for one
// spec.limits[] entry. Resource maps are flattened to map[string]string with
// the resource.Quantity rendered via its canonical .String() form (which is
// what kubectl prints — e.g. "500m", "1Gi"). encoding/json sorts string-
// keyed map keys since Go 1.12, so the output is byte-stable.
type limitItemView struct {
	Type                 string            `json:"type"`
	Default              map[string]string `json:"default,omitempty"`
	DefaultRequest       map[string]string `json:"defaultRequest,omitempty"`
	Min                  map[string]string `json:"min,omitempty"`
	Max                  map[string]string `json:"max,omitempty"`
	MaxLimitRequestRatio map[string]string `json:"maxLimitRequestRatio,omitempty"`
}

// marshalLimits renders spec.limits as deterministic JSON. Entries are
// sorted by .Type so two operators observing the same LimitRange emit the
// same bytes regardless of map iteration order in the source object.
func marshalLimits(items []corev1.LimitRangeItem) (string, error) {
	views := make([]limitItemView, 0, len(items))
	for _, it := range items {
		views = append(views, limitItemView{
			Type:                 string(it.Type),
			Default:              quantityMap(it.Default),
			DefaultRequest:       quantityMap(it.DefaultRequest),
			Min:                  quantityMap(it.Min),
			Max:                  quantityMap(it.Max),
			MaxLimitRequestRatio: quantityMap(it.MaxLimitRequestRatio),
		})
	}
	sort.Slice(views, func(i, j int) bool { return views[i].Type < views[j].Type })

	b, err := json.Marshal(views)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// quantityMap converts a corev1.ResourceList (map[ResourceName]Quantity) into
// a string-keyed map suitable for deterministic JSON. Returns nil for empty
// inputs so the caller's omitempty tag suppresses the field — keeps the JSON
// compact and the diff easy to read.
func quantityMap(rl corev1.ResourceList) map[string]string {
	if len(rl) == 0 {
		return nil
	}
	out := make(map[string]string, len(rl))
	for k, v := range rl {
		out[string(k)] = v.String()
	}
	return out
}
