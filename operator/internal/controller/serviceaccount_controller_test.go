// serviceaccount_controller_test.go: tests for ServiceAccountReconciler (#206).
package controller_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/Omniscience/operator/internal/controller"
	"github.com/100rd/Omniscience/operator/internal/entity"
)

// ─── Constructor preconditions ─────────────────────────────────────────────

func TestNewServiceAccountReconciler_RejectsZeroWorkspace(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewServiceAccountReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
}

func TestNewServiceAccountReconciler_RejectsNilPublisher(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewServiceAccountReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
}

func TestNewServiceAccountReconciler_RejectsEmptyClusterName(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewServiceAccountReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
}

func TestNewServiceAccountReconciler_RejectsNilClient(t *testing.T) {
	if _, err := controller.NewServiceAccountReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

// ─── Lifecycle: create / update / delete ───────────────────────────────────

func saFixture(ns, name string) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Secrets: []corev1.ObjectReference{
			{Name: "build-bot-token-abc"},
		},
	}
}

func TestServiceAccountReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	sa := saFixture("team-a", "build-bot")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(sa).Build()
	pub := &fakePublisher{}

	r, err := controller.NewServiceAccountReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "build-bot")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	ev := got[0]
	if ev.WorkspaceID != tcWorkspace {
		t.Fatalf("workspace_id = %s", ev.WorkspaceID)
	}
	if ev.Action != entity.ActionUpdated {
		t.Fatalf("action = %q", ev.Action)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ServiceAccount/team-a/build-bot" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["secrets_count"] != "1" {
		t.Fatalf("secrets_count = %q", ev.Metadata["secrets_count"])
	}
	if !ev.EmittedAt.Equal(tcFixedTime) {
		t.Fatalf("emitted_at = %s", ev.EmittedAt)
	}
}

// Update path: spec change between reconciles is reflected in the second event.
func TestServiceAccountReconciler_UpdateEmitsSecondUpdated(t *testing.T) {
	s := newScheme(t)
	sa := saFixture("team-a", "build-bot")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(sa).Build()
	pub := &fakePublisher{}

	r, err := controller.NewServiceAccountReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req("team-a", "build-bot")); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	var live corev1.ServiceAccount
	if err := c.Get(ctx, req("team-a", "build-bot").NamespacedName, &live); err != nil {
		t.Fatalf("get: %v", err)
	}
	live.ImagePullSecrets = []corev1.LocalObjectReference{{Name: "ghcr-creds"}}
	if err := c.Update(ctx, &live); err != nil {
		t.Fatalf("update: %v", err)
	}

	if _, err := r.Reconcile(ctx, req("team-a", "build-bot")); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 2 {
		t.Fatalf("publisher saw %d events, want 2", len(got))
	}
	if got[0].Metadata["image_pull_secrets_count"] != "0" {
		t.Fatalf("first image_pull_secrets_count = %q, want 0", got[0].Metadata["image_pull_secrets_count"])
	}
	if got[1].Metadata["image_pull_secrets_count"] != "1" {
		t.Fatalf("second image_pull_secrets_count = %q, want 1", got[1].Metadata["image_pull_secrets_count"])
	}
}

// Delete path: empty client → IsNotFound → Deleted event for stub.
func TestServiceAccountReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}

	r, err := controller.NewServiceAccountReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "build-bot")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	if got[0].Action != entity.ActionDeleted {
		t.Fatalf("action = %q", got[0].Action)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ServiceAccount/team-a/build-bot" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
	if got[0].Metadata["secrets_count"] != "0" {
		t.Fatalf("stub deletion should have empty secrets; secrets_count = %q", got[0].Metadata["secrets_count"])
	}
}
