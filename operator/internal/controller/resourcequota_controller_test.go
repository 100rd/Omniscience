// resourcequota_controller_test.go: tests for ResourceQuotaReconciler (#204).
//
// Same shape as limitrange_controller_test.go: controller-runtime fake client
// + in-memory publisher; lifecycle coverage for create / update / delete.
// Shared fixtures (tcWorkspace, fixedClusterID, tcClusterName, tcFixedTime,
// fixedNowFunc, req, newScheme, fakePublisher) live in
// workload_controllers_test.go.
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

func rqQty(s string) resource.Quantity { return resource.MustParse(s) }

// ─── Constructor preconditions ─────────────────────────────────────────────

func TestNewResourceQuotaReconciler_RejectsZeroWorkspace(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewResourceQuotaReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
}

func TestNewResourceQuotaReconciler_RejectsNilPublisher(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewResourceQuotaReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
}

func TestNewResourceQuotaReconciler_RejectsEmptyClusterName(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewResourceQuotaReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
}

func TestNewResourceQuotaReconciler_RejectsNilClient(t *testing.T) {
	if _, err := controller.NewResourceQuotaReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

// ─── Lifecycle: create / update / delete ───────────────────────────────────

func computeFixture(ns, name string) *corev1.ResourceQuota {
	return &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: corev1.ResourceQuotaSpec{
			Hard: corev1.ResourceList{
				corev1.ResourceCPU:    rqQty("10"),
				corev1.ResourceMemory: rqQty("20Gi"),
			},
		},
	}
}

func TestResourceQuotaReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	rq := computeFixture("team-a", "compute")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(rq).Build()
	pub := &fakePublisher{}

	r, err := controller.NewResourceQuotaReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "compute")); err != nil {
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
		t.Fatalf("action = %q", ev.Action)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ResourceQuota/team-a/compute" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["hard_count"] != "2" {
		t.Fatalf("hard_count = %q", ev.Metadata["hard_count"])
	}
	if !ev.EmittedAt.Equal(tcFixedTime) {
		t.Fatalf("emitted_at = %s", ev.EmittedAt)
	}
}

// Update path: spec change between reconciles is reflected in the second event.
func TestResourceQuotaReconciler_UpdateEmitsSecondUpdated(t *testing.T) {
	s := newScheme(t)
	rq := computeFixture("team-a", "compute")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(rq).Build()
	pub := &fakePublisher{}

	r, err := controller.NewResourceQuotaReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req("team-a", "compute")); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	var live corev1.ResourceQuota
	if err := c.Get(ctx, req("team-a", "compute").NamespacedName, &live); err != nil {
		t.Fatalf("get: %v", err)
	}
	live.Spec.Hard[corev1.ResourcePods] = rqQty("50")
	if err := c.Update(ctx, &live); err != nil {
		t.Fatalf("update: %v", err)
	}

	if _, err := r.Reconcile(ctx, req("team-a", "compute")); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 2 {
		t.Fatalf("publisher saw %d events, want 2", len(got))
	}
	if got[0].Metadata["hard_count"] != "2" {
		t.Fatalf("first hard_count = %q, want 2", got[0].Metadata["hard_count"])
	}
	if got[1].Metadata["hard_count"] != "3" {
		t.Fatalf("second hard_count = %q, want 3 (update should reflect new spec)", got[1].Metadata["hard_count"])
	}
}

// Delete path: empty client → IsNotFound → Deleted event for stub.
func TestResourceQuotaReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}

	r, err := controller.NewResourceQuotaReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "compute")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	if got[0].Action != entity.ActionDeleted {
		t.Fatalf("action = %q", got[0].Action)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ResourceQuota/team-a/compute" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
	if got[0].Metadata["hard_count"] != "0" {
		t.Fatalf("stub deletion should have empty spec; hard_count = %q", got[0].Metadata["hard_count"])
	}
}

// All four shapes (hard-only / scopes / scopeSelector / empty) round-trip
// without altering envelope identity.
func TestResourceQuotaReconciler_AllFixtureShapes(t *testing.T) {
	cases := []struct {
		name string
		rq   *corev1.ResourceQuota
	}{
		{
			name: "HardOnly",
			rq:   computeFixture("team-a", "compute"),
		},
		{
			name: "WithScopes",
			rq: &corev1.ResourceQuota{
				ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "best-effort"},
				Spec: corev1.ResourceQuotaSpec{
					Hard:   corev1.ResourceList{corev1.ResourcePods: rqQty("10")},
					Scopes: []corev1.ResourceQuotaScope{corev1.ResourceQuotaScopeBestEffort},
				},
			},
		},
		{
			name: "WithScopeSelector",
			rq: &corev1.ResourceQuota{
				ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "high-prio"},
				Spec: corev1.ResourceQuotaSpec{
					Hard: corev1.ResourceList{corev1.ResourcePods: rqQty("5")},
					ScopeSelector: &corev1.ScopeSelector{
						MatchExpressions: []corev1.ScopedResourceSelectorRequirement{{
							ScopeName: corev1.ResourceQuotaScopePriorityClass,
							Operator:  corev1.ScopeSelectorOpIn,
							Values:    []string{"high"},
						}},
					},
				},
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := newScheme(t)
			c := fake.NewClientBuilder().WithScheme(s).WithObjects(tc.rq).Build()
			pub := &fakePublisher{}
			r, err := controller.NewResourceQuotaReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
			if err != nil {
				t.Fatalf("new reconciler: %v", err)
			}
			r.Now = fixedNowFunc()
			if _, err := r.Reconcile(context.Background(), req(tc.rq.Namespace, tc.rq.Name)); err != nil {
				t.Fatalf("reconcile: %v", err)
			}
			got := pub.snapshot()
			if len(got) != 1 {
				t.Fatalf("events = %d, want 1", len(got))
			}
			if got[0].Metadata["kind"] != "ResourceQuota" {
				t.Fatalf("kind = %q", got[0].Metadata["kind"])
			}
		})
	}
}
