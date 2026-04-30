// secret.go: corev1.Secret -> Event mapper.
//
// HARD RULE — METADATA ONLY. NO EXCEPTIONS.
//
// This mapper emits ONLY the closed allow-list of metadata fields. Secret
// VALUES (`data`, `stringData`, base64 blobs, the bytes themselves) MUST NOT
// flow into the Event under any circumstance. There is no opt-in like
// ConfigMap has — issue #158 §ACL invariant is explicit on this point: the
// security floor is the closed allow-list, full stop.
//
// Why values are forbidden, even with opt-in:
//   - Secrets routinely carry credentials (database passwords, API tokens,
//     TLS private keys). Indexing them puts every downstream component
//     (graph, vector store, retrieval API, audit logs) in the credential
//     blast radius.
//   - The operator runs with cluster-scoped get/list/watch on `secrets`. The
//     RBAC permission is unavoidable for the watcher; the cap on what we
//     emit is the actual control. Removing that cap would defeat the whole
//     security posture.
//   - K8s itself protects Secret values via etcd encryption-at-rest. Pulling
//     them through Omniscience would route them into stores that may have
//     different at-rest guarantees.
//
// Tests in secret_test.go assert this hard rule structurally: for any
// fixture Secret carrying values, the JSON-marshalled Event payload contains
// none of the value bytes (substring search). The test fails if a single
// byte of a value leaks anywhere in the payload.
//
// See also: docs/connectors/k8s-operator-secrets.md.
package entity

import (
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindSecret is the K8s kind segment in Secret external_ids.
const EntityKindSecret = "Secret"

// SecretToEvent maps a corev1.Secret plus an action into an Event.
//
// The function is pure and *intentionally narrow*. The only Secret fields
// read are:
//   - ObjectMeta.Namespace, ObjectMeta.Name
//   - Type
//   - the *keys* of Data and StringData (lengths, names — never values)
//
// Anything not enumerated above is dropped on the floor. This is a defence-
// in-depth design: even if a future contributor added a leaky line, the
// closed allow-list of metadata makes the regression visible to the
// structural test in secret_test.go.
func SecretToEvent(s *corev1.Secret, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(s.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindSecret, s.Name)
	meta["secret_type"] = string(s.Type)

	// Union of the keys in Data and StringData (k8s permits both). Sorted so
	// the metadata is deterministic. Values are NOT touched.
	keys := mergedSortedSecretKeys(s.Data, s.StringData)
	meta["data_keys"] = strings.Join(keys, ",")
	meta["data_count"] = strconv.Itoa(len(keys))

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindSecret, namespace, s.Name),
		URI:         uriFor(clusterName, namespace, EntityKindSecret, s.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
		// Edges are intentionally nil. Pod->Secret CONSUMES edges live on
		// the Pod controller side and are issue #157 / Wave-3 follow-up
		// territory.
	}
}

// mergedSortedSecretKeys returns the sorted union of keys in `data` and
// `stringData`. Tests assert that this function NEVER reads a value byte.
func mergedSortedSecretKeys(data map[string][]byte, stringData map[string]string) []string {
	seen := make(map[string]struct{}, len(data)+len(stringData))
	for k := range data {
		seen[k] = struct{}{}
	}
	for k := range stringData {
		seen[k] = struct{}{}
	}
	return sortedKeys(seen)
}
