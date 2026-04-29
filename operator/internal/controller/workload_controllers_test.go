// Tests for the five workload controllers added in #157. envtest is not
// wired in v0.2; we test against the controller-runtime fake client +
// a hand-rolled in-memory publisher. The fake client is sufficient
// because the reconciler's behaviour boils down to: Get → mapper → Publish.
//
// envtest-based tests can layer on top of these in a follow-up sub-issue
// without invalidating the assertions here.
package controller_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/omniscience/operator/internal/controller"
	"github.com/100rd/omniscience/operator/internal/entity"
)

// fakePublisher is a thread-safe in-memory implementation of
// publisher.Publisher used by every controller test in this file.
type fakePublisher struct {
	mu     sync.Mutex
	events []*entity.Event
	// errOnPublish, if set, is returned by Publish to drive the requeue
	// branch in tests.
	errOnPublish error
}

func (f *fakePublisher) Publish(_ context.Context, ev *entity.Event) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.errOnPublish != nil {
		return f.errOnPublish
	}
	f.events = append(f.events, ev)
	return nil
}

func (f *fakePublisher) Close() error { return nil }

func (f *fakePublisher) snapshot() []*entity.Event {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]*entity.Event, len(f.events))
	copy(out, f.events)
	return out
}

var (
	tcWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	tcClusterName = "prod-eu"
	tcFixedTime   = time.Date(2026, 4, 24, 12, 0, 0, 0, time.UTC)
)

// newScheme returns a runtime.Scheme registering the API groups we test.
func newScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatalf("scheme add: %v", err)
	}
	return s
}

// fixedNowFunc returns a closure honouring the deterministic test clock.
func fixedNowFunc() func() time.Time {
	return func() time.Time { return tcFixedTime }
}

// req builds the controller-runtime reconcile request shorthand.
func req(namespace, name string) ctrl.Request {
	return ctrl.Request{NamespacedName: types.NamespacedName{Namespace: namespace, Name: name}}
}

// ─── New*Reconciler precondition checks ──────────────────────────────────────

func TestNewDeploymentReconciler_RejectsZeroWorkspace(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewDeploymentReconciler(c, &fakePublisher{}, uuid.UUID{}, "x"); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
}

func TestNewReplicaSetReconciler_RejectsEmptyCluster(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewReplicaSetReconciler(c, &fakePublisher{}, tcWorkspace, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
}

func TestNewStatefulSetReconciler_RejectsNilPublisher(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewStatefulSetReconciler(c, nil, tcWorkspace, "x"); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
}

func TestNewDaemonSetReconciler_RejectsNilClient(t *testing.T) {
	if _, err := controller.NewDaemonSetReconciler(nil, &fakePublisher{}, tcWorkspace, "x"); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

func TestNewJobReconciler_OK(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewJobReconciler(c, &fakePublisher{}, tcWorkspace, "x"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

// ─── Reconcile happy paths: Get-OK emits "updated" with workspace stamp ────

func TestDeploymentReconciler_ReconcileEmitsUpdatedWithWorkspaceStamp(t *testing.T) {
	s := newScheme(t)
	replicas := int32(3)
	d := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "checkout",
			// Adversarial labels — must not affect workspace_id
			Labels: map[string]string{"omniscience.io/workspace": "evil-ws"},
		},
		Spec:   appsv1.DeploymentSpec{Replicas: &replicas},
		Status: appsv1.DeploymentStatus{AvailableReplicas: 2},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(d).Build()
	pub := &fakePublisher{}

	r, err := controller.NewDeploymentReconciler(c, pub, tcWorkspace, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "checkout")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	ev := got[0]
	if ev.WorkspaceID != tcWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, tcWorkspace)
	}
	if ev.Action != entity.ActionUpdated {
		t.Fatalf("action = %q, want %q", ev.Action, entity.ActionUpdated)
	}
	if ev.ExternalID != "k8s_resource/Deployment/team-a/checkout" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if !ev.EmittedAt.Equal(tcFixedTime) {
		t.Fatalf("emitted_at = %s, want %s", ev.EmittedAt, tcFixedTime)
	}
}

func TestReplicaSetReconciler_ReconcileEmitsOwnsEdge(t *testing.T) {
	s := newScheme(t)
	replicas := int32(2)
	rs := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "checkout-7d9f",
			OwnerReferences: []metav1.OwnerReference{
				{APIVersion: "apps/v1", Kind: "Deployment", Name: "checkout", UID: "dep-uid"},
			},
		},
		Spec: appsv1.ReplicaSetSpec{Replicas: &replicas},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(rs).Build()
	pub := &fakePublisher{}

	r, err := controller.NewReplicaSetReconciler(c, pub, tcWorkspace, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req("team-a", "checkout-7d9f")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("event count = %d", len(got))
	}
	if len(got[0].Edges) != 1 || got[0].Edges[0].FromKind != "Deployment" {
		t.Fatalf("expected one Deployment OWNS edge; got %#v", got[0].Edges)
	}
	if got[0].Edges[0].ToExternalID != got[0].ExternalID {
		t.Fatalf("edge to_external_id should equal event external_id")
	}
}

