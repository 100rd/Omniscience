// rolebinding_mapper.go: rbacv1.RoleBinding + rbacv1.ClusterRoleBinding -> Event mappers (issue #212).
//
// Both kinds carry a RoleRef and []Subject. The mapper renders both as
// deterministic JSON. ClusterRoleBinding is the cluster-scoped counterpart
// — namespace is empty in baseMetadata for that kind.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through.
//   - `namespace` appears in baseMetadata + external_id ONLY (RoleBinding).
//   - Subject names (e.g. ServiceAccount names, User emails, Group names)
//     are emitted as-is in the JSON payload. They are not metric labels.
//     If a future requirement to redact email-shaped User names emerges,
//     it should hash User subjects at this boundary.
package entity

import (
	"encoding/json"
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	rbacv1 "k8s.io/api/rbac/v1"
)

// EntityKindRoleBinding / EntityKindClusterRoleBinding are the K8s kind segments.
const (
	EntityKindRoleBinding        = "RoleBinding"
	EntityKindClusterRoleBinding = "ClusterRoleBinding"
)

// RoleBindingToEvent maps a rbacv1.RoleBinding plus an action into an Event.
func RoleBindingToEvent(rb *rbacv1.RoleBinding, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(rb.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindRoleBinding, rb.Name)
	addRoleRefMetadata(meta, rb.RoleRef)
	addSubjectsMetadata(meta, rb.Subjects)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindRoleBinding, namespace, rb.Name),
		URI:         uriFor(clusterName, namespace, EntityKindRoleBinding, rb.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// ClusterRoleBindingToEvent maps a rbacv1.ClusterRoleBinding plus an action
// into an Event. Cluster-scoped — namespace is empty (resolveNamespace
// returns "default" for graph well-formedness; consumers distinguish via kind).
func ClusterRoleBindingToEvent(crb *rbacv1.ClusterRoleBinding, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace("") // ClusterRoleBinding has no namespace
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindClusterRoleBinding, crb.Name)
	addRoleRefMetadata(meta, crb.RoleRef)
	addSubjectsMetadata(meta, crb.Subjects)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindClusterRoleBinding, namespace, crb.Name),
		URI:         uriFor(clusterName, namespace, EntityKindClusterRoleBinding, crb.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// addRoleRefMetadata writes role_ref_kind + role_ref_name + role_ref_api_group
// into the supplied metadata map. RoleRef is always present (it's a required
// field in the Kubernetes API), so no count or omitempty is needed.
func addRoleRefMetadata(meta map[string]string, ref rbacv1.RoleRef) {
	meta["role_ref_kind"] = ref.Kind
	meta["role_ref_name"] = ref.Name
	meta["role_ref_api_group"] = ref.APIGroup
}

// addSubjectsMetadata writes subjects_count + subjects_json (when non-empty)
// into the supplied metadata map.
func addSubjectsMetadata(meta map[string]string, subjects []rbacv1.Subject) {
	meta["subjects_count"] = strconv.Itoa(len(subjects))
	if len(subjects) > 0 {
		raw, err := marshalSubjects(subjects)
		if err != nil {
			meta["subjects_json"] = ""
		} else {
			meta["subjects_json"] = raw
		}
	}
}

// subjectView is the deterministic JSON shape for one Subject. Optional
// fields use omitempty.
type subjectView struct {
	Kind      string `json:"kind"`
	Name      string `json:"name"`
	Namespace string `json:"namespace,omitempty"`
	APIGroup  string `json:"apiGroup,omitempty"`
}

// marshalSubjects renders []Subject as deterministic JSON, sorted by
// (kind, namespace, name) tuple.
func marshalSubjects(subjects []rbacv1.Subject) (string, error) {
	views := make([]subjectView, 0, len(subjects))
	for _, s := range subjects {
		views = append(views, subjectView{
			Kind:      s.Kind,
			Name:      s.Name,
			Namespace: s.Namespace,
			APIGroup:  s.APIGroup,
		})
	}
	sort.Slice(views, func(i, j int) bool {
		if views[i].Kind != views[j].Kind {
			return views[i].Kind < views[j].Kind
		}
		if views[i].Namespace != views[j].Namespace {
			return views[i].Namespace < views[j].Namespace
		}
		return views[i].Name < views[j].Name
	})

	b, err := json.Marshal(views)
	if err != nil {
		return "", err
	}
	return string(b), nil
}
