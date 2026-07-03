// hpa_controller_test.go: tests for HPAReconciler (#214).
package controller_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/100rd/Omniscience/operator/internal/controller"
	"github.com/100rd/Omniscience/operator/internal/entity"
)

func hpaCtrlInt32Ptr(i int32) *int32 { return &i }

// ─── Constructor preconditions ─────────────────────────────────────────────

func TestNewHPAReconciler_Preconditions(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	if _, err := controller.NewHPAReconciler(c, &fakePublisher{}, uuid.UUID{}, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on zero workspace UUID")
	}
	if _, err := controller.NewHPAReconciler(c, nil, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil publisher")
	}
	if _, err := controller.NewHPAReconciler(c, &fakePublisher{}, tcWorkspace, fixedClusterID, ""); err == nil {
		t.Fatalf("expected error on empty cluster name")
	}
	if _, err := controller.NewHPAReconciler(nil, &fakePublisher{}, tcWorkspace, fixedClusterID, tcClusterName); err == nil {
		t.Fatalf("expected error on nil client")
	}
}

// ─── Lifecycle: create / update / delete ───────────────────────────────────

func hpaFixture(ns, name string) *autoscalingv2.HorizontalPodAutoscaler {
	return &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{
				Kind: "Deployment", Name: "api", APIVersion: "apps/v1",
			},
			MinReplicas: hpaCtrlInt32Ptr(2),
			MaxReplicas: 10,
			Metrics: []autoscalingv2.MetricSpec{{
				Type: autoscalingv2.ResourceMetricSourceType,
				Resource: &autoscalingv2.ResourceMetricSource{
					Name: corev1.ResourceCPU,
					Target: autoscalingv2.MetricTarget{
						Type:               autoscalingv2.UtilizationMetricType,
						AverageUtilization: hpaCtrlInt32Ptr(75),
					},
				},
			}},
		},
	}
}

func TestHPAReconciler_CreateEmitsUpdated(t *testing.T) {
	s := newScheme(t)
	hpa := hpaFixture("team-a", "api-hpa")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(hpa).Build()
	pub := &fakePublisher{}

	r, err := controller.NewHPAReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "api-hpa")); err != nil {
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
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/HorizontalPodAutoscaler/team-a/api-hpa" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["scale_target_kind"] != "Deployment" {
		t.Fatalf("scale_target_kind = %q", ev.Metadata["scale_target_kind"])
	}
	if ev.Metadata["min_replicas"] != "2" || ev.Metadata["max_replicas"] != "10" {
		t.Fatalf("min/max = %q/%q", ev.Metadata["min_replicas"], ev.Metadata["max_replicas"])
	}
	if ev.Metadata["metrics_count"] != "1" {
		t.Fatalf("metrics_count = %q", ev.Metadata["metrics_count"])
	}
}

// Update path: maxReplicas change between reconciles flows through.
func TestHPAReconciler_UpdateEmitsSecondUpdated(t *testing.T) {
	s := newScheme(t)
	hpa := hpaFixture("team-a", "api-hpa")
	c := fake.NewClientBuilder().WithScheme(s).WithObjects(hpa).Build()
	pub := &fakePublisher{}

	r, err := controller.NewHPAReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	ctx := context.Background()
	if _, err := r.Reconcile(ctx, req("team-a", "api-hpa")); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	var live autoscalingv2.HorizontalPodAutoscaler
	if err := c.Get(ctx, req("team-a", "api-hpa").NamespacedName, &live); err != nil {
		t.Fatalf("get: %v", err)
	}
	live.Spec.MaxReplicas = 20
	if err := c.Update(ctx, &live); err != nil {
		t.Fatalf("update: %v", err)
	}

	if _, err := r.Reconcile(ctx, req("team-a", "api-hpa")); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 2 {
		t.Fatalf("publisher saw %d events, want 2", len(got))
	}
	if got[0].Metadata["max_replicas"] != "10" {
		t.Fatalf("first max_replicas = %q", got[0].Metadata["max_replicas"])
	}
	if got[1].Metadata["max_replicas"] != "20" {
		t.Fatalf("second max_replicas = %q (spec update should flow through)", got[1].Metadata["max_replicas"])
	}
}

// Delete path: empty client → IsNotFound → Deleted event for stub.
func TestHPAReconciler_DeleteEmitsDeleted(t *testing.T) {
	s := newScheme(t)
	c := fake.NewClientBuilder().WithScheme(s).Build()
	pub := &fakePublisher{}

	r, err := controller.NewHPAReconciler(c, pub, tcWorkspace, fixedClusterID, tcClusterName)
	if err != nil {
		t.Fatalf("new reconciler: %v", err)
	}
	r.Now = fixedNowFunc()

	if _, err := r.Reconcile(context.Background(), req("team-a", "api-hpa")); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	got := pub.snapshot()
	if len(got) != 1 {
		t.Fatalf("publisher saw %d events, want 1", len(got))
	}
	if got[0].Action != entity.ActionDeleted {
		t.Fatalf("action = %q", got[0].Action)
	}
	if got[0].ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/HorizontalPodAutoscaler/team-a/api-hpa" {
		t.Fatalf("external_id = %q", got[0].ExternalID)
	}
}
