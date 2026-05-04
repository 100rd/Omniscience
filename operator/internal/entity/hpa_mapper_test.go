// hpa_mapper_test.go: pure unit tests for HorizontalPodAutoscalerToEvent (#214).
package entity_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

var (
	hpaTestWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	hpaTestClusterID   = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	hpaTestClusterName = "prod-eu"
	hpaTestNow         = time.Date(2026, 5, 4, 12, 0, 0, 0, time.UTC)
)

func hpaInt32Ptr(i int32) *int32  { return &i }
func hpaQty(s string) resource.Quantity { return resource.MustParse(s) }

// ─── Envelope ─────────────────────────────────────────────────────────────

func TestHPAToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "api-hpa",
			Labels: map[string]string{
				"omniscience.io/workspace": "evil-ws",
			},
		},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{
				Kind: "Deployment", Name: "api", APIVersion: "apps/v1",
			},
			MaxReplicas: 10,
		},
	}
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)

	if ev.WorkspaceID != hpaTestWorkspace {
		t.Fatalf("workspace_id = %s — adversarial label honoured!", ev.WorkspaceID)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/HorizontalPodAutoscaler/team-a/api-hpa" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/HorizontalPodAutoscaler/api-hpa" {
		t.Fatalf("uri = %q", ev.URI)
	}
	if ev.Metadata["scale_target_kind"] != "Deployment" || ev.Metadata["scale_target_name"] != "api" || ev.Metadata["scale_target_api_version"] != "apps/v1" {
		t.Fatalf("scale_target = %q/%q/%q", ev.Metadata["scale_target_kind"], ev.Metadata["scale_target_name"], ev.Metadata["scale_target_api_version"])
	}
}

// ACL invariant: only the closed allow-list of metadata keys is emitted.
func TestHPAToEvent_ACL_NoUserLabelsOrAnnotationsLeak(t *testing.T) {
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:   "team-a",
			Name:        "api-hpa",
			Labels:      map[string]string{"team": "platform", "cost-center": "eng-42"},
			Annotations: map[string]string{"description": "api autoscaler"},
		},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "api", APIVersion: "apps/v1"},
			MaxReplicas:    10,
		},
	}
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"scale_target_kind": {}, "scale_target_name": {}, "scale_target_api_version": {},
		"min_replicas": {}, "max_replicas": {}, "metrics_count": {}, "metrics_json": {},
		"status_current_replicas": {}, "status_desired_replicas": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q) — ACL allow-list violated", k, ev.Metadata[k])
		}
	}
}

// ─── Fixture 1: minimal — only required scale target + maxReplicas ────────

func TestHPAToEvent_MinimalSpec(t *testing.T) {
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "min"},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "x", APIVersion: "apps/v1"},
			MaxReplicas:    5,
		},
	}
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionCreated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)

	if ev.Metadata["max_replicas"] != "5" {
		t.Fatalf("max_replicas = %q", ev.Metadata["max_replicas"])
	}
	if ev.Metadata["min_replicas"] != "" {
		t.Fatalf("min_replicas = %q, want empty for nil", ev.Metadata["min_replicas"])
	}
	if ev.Metadata["metrics_count"] != "0" {
		t.Fatalf("metrics_count = %q", ev.Metadata["metrics_count"])
	}
	if _, present := ev.Metadata["metrics_json"]; present {
		t.Fatalf("metrics_json must be absent when no metrics")
	}
	if ev.Metadata["status_current_replicas"] != "0" {
		t.Fatalf("status_current_replicas = %q", ev.Metadata["status_current_replicas"])
	}
}

// ─── Fixture 2: Resource metric (CPU averageUtilization) ──────────────────

