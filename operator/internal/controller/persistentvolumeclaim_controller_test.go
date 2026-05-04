// persistentvolumeclaim_controller_test.go: tests for PersistentVolumeClaimReconciler (#208).
package controller_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/omniscience/operator/internal/controller"
	"github.com/100rd/omniscience/operator/internal/entity"
)

func pvcCtrlQty(s string) resource.Quantity { return resource.MustParse(s) }

// ─── Constructor preconditions ─────────────────────────────────────────────

func TestNewPVCReconciler_RejectsZeroWorkspace(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewPersistentVolumeClaimReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
}

func TestNewPVCReconciler_RejectsNilPublisher(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewPersistentVolumeClaimReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
}

func TestNewPVCReconciler_RejectsEmptyClusterName(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewPersistentVolumeClaimReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
}

func TestNewPVCReconciler_RejectsNilClient(t *testing.T) {
	if _, err := controller.NewPersistentVolumeClaimReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

// ─── Lifecycle: create / update / delete ───────────────────────────────────

func pvcFixture(ns, name string) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: pvcCtrlQty("10Gi")},
			},
		},
		Status: corev1.PersistentVolumeClaimStatus{Phase: corev1.ClaimPending},
	}
}

func TestPVCReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	pvc := pvcFixture("team-a", "data")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(pvc).Build()
	pub := &fakePublisher{}

	r, err := controller.NewPersistentVolumeClaimReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "data")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	ev := got[0]
	if ev.Action != entity.ActionUpdated {
		t.Fatalf("action = %q", ev.Action)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/PersistentVolumeClaim/team-a/data" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["status_phase"] != "Pending" {
		t.Fatalf("status_phase = %q", ev.Metadata["status_phase"])
	}
	if ev.Metadata["requests_storage"] != "10Gi" {
		t.Fatalf("requests_storage = %q", ev.Metadata["requests_storage"])
	}
}

// Update path: status phase change between reconciles flows through.
func TestPVCReconciler_UpdateEmitsSecondUpdated(t *testing.T) {
	s := newScheme(t)
	pvc := pvcFixture("team-a", "data")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(pvc).Build()
	pub := &fakePublisher{}

	r, err := controller.NewPersistentVolumeClaimReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req("team-a", "data")); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	var live corev1.PersistentVolumeClaim
	if err := c.Get(ctx, req("team-a", "data").NamespacedName, &live); err != nil {
		t.Fatalf("get: %v", err)
	}
	live.Status.Phase = corev1.ClaimBound
	live.Spec.VolumeName = "pv-12345"
	if err := c.Update(ctx, &live); err != nil {
		t.Fatalf("update: %v", err)
	}

	if _, err := r.Reconcile(ctx, req("team-a", "data")); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 2 {
		t.Fatalf("publisher saw %d events, want 2", len(got))
	}
	if got[0].Metadata["status_phase"] != "Pending" {
		t.Fatalf("first status_phase = %q", got[0].Metadata["status_phase"])
	}
	if got[1].Metadata["volume_name"] != "pv-12345" {
		t.Fatalf("second volume_name = %q (update should reflect bound state)", got[1].Metadata["volume_name"])
	}
}

// Delete path: empty client → IsNotFound → Deleted event for stub.
func TestPVCReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}

	r, err := controller.NewPersistentVolumeClaimReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "data")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	if got[0].Action != entity.ActionDeleted {
		t.Fatalf("action = %q", got[0].Action)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/PersistentVolumeClaim/team-a/data" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
	if got[0].Metadata["status_phase"] != "" {
		t.Fatalf("stub deletion should have empty status; status_phase = %q", got[0].Metadata["status_phase"])
	}
}
