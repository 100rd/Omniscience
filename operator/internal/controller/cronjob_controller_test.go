// cronjob_controller_test.go: tests for CronJobReconciler (#210).
package controller_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	batchv1 "k8s.io/api/batch/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/omniscience/operator/internal/controller"
	"github.com/100rd/omniscience/operator/internal/entity"
)

// ─── Constructor preconditions ─────────────────────────────────────────────

func TestNewCronJobReconciler_RejectsZeroWorkspace(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewCronJobReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
}

func TestNewCronJobReconciler_RejectsNilPublisher(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewCronJobReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
}

func TestNewCronJobReconciler_RejectsEmptyClusterName(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewCronJobReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
}

func TestNewCronJobReconciler_RejectsNilClient(t *testing.T) {
	if _, err := controller.NewCronJobReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

// ─── Lifecycle: create / update / delete ───────────────────────────────────

func cjFixture(ns, name string) *batchv1.CronJob {
	return &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: batchv1.CronJobSpec{
			Schedule:          "0 2 * * *",
			ConcurrencyPolicy: batchv1.AllowConcurrent,
		},
	}
}

func TestCronJobReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	cj := cjFixture("team-a", "nightly")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(cj).Build()
	pub := &fakePublisher{}

	r, err := controller.NewCronJobReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "nightly")); err != nil {
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
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/CronJob/team-a/nightly" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["schedule"] != "0 2 * * *" {
		t.Fatalf("schedule = %q", ev.Metadata["schedule"])
	}
	if ev.Metadata["concurrency_policy"] != "Allow" {
		t.Fatalf("concurrency_policy = %q", ev.Metadata["concurrency_policy"])
	}
}

// Update path: spec change between reconciles is reflected in the second
// event. (We test a spec field rather than a status field because the
// controller-runtime fake client's Update() does not propagate to the
// status subresource by default; see status subresource semantics.)
func TestCronJobReconciler_UpdateEmitsSecondUpdated(t *testing.T) {
	s := newScheme(t)
	cj := cjFixture("team-a", "nightly")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(cj).Build()
	pub := &fakePublisher{}

	r, err := controller.NewCronJobReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req("team-a", "nightly")); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	var live batchv1.CronJob
	if err := c.Get(ctx, req("team-a", "nightly").NamespacedName, &live); err != nil {
		t.Fatalf("get: %v", err)
	}
	suspend := true
	live.Spec.Suspend = &suspend
	if err := c.Update(ctx, &live); err != nil {
		t.Fatalf("update: %v", err)
	}

	if _, err := r.Reconcile(ctx, req("team-a", "nightly")); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 2 {
		t.Fatalf("publisher saw %d events, want 2", len(got))
	}
	if got[0].Metadata["suspend"] != "" {
		t.Fatalf("first suspend = %q, want empty (nil)", got[0].Metadata["suspend"])
	}
	if got[1].Metadata["suspend"] != "true" {
		t.Fatalf("second suspend = %q, want \"true\" (spec update should flow through)", got[1].Metadata["suspend"])
	}
}

// Delete path: empty client → IsNotFound → Deleted event for stub.
func TestCronJobReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}

	r, err := controller.NewCronJobReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "nightly")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	if got[0].Action != entity.ActionDeleted {
		t.Fatalf("action = %q", got[0].Action)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/CronJob/team-a/nightly" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
	if got[0].Metadata["schedule"] != "" {
		t.Fatalf("stub deletion should have empty schedule; got %q", got[0].Metadata["schedule"])
	}
}