func TestHPAToEvent_ResourceMetric(t *testing.T) {
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "cpu-hpa"},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "api", APIVersion: "apps/v1"},
			MinReplicas:    hpaInt32Ptr(2),
			MaxReplicas:    10,
			Metrics: []autoscalingv2.MetricSpec{{
				Type: autoscalingv2.ResourceMetricSourceType,
				Resource: &autoscalingv2.ResourceMetricSource{
					Name: corev1.ResourceCPU,
					Target: autoscalingv2.MetricTarget{
						Type:               autoscalingv2.UtilizationMetricType,
						AverageUtilization: hpaInt32Ptr(75),
					},
				},
			}},
		},
		Status: autoscalingv2.HorizontalPodAutoscalerStatus{
			CurrentReplicas: 3, DesiredReplicas: 4,
		},
	}
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)

	if ev.Metadata["min_replicas"] != "2" {
		t.Fatalf("min_replicas = %q", ev.Metadata["min_replicas"])
	}
	if ev.Metadata["status_current_replicas"] != "3" {
		t.Fatalf("status_current_replicas = %q", ev.Metadata["status_current_replicas"])
	}
	if ev.Metadata["status_desired_replicas"] != "4" {
		t.Fatalf("status_desired_replicas = %q", ev.Metadata["status_desired_replicas"])
	}

	var got []map[string]any
	if err := json.Unmarshal([]byte(ev.Metadata["metrics_json"]), &got); err != nil {
		t.Fatalf("metrics_json not valid JSON: %v\n%s", err, ev.Metadata["metrics_json"])
	}
	if len(got) != 1 || got[0]["type"] != "Resource" {
		t.Fatalf("got %#v", got)
	}
	res := got[0]["resource"].(map[string]any)
	if res["name"] != "cpu" {
		t.Fatalf("resource.name = %q", res["name"])
	}
	target := res["target"].(map[string]any)
	if target["averageUtilization"] != "75" {
		t.Fatalf("averageUtilization = %q (numeric rendered as decimal string)", target["averageUtilization"])
	}
}

// ─── Fixture 3: multi-metric (Resource + Pods + Object) sorted ────────────

func TestHPAToEvent_MultipleMetrics(t *testing.T) {
	avgVal := hpaQty("100")
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "multi"},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "api", APIVersion: "apps/v1"},
			MaxReplicas:    20,
			Metrics: []autoscalingv2.MetricSpec{
				// Intentionally unsorted on input.
				{
					Type: autoscalingv2.PodsMetricSourceType,
					Pods: &autoscalingv2.PodsMetricSource{
						Metric: autoscalingv2.MetricIdentifier{Name: "requests_per_second"},
						Target: autoscalingv2.MetricTarget{Type: autoscalingv2.AverageValueMetricType, AverageValue: &avgVal},
					},
				},
				{
					Type: autoscalingv2.ResourceMetricSourceType,
					Resource: &autoscalingv2.ResourceMetricSource{
						Name:   corev1.ResourceCPU,
						Target: autoscalingv2.MetricTarget{Type: autoscalingv2.UtilizationMetricType, AverageUtilization: hpaInt32Ptr(75)},
					},
				},
				{
					Type: autoscalingv2.ObjectMetricSourceType,
					Object: &autoscalingv2.ObjectMetricSource{
						DescribedObject: autoscalingv2.CrossVersionObjectReference{Kind: "Service", Name: "api"},
						Metric:          autoscalingv2.MetricIdentifier{Name: "p95_latency"},
						Target:          autoscalingv2.MetricTarget{Type: autoscalingv2.ValueMetricType, Value: &avgVal},
					},
				},
			},
		},
	}
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)
	if ev.Metadata["metrics_count"] != "3" {
		t.Fatalf("metrics_count = %q", ev.Metadata["metrics_count"])
	}
}

// ─── Fixture 4: External + ContainerResource metric types ─────────────────

