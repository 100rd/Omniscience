// event_lag_wiring_test.go (#198): per-controller assertions that the
// RecordEmit helper call-site fires exactly once per successful reconcile.
//
// These tests prove the wiring (the helper unit tests in
// operator/internal/metrics/record_emit_test.go prove the helper itself).
// Coverage strategy: one representative per category (workload via Service /
// Node / StorageClass, ArgoCD CRD via Application, Argo Rollouts via
// Rollout, DRA CRD via ResourceClaim) plus a deletion-stub negative case
// on Deployment. Per-kind exhaustive coverage is unnecessary because the
// call-site is a one-line helper invocation that either runs or doesn't —
// a bug in any of the 23 sites would surface as a flat SampleCount for
// that kind.
package controller_test

import (
	"context"
	"testing"
	"time"

	rolloutsv1alpha1 "github.com/argoproj/argo-rollouts/pkg/apis/rollouts/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	resourcev1beta1 "k8s.io/api/resource/v1beta1"
	storagev1 "k8s.io/api/storage/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/Omniscience/operator/internal/controller"
	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
)

// eventLagSampleCount returns the cumulative SampleCount for the
// event_lag_seconds histogram for the given kind, or 0 if the series is
// not yet emitted. Duplicated from metrics/record_emit_test.go because Go
// test helpers do not cross package boundaries.
func eventLagSampleCount(t *testing.T, kind string) uint64 {
	t.Helper()
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != "omniscience_operator_event_lag_seconds" {
			continue
		}
		for _, m := range f.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "kind" && lp.GetValue() == kind {
					return m.GetHistogram().GetSampleCount()
				}
			}
		}
	}
	return 0
}

// reconcileObservedFreshness pre-snapshots, runs the reconcile, and asserts
// the SampleCount went up by exactly one.
func reconcileObservedFreshness(t *testing.T, kind string, run func() error) {
	t.Helper()
	opmetrics.MustRegister()
	before := eventLagSampleCount(t, kind)
	if err := run(); err != nil {
		t.Fatalf("reconcile failed: %v", err)
	}
	after := eventLagSampleCount(t, kind)
	if after != before+1 {
		t.Errorf("event_lag_seconds{kind=%q} SampleCount went %d → %d, want +1", kind, before, after)
	}
}

// freshObjectMeta returns ObjectMeta with a CreationTimestamp set 1 second
// in the past so RecordEmit observes a non-zero, non-clock-skewed lag.
func freshObjectMeta(namespace, name string) metav1.ObjectMeta {
	return metav1.ObjectMeta{
		Namespace:         namespace,
		Name:              name,
		CreationTimestamp: metav1.NewTime(time.Now().Add(-time.Second)),
	}
}

// ─── Networking + config (#158): Service. ──────────────────────────────────

func TestServiceReconciler_RecordsEventLag_198(t *testing.T) {
	s := newScheme(t)
	svc := &corev1.Service{
		ObjectMeta: freshObjectMeta("team-a", "checkout"),
		Spec:       corev1.ServiceSpec{ClusterIP: "10.0.0.1"},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(svc).Build()
	pub := &fakePublisher{}
	r, err := controller.NewServiceReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("NewServiceReconciler: %v", err)
	}
	reconcileObservedFreshness(t, "Service", func() error {
		_, e := r.Reconcile(context.Background(), req("team-a", "checkout"))
		return e
	})
}

// ─── Cluster-scoped (#159): Node. ──────────────────────────────────────────

func TestNodeReconciler_RecordsEventLag_198(t *testing.T) {
	s := newScheme(t)
	node := &corev1.Node{
		ObjectMeta: freshObjectMeta("", "node-01"),
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(node).Build()
	pub := &fakePublisher{}
	r, err := controller.NewNodeReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("NewNodeReconciler: %v", err)
	}
	reconcileObservedFreshness(t, "Node", func() error {
		_, e := r.Reconcile(context.Background(), req("", "node-01"))
		return e
	})
}

// ─── ArgoCD (#160): Application via *unstructured.Unstructured. ────────────
// The call-site passes the unstructured to RecordEmit so the helper sees
// the full ObjectMeta (the typed argocd.Application mirror only carries
// the read subset; CreationTimestamp is on the raw object).

