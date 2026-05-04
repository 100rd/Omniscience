// rolebinding_mapper_test.go: pure unit tests for RoleBindingToEvent + ClusterRoleBindingToEvent (#212).
package entity_test

import (
	"encoding/json"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

// reuse rTestWorkspace / rTestClusterID / rTestClusterName / rTestNow from role_mapper_test.go

// ─── RoleBinding envelope + ACL ───────────────────────────────────────────

func TestRoleBindingToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	rb := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "viewers",
			Labels:    map[string]string{"omniscience.io/workspace": "evil-ws"},
		},
		RoleRef: rbacv1.RoleRef{
			Kind:     "Role",
			Name:     "viewer",
			APIGroup: "rbac.authorization.k8s.io",
		},
	}
	ev := entity.RoleBindingToEvent(rb, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	if ev.WorkspaceID != rTestWorkspace {
		t.Fatalf("workspace_id = %s — adversarial label honoured!", ev.WorkspaceID)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/RoleBinding/team-a/viewers" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["role_ref_kind"] != "Role" || ev.Metadata["role_ref_name"] != "viewer" {
		t.Fatalf("role_ref = %q/%q", ev.Metadata["role_ref_kind"], ev.Metadata["role_ref_name"])
	}
}

func TestRoleBindingToEvent_ACL_NoUserLabelsLeak(t *testing.T) {
	rb := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:   "team-a",
			Name:        "viewers",
			Labels:      map[string]string{"team": "platform"},
			Annotations: map[string]string{"description": "view team resources"},
		},
		RoleRef: rbacv1.RoleRef{Kind: "Role", Name: "viewer", APIGroup: "rbac.authorization.k8s.io"},
	}
	ev := entity.RoleBindingToEvent(rb, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"role_ref_kind": {}, "role_ref_name": {}, "role_ref_api_group": {},
		"subjects_count": {}, "subjects_json": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q)", k, ev.Metadata[k])
		}
	}
}

// ─── RoleBinding subjects: multi-subject sorted by (kind, namespace, name) ──

func TestRoleBindingToEvent_MultipleSubjects(t *testing.T) {
	rb := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "viewers"},
		RoleRef:    rbacv1.RoleRef{Kind: "Role", Name: "viewer", APIGroup: "rbac.authorization.k8s.io"},
		Subjects: []rbacv1.Subject{
			// Intentionally unsorted — mapper must sort by (kind, namespace, name).
			{Kind: "User", Name: "bob@example.com", APIGroup: "rbac.authorization.k8s.io"},
			{Kind: "ServiceAccount", Name: "default", Namespace: "team-a"},
			{Kind: "Group", Name: "platform-eng", APIGroup: "rbac.authorization.k8s.io"},
			{Kind: "User", Name: "alice@example.com", APIGroup: "rbac.authorization.k8s.io"},
		},
	}
	ev := entity.RoleBindingToEvent(rb, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	if ev.Metadata["subjects_count"] != "4" {
		t.Fatalf("subjects_count = %q", ev.Metadata["subjects_count"])
	}

	var got []map[string]any
	if err := json.Unmarshal([]byte(ev.Metadata["subjects_json"]), &got); err != nil {
		t.Fatalf("subjects_json not valid JSON: %v", err)
	}
	if len(got) != 4 {
		t.Fatalf("got %d subjects, want 4", len(got))
	}
	// Sorted by kind first: Group, ServiceAccount, User, User
	if got[0]["kind"] != "Group" {
		t.Fatalf("first subject kind = %q, want Group", got[0]["kind"])
	}
	if got[1]["kind"] != "ServiceAccount" {
		t.Fatalf("second subject kind = %q, want ServiceAccount", got[1]["kind"])
	}
	// The two Users sorted by name: alice before bob.
	if got[2]["kind"] != "User" || got[2]["name"] != "alice@example.com" {
		t.Fatalf("third subject = %#v", got[2])
	}
	if got[3]["kind"] != "User" || got[3]["name"] != "bob@example.com" {
		t.Fatalf("fourth subject = %#v", got[3])
	}
}

func TestRoleBindingToEvent_DeterministicJSON(t *testing.T) {
	build := func() *rbacv1.RoleBinding {
		return &rbacv1.RoleBinding{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
			RoleRef:    rbacv1.RoleRef{Kind: "Role", Name: "viewer"},
			Subjects: []rbacv1.Subject{
				{Kind: "User", Name: "z@example.com"},
				{Kind: "User", Name: "a@example.com"},
				{Kind: "Group", Name: "ops"},
			},
		}
	}
	a := entity.RoleBindingToEvent(build(), entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	b := entity.RoleBindingToEvent(build(), entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	if a.Metadata["subjects_json"] != b.Metadata["subjects_json"] {
		t.Fatalf("non-deterministic subjects_json:\nA: %s\nB: %s", a.Metadata["subjects_json"], b.Metadata["subjects_json"])
	}
}

func TestRoleBindingToEvent_EmptySubjects(t *testing.T) {
	rb := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "empty"},
		RoleRef:    rbacv1.RoleRef{Kind: "Role", Name: "x"},
	}
	ev := entity.RoleBindingToEvent(rb, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	if ev.Metadata["subjects_count"] != "0" {
		t.Fatalf("subjects_count = %q", ev.Metadata["subjects_count"])
	}
	if _, present := ev.Metadata["subjects_json"]; present {
		t.Fatalf("subjects_json must be absent when subjects empty")
	}
}

// ─── ClusterRoleBinding ───────────────────────────────────────────────────

func TestClusterRoleBindingToEvent_NoNamespace(t *testing.T) {
	crb := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "cluster-admin-binding"},
		RoleRef:    rbacv1.RoleRef{Kind: "ClusterRole", Name: "cluster-admin", APIGroup: "rbac.authorization.k8s.io"},
		Subjects: []rbacv1.Subject{
			{Kind: "User", Name: "admin@example.com", APIGroup: "rbac.authorization.k8s.io"},
		},
	}
	ev := entity.ClusterRoleBindingToEvent(crb, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	if ev.Metadata["kind"] != "ClusterRoleBinding" {
		t.Fatalf("kind = %q", ev.Metadata["kind"])
	}
	if ev.Metadata["namespace"] != "default" {
		t.Fatalf("namespace = %q (resolveNamespace fallback)", ev.Metadata["namespace"])
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ClusterRoleBinding/default/cluster-admin-binding" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["role_ref_kind"] != "ClusterRole" {
		t.Fatalf("role_ref_kind = %q", ev.Metadata["role_ref_kind"])
	}
}

// ─── Default namespace fallback (RoleBinding with empty namespace) ────────

func TestRoleBindingToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	rb := &rbacv1.RoleBinding{
		RoleRef: rbacv1.RoleRef{Kind: "Role", Name: "x"},
	}
	rb.Name = "x"
	ev := entity.RoleBindingToEvent(rb, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/RoleBinding/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
}
