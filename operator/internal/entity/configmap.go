// configmap.go: corev1.ConfigMap -> Event mapper.
//
// SECURITY POSTURE — metadata only by default.
//
// ConfigMaps frequently carry application configuration that is *not*
// sensitive in name only — connection strings, internal hostnames, feature
// flags, etc. Treating them as opaque means the safe default is to emit only
// the closed allow-list of metadata fields, NEVER the values.
//
// Opt-in for value emission is per-ConfigMap and namespace-scoped: the
// ConfigMap author sets metadata.annotations["omniscience.io/index-data"] =
// "true". This is documented in docs/connectors/k8s-operator-secrets.md.
//
// The emitted fields when the opt-in is absent (default):
//   - cluster, namespace, name, kind, emitter (base)
//   - data_keys: sorted comma-joined list of *names* in spec.data and
//     spec.binaryData (NEVER values — same posture as Secret).
//   - data_count: total count as a string.
//   - indexed: "false"
//
// When the opt-in is present:
//   - everything above, but `indexed` becomes "true" and an additional
//     metadata key `data_values_json` carries the values (as JSON-encoded
//     map). binaryData values are NOT emitted even with opt-in — they are
//     opaque blobs and almost always not useful for retrieval.
//
// Issue #158 explicitly forbids data values from leaking via Secret. The same
// hard rule applies to ConfigMap unless explicitly opted in per-resource.
package entity

import (
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindConfigMap is the K8s kind segment in ConfigMap external_ids.
const EntityKindConfigMap = "ConfigMap"

// IndexDataAnnotation is the per-resource opt-in annotation that allows the
// ConfigMap mapper to emit `data` values. Default is metadata-only.
const IndexDataAnnotation = "omniscience.io/index-data"

// ConfigMapToEvent maps a corev1.ConfigMap plus an action into an Event.
// See file header for the security posture and the opt-in mechanism.
//
// This function is pure and never reads values out of `binaryData`. The
// `data_values_json` field is only populated when the opt-in annotation is
// present AND `data` (the textual variant) is non-empty.
func ConfigMapToEvent(cm *corev1.ConfigMap, action Action, workspaceID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(cm.Namespace)
	meta := baseMetadata(clusterName, namespace, EntityKindConfigMap, cm.Name)

	keys := mergedSortedKeys(cm.Data, cm.BinaryData)
	meta["data_keys"] = strings.Join(keys, ",")
	meta["data_count"] = strconv.Itoa(len(keys))

	indexed := isIndexDataOptIn(cm.Annotations)
	meta["indexed"] = strconv.FormatBool(indexed)
	if indexed && len(cm.Data) > 0 {
		// Marshal a sorted-key view so the JSON is deterministic. Iteration
		// order of map[string]string is non-deterministic in Go.
		raw, err := marshalSortedStringMap(cm.Data)
		if err != nil {
			// Marshal of a map[string]string can't fail in practice, but if
			// it ever did we'd rather NOT emit anything than risk partial
			// or corrupted JSON. Emit empty marker so the consumer can tell.
			meta["data_values_json"] = ""
		} else {
			meta["data_values_json"] = raw
		}
	}

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(EntityKindConfigMap, namespace, cm.Name),
		URI:         uriFor(clusterName, namespace, EntityKindConfigMap, cm.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// isIndexDataOptIn returns true iff annotations contains the IndexDataAnnotation
// set to the literal string "true". Any other value (including "True", "1",
// "yes") is treated as a no-opt to keep the gate conservative.
func isIndexDataOptIn(annotations map[string]string) bool {
	v, ok := annotations[IndexDataAnnotation]
	return ok && v == "true"
}

// mergedSortedKeys returns the union of keys in `data` and `binaryData`,
// sorted lexicographically. The result is the *names* only — values are never
// touched.
func mergedSortedKeys(data map[string]string, binary map[string][]byte) []string {
	seen := make(map[string]struct{}, len(data)+len(binary))
	for k := range data {
		seen[k] = struct{}{}
	}
	for k := range binary {
		seen[k] = struct{}{}
	}
	return sortedKeys(seen)
}

// marshalSortedStringMap returns the JSON encoding of m with keys in sorted
// order. encoding/json's Encoder sorts map keys automatically since Go 1.12,
// so we just hand it the map; this wrapper exists only to centralise the
// guarantee.
func marshalSortedStringMap(m map[string]string) (string, error) {
	b, err := json.Marshal(m)
	if err != nil {
		return "", err
	}
	return string(b), nil
}