func TestHPAToEvent_ExternalAndContainerResourceMetrics(t *testing.T) {
	avgVal := hpaQty("50")
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "ext"},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "api", APIVersion: "apps/v1"},
			MaxReplicas:    10,
			Metrics: []autoscalingv2.MetricSpec{
				{
					Type: autoscalingv2.ExternalMetricSourceType,
					External: &autoscalingv2.ExternalMetricSource{
						Metric: autoscalingv2.MetricIdentifier{Name: "queue_depth"},
						Target: autoscalingv2.MetricTarget{Type: autoscalingv2.AverageValueMetricType, AverageValue: &avgVal},
					},
				},
				{
					Type: autoscalingv2.ContainerResourceMetricSourceType,
					ContainerResource: &autoscalingv2.ContainerResourceMetricSource{
						Name:      corev1.ResourceMemory,
						Container: "app",
						Target:    autoscalingv2.MetricTarget{Type: autoscalingv2.UtilizationMetricType, AverageUtilization: hpaInt32Ptr(80)},
					},
				},
			},
		},
	}
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)
	var got []map[string]any
	if err := json.Unmarshal([]byte(ev.Metadata["metrics_json"]), &got); err != nil {
		t.Fatalf("metrics_json: %v", err)
	}
	// Two metrics rendered, types both visible (sorted).
	types := make(map[string]bool, len(got))
	for _, m := range got {
		types[m["type"].(string)] = true
	}
	if !types["External"] || !types["ContainerResource"] {
		t.Fatalf("missing metric types in %#v", got)
	}
}

// ─── Determinism ──────────────────────────────────────────────────────────

func TestHPAToEvent_DeterministicMetricsJSON(t *testing.T) {
	build := func() *autoscalingv2.HorizontalPodAutoscaler {
		return &autoscalingv2.HorizontalPodAutoscaler{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
			Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
				ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "x", APIVersion: "apps/v1"},
				MaxReplicas:    10,
				Metrics: []autoscalingv2.MetricSpec{
					{Type: autoscalingv2.ResourceMetricSourceType, Resource: &autoscalingv2.ResourceMetricSource{Name: corev1.ResourceMemory, Target: autoscalingv2.MetricTarget{Type: autoscalingv2.UtilizationMetricType, AverageUtilization: hpaInt32Ptr(80)}}},
					{Type: autoscalingv2.ResourceMetricSourceType, Resource: &autoscalingv2.ResourceMetricSource{Name: corev1.ResourceCPU, Target: autoscalingv2.MetricTarget{Type: autoscalingv2.UtilizationMetricType, AverageUtilization: hpaInt32Ptr(75)}}},
				},
			},
		}
	}
	a := entity.HorizontalPodAutoscalerToEvent(build(), entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)
	b := entity.HorizontalPodAutoscalerToEvent(build(), entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)
	if a.Metadata["metrics_json"] != b.Metadata["metrics_json"] {
		t.Fatalf("non-deterministic metrics_json:\nA: %s\nB: %s", a.Metadata["metrics_json"], b.Metadata["metrics_json"])
	}
}

// ─── Default namespace fallback ───────────────────────────────────────────

func TestHPAToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "x", APIVersion: "apps/v1"},
			MaxReplicas:    1,
		},
	}
	hpa.Name = "x"
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/HorizontalPodAutoscaler/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
}

// ─── Behavior policies are NOT emitted ────────────────────────────────────

func TestHPAToEvent_BehaviorNotEmitted(t *testing.T) {
	stabWindow := int32(60)
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{Kind: "Deployment", Name: "x", APIVersion: "apps/v1"},
			MaxReplicas:    10,
			Behavior: &autoscalingv2.HorizontalPodAutoscalerBehavior{
				ScaleDown: &autoscalingv2.HPAScalingRules{StabilizationWindowSeconds: &stabWindow},
			},
		},
	}
	ev := entity.HorizontalPodAutoscalerToEvent(hpa, entity.ActionUpdated, hpaTestWorkspace, hpaTestClusterID, hpaTestClusterName, hpaTestNow)
	for k := range ev.Metadata {
		if k == "behavior" || k == "behavior_json" || k == "scale_down" {
			t.Fatalf("behavior policies leaked into metadata under key %q", k)
		}
	}
}
