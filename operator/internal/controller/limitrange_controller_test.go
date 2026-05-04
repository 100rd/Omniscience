// limitrange_controller_test.go: tests for LimitRangeReconciler (#202).
//
// envtest is not wired in this controller package (workload_controllers_test
// .go uses controller-runtime's fake client + an in-memory publisher). We
// follow the same pattern here. Lifecycle coverage:
//
//   - Create: object present in client → publisher sees Updated event with
//     correct external_id and workspace stamp.
//   - Update: object updated in client between reconciles → publisher sees
//     a second Updated event with the new spec reflected in metadata
//     (limits_count change).
//   - Delete: client returns IsNotFound → publisher sees a Deleted event
//     for a stub LimitRange (no RecordEmit on this branch).
//
// The fakePublisher and shared test fixtures (tcWorkspace, fixedClusterID,
// tcClusterName, tcFixedTime, fixedNowFunc, req, newScheme) are defined in
// workload_controllers_test.go and reused here.
package controller_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/omniscience/operator/internal/controller"
	"github.com/100rd/omniscience/operator/internal/entity"
)

func lrQty(s string) resource.Quantity { return resource.MustParse(s) }

// ─── Constructor preconditions ─────────────────────────────────────────────

func TestNewLimitRangeReconciler_RejectsZeroWorkspace(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewLimitRangeReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
}

func TestNewLimitRangeReconciler_RejectsNilPublisher(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewLimitRangeReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
}

func TestNewLimitRangeReconciler_RejectsEmptyClusterName(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewLimitRangeReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
}

func TestNewLimitRangeReconciler_RejectsNilClient(t *testing.T) {
	if _, err := controller.NewLimitRangeReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

// ─── Lifecycle: create / update / delete ───────────────────────────────────

// containerFixture builds a Container-typed LimitRange with default+min+max.
func containerFixture(ns, name string) *corev1.LimitRange {
	return &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
			Type:    corev1.LimitTypeContainer,
			Default: corev1.ResourceList{corev1.ResourceCPU: lrQty("500m")},
			Min:     corev1.ResourceList{corev1.ResourceCPU: lrQty("50m")},
			Max:     corev1.ResourceList{corev1.ResourceCPU: lrQty("2")},
		}}},
	}
}

func TestLimitRangeReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	lr := containerFixture("team-a", "container-limits")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(lr).Build()
	pub := &fakePublisher{}

	r, err := controller.NewLimitRangeReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "container-limits")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	ev := got[0]
	if ev.WorkspaceID != tcWorkspace {
		t.Fatalf("workspace_id = %s, want %s", ev.WorkspaceID, tcWorkspace)
	}
	if ev.Action != entity.ActionUpdated {
		t.Fatalf("action = %q, want %q", ev.Action, entity.ActionUpdated)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/LimitRange/team-a/container-limits" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["limits_count"] != "1" {
		t.Fatalf("limits_count = %q", ev.Metadata["limits_count"])
	}
	if !ev.EmittedAt.Equal(tcFixedTime) {
		t.Fatalf("emitted_at = %s, want %s", ev.EmittedAt, tcFixedTime)
	}
}