func TestArgoCDApplicationReconciler_RecordsEventLag_198(t *testing.T) {
	s := newScheme(t)
	u := &unstructured.Unstructured{Object: map[string]any{}}
	u.SetGroupVersionKind(schema.GroupVersionKind{
		Group: "argoproj.io", Version: "v1alpha1", Kind: "Application",
	})
	u.SetNamespace("argocd")
	u.SetName("checkout-app")
	u.SetCreationTimestamp(metav1.NewTime(time.Now().Add(-time.Second)))
	// Minimum spec/status so decodeApplication succeeds (ApplicationToEvent
	// is called on the decoded typed mirror).
	u.Object["spec"] = map[string]any{
		"project":     "default",
		"destination": map[string]any{"server": "https://kubernetes.default.svc", "namespace": "ns"},
	}
	u.Object["status"] = map[string]any{
		"sync":   map[string]any{"status": "Synced"},
		"health": map[string]any{"status": "Healthy"},
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(u).Build()
	pub := &fakePublisher{}
	r, err := controller.NewArgoCDApplicationReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("NewArgoCDApplicationReconciler: %v", err)
	}
	reconcileObservedFreshness(t, "Application", func() error {
		_, e := r.Reconcile(context.Background(), req("argocd", "checkout-app"))
		return e
	})
}

// ─── Argo Rollouts (#161): Rollout. ────────────────────────────────────────

func TestArgoRolloutReconciler_RecordsEventLag_198(t *testing.T) {
	s := newScheme(t)
	if err := rolloutsv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("rollouts AddToScheme: %v", err)
	}
	ro := &rolloutsv1alpha1.Rollout{
		ObjectMeta: freshObjectMeta("team-a", "checkout-rollout"),
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(ro).Build()
	pub := &fakePublisher{}
	r, err := controller.NewArgoRolloutReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("NewArgoRolloutReconciler: %v", err)
	}
	reconcileObservedFreshness(t, "Rollout", func() error {
		_, e := r.Reconcile(context.Background(), req("team-a", "checkout-rollout"))
		return e
	})
}

// ─── DRA (#162/#190): ResourceClaim. ───────────────────────────────────────

func TestResourceClaimReconciler_RecordsEventLag_198(t *testing.T) {
	s := newScheme(t)
	if err := resourcev1beta1.AddToScheme(s); err != nil {
		t.Fatalf("resource AddToScheme: %v", err)
	}
	rc := &resourcev1beta1.ResourceClaim{
		ObjectMeta: freshObjectMeta("team-a", "claim-01"),
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(rc).Build()
	pub := &fakePublisher{}
	r, err := controller.NewResourceClaimReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("NewResourceClaimReconciler: %v", err)
	}
	reconcileObservedFreshness(t, "ResourceClaim", func() error {
		_, e := r.Reconcile(context.Background(), req("team-a", "claim-01"))
		return e
	})
}

// ─── Cluster-scoped storage: StorageClass widens cluster-scoped coverage. ──

func TestStorageClassReconciler_RecordsEventLag_198(t *testing.T) {
	s := newScheme(t)
	if err := storagev1.AddToScheme(s); err != nil {
		t.Fatalf("storage AddToScheme: %v", err)
	}
	sc := &storagev1.StorageClass{
		ObjectMeta:  freshObjectMeta("", "gp3"),
		Provisioner: "ebs.csi.aws.com",
	}
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(sc).Build()
	pub := &fakePublisher{}
	r, err := controller.NewStorageClassReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("NewStorageClassReconciler: %v", err)
	}
	reconcileObservedFreshness(t, "StorageClass", func() error {
		_, e := r.Reconcile(context.Background(), req("", "gp3"))
		return e
	})
}

// ─── Negative path: deletion stub must NOT record freshness. ──────────────
// The NotFound branch builds a stub from req.NamespacedName with zero
// CreationTimestamp; RecordEmit must short-circuit (no observation) so the
// histogram does not get a bogus huge-lag sample (now − zero-time = decades).
// Wiring choice: callers do not invoke RecordEmit on the deletion path at
// all — the helper short-circuit is the second line of defence.

func TestDeploymentReconciler_DeletionStub_DoesNotRecordEventLag_198(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build() // no objects → NotFound
	pub := &fakePublisher{}
	r, err := controller.NewDeploymentReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("NewDeploymentReconciler: %v", err)
	}
	opmetrics.MustRegister()
	before := eventLagSampleCount(t, "Deployment")
	if _, e := r.Reconcile(context.Background(), req("team-a", "gone")); e != nil {
		t.Fatalf("reconcile: %v", e)
	}
	after := eventLagSampleCount(t, "Deployment")
	if after != before {
		t.Errorf("deletion path recorded freshness lag (count went %d → %d) — should short-circuit", before, after)
	}
	if got := pub.snapshot(); len(got) != 1 {
		t.Fatalf("publisher saw %d events on deletion, want 1", len(got))
	}
}
