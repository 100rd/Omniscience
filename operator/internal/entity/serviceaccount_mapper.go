// serviceaccount_mapper.go: corev1.ServiceAccount -> Event mapper (issue #206).
//
// ServiceAccount represents a workload identity in a namespace. The mapper
// emits ONLY the closed allow-list of fields from baseMetadata plus a small
// shape describing which Secrets and image pull Secrets the SA references
// (by NAME ONLY — no values, ever) and the automount setting.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through. The
//     ACL invariant (ADR-0007 §ACL) is that workspace_id flows from operator
//     config; nothing on the watched resource influences tenancy.
//
//   - `namespace` appears in baseMetadata + external_id ONLY. It is a graph-
//     linking field and MUST NOT become a metric label dimension. The
//     corresponding metric (RecordEmit) is wired in the controller and uses
//     kind only — see #198/#201.
//
//   - Secret references are emitted as NAMES ONLY. Secret values are never
//     touched here — the operator's Secret watcher (#158, redacted-by-default
//     per #166) is the canonical Secret-content emitter and lives in
//     secret_mapper.go.
package entity

import (
	"encoding/json"
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindServiceAccount is the K8s kind segment in ServiceAccount external_ids.
const EntityKindServiceAccount = "ServiceAccount"

// ServiceAccountToEvent maps a corev1.ServiceAccount plus an action into an
// Event. Pure function — no I/O, no clients, no clock beyond `now`.
//
// Emitted metadata:
//
//   - Base allow-list: cluster, cluster_id, namespace, name, kind, emitter
//   - "secrets_count":                  decimal string of len(.Secrets)
//   - "secrets_json":                   sorted JSON array of Secret NAMES.
//                                       Absent when .Secrets is empty.
//   - "image_pull_secrets_count":       decimal string of len(.ImagePullSecrets)
//   - "image_pull_secrets_json":        sorted JSON array of Secret NAMES.
//                                       Absent when .ImagePullSecrets is empty.
//   - "automount_service_account_token": "true" / "false" / "" (when nil)
func ServiceAccountToEvent(sa *corev1.ServiceAccount, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(sa.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindServiceAccount, sa.Name)

	meta["secrets_count"] = strconv.Itoa(len(sa.Secrets))
	if len(sa.Secrets) > 0 {
		raw, err := marshalObjectRefNames(sa.Secrets)
		if err != nil {
			meta["secrets_json"] = ""
		} else {
			meta["secrets_json"] = raw
		}
	}

	meta["image_pull_secrets_count"] = strconv.Itoa(len(sa.ImagePullSecrets))
	if len(sa.ImagePullSecrets) > 0 {
		raw, err := marshalLocalObjectRefNames(sa.ImagePullSecrets)
		if err != nil {
			meta["image_pull_secrets_json"] = ""
		} else {
			meta["image_pull_secrets_json"] = raw
		}
	}

	meta["automount_service_account_token"] = automountValue(sa.AutomountServiceAccountToken)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindServiceAccount, namespace, sa.Name),
		URI:         uriFor(clusterName, namespace, EntityKindServiceAccount, sa.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// marshalObjectRefNames renders []ObjectReference as a sorted JSON array of
// the .Name fields only. Other fields (UID, ResourceVersion, etc.) are
// intentionally dropped — they're high-cardinality and not graph-relevant.
func marshalObjectRefNames(refs []corev1.ObjectReference) (string, error) {
	names := make([]string, 0, len(refs))
	for _, r := range refs {
		names = append(names, r.Name)
	}
	sort.Strings(names)
	b, err := json.Marshal(names)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// marshalLocalObjectRefNames is the LocalObjectReference variant.
// LocalObjectReference has only a single Name field, but we keep the helper
// separate so the caller's intent (image pull secrets vs other refs) is
// type-checked at the call site.
func marshalLocalObjectRefNames(refs []corev1.LocalObjectReference) (string, error) {
	names := make([]string, 0, len(refs))
	for _, r := range refs {
		names = append(names, r.Name)
	}
	sort.Strings(names)
	b, err := json.Marshal(names)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// automountValue renders *bool as "true" / "false" / "" (nil). String form
// keeps the metadata schema flat (all values are strings); the empty string
// distinguishes "explicitly nil — defer to namespace policy" from the two
// concrete settings.
func automountValue(b *bool) string {
	if b == nil {
		return ""
	}
	if *b {
		return "true"
	}
	return "false"
}