// Update path: reconcile twice with a spec change in between; both events
// land and reflect the current spec at reconcile time.
func TestLimitRangeReconciler_UpdateEmitsSecondUpdated(t *testing.T) {
	s := newScheme(t)
	lr := containerFixture("team-a", "container-limits")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(lr).Build()
	pub := &fakePublisher{}

	r, err := controller.NewLimitRangeReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req("team-a", "container-limits")); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	// Mutate: add a Pod-type limit so limits_count flips 1 → 2.
	var live corev1.LimitRange
	if err := c.Get(ctx, req("team-a", "container-limits").NamespacedName, &live); err != nil {
		t.Fatalf("get: %v", err)
	}
	live.Spec.Limits = append(live.Spec.Limits, corev1.LimitRangeItem{
		Type: corev1.LimitTypePod,
		Max:  corev1.ResourceList{corev1.ResourceCPU: lrQty("4")},
	})
	if err := c.Update(ctx, &live); err != nil {
		t.Fatalf("update: %v", err)
	}

	if _, err := r.Reconcile(ctx, req("team-a", "container-limits")); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 2 {
		t.Fatalf("publisher saw %d events, want 2", len(got))
	}
	if got[0].Metadata["limits_count"] != "1" {
		t.Fatalf("first event limits_count = %q, want 1", got[0].Metadata["limits_count"])
	}
	if got[1].Metadata["limits_count"] != "2" {
		t.Fatalf("second event limits_count = %q, want 2 (update should reflect new spec)", got[1].Metadata["limits_count"])
	}
	for _, ev := range got {
		if ev.Action != entity.ActionUpdated {
			t.Fatalf("action = %q, want %q", ev.Action, entity.ActionUpdated)
		}
	}
}

// Delete path: reconcile against an empty client → IsNotFound branch fires
// and a Deleted event is emitted with a stub LimitRange identity.
func TestLimitRangeReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build() // no objects
	pub := &fakePublisher{}

	r, err := controller.NewLimitRangeReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "container-limits")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	if got[0].Action != entity.ActionDeleted {
		t.Fatalf("action = %q, want %q", got[0].Action, entity.ActionDeleted)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/LimitRange/team-a/container-limits" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
	if got[0].Metadata["limits_count"] != "0" {
		t.Fatalf("stub deletion should have empty spec; limits_count = %q", got[0].Metadata["limits_count"])
	}
}

// All four spec.limits[].type fixture shapes (Container / Pod / PVC / ratio)
// round-trip through the controller without altering envelope identity.
func TestLimitRangeReconciler_AllFixtureShapes(t *testing.T) {
	cases := []struct {
		name string
		lr   *corev1.LimitRange
	}{
		{
			name: "Container",
			lr:   containerFixture("team-a", "container-limits"),
		},
		{
			name: "Pod",
			lr: &corev1.LimitRange{
				ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "pod-limits"},
				Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
					Type: corev1.LimitTypePod,
					Min:  corev1.ResourceList{corev1.ResourceCPU: lrQty("100m")},
					Max:  corev1.ResourceList{corev1.ResourceCPU: lrQty("4")},
				}}},
			},
		},
		{
			name: "PersistentVolumeClaim",
			lr: &corev1.LimitRange{
				ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "pvc-limits"},
				Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
					Type: corev1.LimitTypePersistentVolumeClaim,
					Min:  corev1.ResourceList{corev1.ResourceStorage: lrQty("1Gi")},
					Max:  corev1.ResourceList{corev1.ResourceStorage: lrQty("100Gi")},
				}}},
			},
		},
		{
			name: "MaxLimitRequestRatio",
			lr: &corev1.LimitRange{
				ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "ratio-limits"},
				Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
					Type:                 corev1.LimitTypeContainer,
					MaxLimitRequestRatio: corev1.ResourceList{corev1.ResourceCPU: lrQty("4")},
				}}},
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := newScheme(t)
			c := fake.NewClientBuilder().WithScheme(s).WithObjects(tc.lr).Build()
			pub := &fakePublisher{}
			r, err := controller.NewLimitRangeReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
			if err != nil {
				t.Fatalf("new reconciler: %v", err)
			}
			r.Now = fixedNowFunc()
			if _, err := r.Reconcile(context.Background(), req(tc.lr.Namespace, tc.lr.Name)); err != nil {
				t.Fatalf("reconcile: %v", err)
			}
			got := pub.snapshot()
			if len(got) != 1 {
				t.Fatalf("events = %d, want 1", len(got))
			}
			if got[0].Metadata["kind"] != "LimitRange" {
				t.Fatalf("kind = %q", got[0].Metadata["kind"])
			}
			if got[0].Metadata["limits_count"] != "1" {
				t.Fatalf("limits_count = %q", got[0].Metadata["limits_count"])
			}
		})
	}
}