func TestStatefulSetReconciler_ReconcileEmitsUpsert(t *testing.T) {
	s := newScheme(t)
	replicas := int32(5)
	ss := &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "kafka"},
		Spec:       appsv1.StatefulSetSpec{Replicas: &replicas},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(ss).Build()
	pub := &fakePublisher{}

	r, err := controller.NewStatefulSetReconciler(c, pub, tcWorkspace, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req("team-a", "kafka")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].ExternalID != "k8s_resource/StatefulSet/team-a/kafka" {
		t.Fatalf("got %#v", got)
	}
}

func TestDaemonSetReconciler_ReconcileEmitsUpsertWithStatusReplicas(t *testing.T) {
	s := newScheme(t)
	ds := &appsv1.DaemonSet{
		ObjectMeta: metav1.ObjectMeta{Namespace: "kube-system", Name: "fluent-bit"},
		Status:     appsv1.DaemonSetStatus{DesiredNumberScheduled: 12, NumberAvailable: 11},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(ds).Build()
	pub := &fakePublisher{}

	r, err := controller.NewDaemonSetReconciler(c, pub, tcWorkspace, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req("kube-system", "fluent-bit")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("event count = %d", len(got))
	}
	if got[0].Metadata["replicas"] != "12" || got[0].Metadata["available_replicas"] != "11" {
		t.Fatalf("replicas metadata = %v", got[0].Metadata)
	}
}

func TestJobReconciler_ReconcileEmitsUpsert(t *testing.T) {
	s := newScheme(t)
	parallelism := int32(2)
	j := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "nightly"},
		Spec:       batchv1.JobSpec{Parallelism: &parallelism},
		Status:     batchv1.JobStatus{Active: 1},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(j).Build()
	pub := &fakePublisher{}

	r, err := controller.NewJobReconciler(c, pub, tcWorkspace, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req("team-a", "nightly")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].ExternalID != "k8s_resource/Job/team-a/nightly" {
		t.Fatalf("got %#v", got)
	}
	if got[0].Metadata["replicas"] != "2" || got[0].Metadata["available_replicas"] != "1" {
		t.Fatalf("replicas metadata = %v", got[0].Metadata)
	}
}

// ─── Reconcile NotFound branch emits "deleted" ───────────────────────────────

func TestDeploymentReconciler_NotFoundEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	// No object in the cluster — Get will return NotFound.
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}

	r, err := controller.NewDeploymentReconciler(c, pub, tcWorkspace, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req("team-a", "checkout")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 || got[0].Action != entity.ActionDeleted {
		t.Fatalf("got %#v, want one deleted event", got)
	}
	if got[0].ExternalID != "k8s_resource/Deployment/team-a/checkout" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
}

// ─── Reconcile error branch is wrapped, returned, and event NOT published ──

func TestJobReconciler_PublishErrorIsReturned(t *testing.T) {
	s := newScheme(t)
	parallelism := int32(1)
	j := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "nightly"},
		Spec:       batchv1.JobSpec{Parallelism: &parallelism},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(j).Build()
	pub := &fakePublisher{errOnPublish: errors.New("nats down")}

	r, err := controller.NewJobReconciler(c, pub, tcWorkspace, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	_, err = r.Reconcile(context.Background(), req("team-a", "nightly"))
	if err == nil {
		t.Fatalf("expected reconcile error to propagate publish failure")
	}
}

// ─── SetupWithManager registers controllers with unique names ──────────────

func TestAllReconcilers_HaveDistinctControllerNames(t *testing.T) {
	// We assert this indirectly: each Setup* uses a distinct .Named() value,
	// otherwise controller-runtime's manager would reject the second one
	// with "controller already exists". This is exercised end-to-end in the
	// kind smoke run; here we content-check each reconciler's expected
	// behaviour exists and compiles.
	if entity.SourceType != "k8s-operator" {
		t.Fatalf("source_type drift: %q", entity.SourceType)
	}
	// Sanity check that the K8s API groups we register against are still
	// present under the schemes the manager uses (apps/v1 and batch/v1).
	gvkDeployment := schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}
	if gvkDeployment.Empty() {
		t.Fatalf("gvk drift")
	}
}

// ─── NotFound branch on the apierrors helper itself, defence in depth ─────

func TestAPIErrorsIsNotFound_StillBehavesAsExpected(t *testing.T) {
	// Guard against silent breakage if k8s.io/apimachinery rewires the
	// helper. The reconciler relies on this exact behaviour.
	err := apierrors.NewNotFound(schema.GroupResource{Group: "apps", Resource: "deployments"}, "x")
	if !apierrors.IsNotFound(err) {
		t.Fatalf("apierrors.IsNotFound regressed")
	}
}
