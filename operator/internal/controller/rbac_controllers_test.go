// rbac_controllers_test.go: tests for the four RBAC controllers (#212).
//
// Single test file covering all four reconcilers since the shape is
// identical — only the kind, mapper, and metric label change. Each
// reconciler gets a constructor-precondition pass, a create-emits-Updated
// case, and a delete-emits-Deleted case. Update lifecycle is covered by
// the per-mapper unit tests (mapper is the part that varies; controller
// shape is invariant across kinds).
package controller_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/Omniscience/operator/internal/controller"
	"github.com/100rd/Omniscience/operator/internal/entity"
)

// ─── Role ─────────────────────────────────────────────────────────────────

func TestNewRoleReconciler_Preconditions(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewRoleReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
	if _, err := controller.NewRoleReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
	if _, err := controller.NewRoleReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
	if _, err := controller.NewRoleReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

func TestRoleReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	r := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "viewer"},
		Rules: []rbacv1.PolicyRule{
			{Verbs: []string{"get", "list"}, APIGroups: []string{""}, Resources: []string{"pods"}},
		},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(r).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewRoleReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("team-a", "viewer")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionUpdated {
		t.Fatalf("got %#v", got)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Role/team-a/viewer" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
	if got[0].Metadata["rules_count"] != "1" {
		t.Fatalf("rules_count = %q", got[0].Metadata["rules_count"])
	}
}

func TestRoleReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewRoleReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("team-a", "viewer")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionDeleted {
		t.Fatalf("got %#v", got)
	}
}

// ─── RoleBinding ──────────────────────────────────────────────────────────

func TestNewRoleBindingReconciler_Preconditions(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewRoleBindingReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
	if _, err := controller.NewRoleBindingReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
	if _, err := controller.NewRoleBindingReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
	if _, err := controller.NewRoleBindingReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

func TestRoleBindingReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	rb := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "viewers"},
		RoleRef:    rbacv1.RoleRef{Kind: "Role", Name: "viewer", APIGroup: "rbac.authorization.k8s.io"},
		Subjects: []rbacv1.Subject{
			{Kind: "User", Name: "alice@example.com", APIGroup: "rbac.authorization.k8s.io"},
		},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(rb).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewRoleBindingReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("team-a", "viewers")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionUpdated {
		t.Fatalf("got %#v", got)
	}
	if got[0].Metadata["role_ref_name"] != "viewer" {
		t.Fatalf("role_ref_name = %q", got[0].Metadata["role_ref_name"])
	}
	if got[0].Metadata["subjects_count"] != "1" {
		t.Fatalf("subjects_count = %q", got[0].Metadata["subjects_count"])
	}
}

func TestRoleBindingReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewRoleBindingReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("team-a", "viewers")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionDeleted {
		t.Fatalf("got %#v", got)
	}
}

// ─── ClusterRole ──────────────────────────────────────────────────────────

func TestNewClusterRoleReconciler_Preconditions(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewClusterRoleReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
	if _, err := controller.NewClusterRoleReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
	if _, err := controller.NewClusterRoleReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
	if _, err := controller.NewClusterRoleReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

func TestClusterRoleReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	cr := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{Name: "cluster-admin"},
		Rules: []rbacv1.PolicyRule{
			{Verbs: []string{"*"}, APIGroups: []string{"*"}, Resources: []string{"*"}},
		},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(cr).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewClusterRoleReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("", "cluster-admin")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionUpdated {
		t.Fatalf("got %#v", got)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ClusterRole/default/cluster-admin" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
}

func TestClusterRoleReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewClusterRoleReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("", "cluster-admin")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionDeleted {
		t.Fatalf("got %#v", got)
	}
}

// ─── ClusterRoleBinding ───────────────────────────────────────────────────

func TestNewClusterRoleBindingReconciler_Preconditions(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewClusterRoleBindingReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
	if _, err := controller.NewClusterRoleBindingReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
	if _, err := controller.NewClusterRoleBindingReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
	if _, err := controller.NewClusterRoleBindingReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

func TestClusterRoleBindingReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	crb := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "cluster-admin-binding"},
		RoleRef:    rbacv1.RoleRef{Kind: "ClusterRole", Name: "cluster-admin", APIGroup: "rbac.authorization.k8s.io"},
		Subjects: []rbacv1.Subject{
			{Kind: "User", Name: "admin@example.com", APIGroup: "rbac.authorization.k8s.io"},
		},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(crb).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewClusterRoleBindingReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("", "cluster-admin-binding")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionUpdated {
		t.Fatalf("got %#v", got)
	}
	if got[0].Metadata["role_ref_kind"] != "ClusterRole" {
		t.Fatalf("role_ref_kind = %q", got[0].Metadata["role_ref_kind"])
	}
}

func TestClusterRoleBindingReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}
	rec, err := controller.NewClusterRoleBindingReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	rec.Now = fixedNowFunc()
	if _, err := rec.Reconcile(context.Background(), req("", "cluster-admin-binding")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionDeleted {
		t.Fatalf("got %#v", got)
	}
}
