// hpa_mapper.go: autoscalingv2.HorizontalPodAutoscaler -> Event mapper (issue #214).
//
// HPA is a namespaced workload-autoscaling resource. The mapper emits ONLY
// the closed allow-list of fields from baseMetadata plus the scale target,
// replica bounds, metric specs (essential subset, not full source configs),
// and current/desired replica counts.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through.
//   - `namespace` appears in baseMetadata + external_id ONLY.
//   - `spec.behavior` (scale-up/scale-down policies) is NOT emitted —
//     it's operational tuning, not graph-relevant.
//   - Metric specs are rendered as a discriminated-union JSON keeping only
//     the fields needed for graph linking and threshold understanding.
package entity

import (
	"encoding/json"
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
)

// EntityKindHorizontalPodAutoscaler is the K8s kind segment.
const EntityKindHorizontalPodAutoscaler = "HorizontalPodAutoscaler"

// HorizontalPodAutoscalerToEvent maps an autoscalingv2.HorizontalPodAutoscaler
// plus an action into an Event. Pure function — no I/O, no clients, no clock
// beyond `now`.
//
// Emitted metadata:
//
//   - Base allow-list: cluster, cluster_id, namespace, name, kind, emitter
//   - "scale_target_kind":         e.g. "Deployment"
//   - "scale_target_name":         workload name
//   - "scale_target_api_version":  e.g. "apps/v1"
//   - "min_replicas":              decimal string; empty when nil
//   - "max_replicas":              decimal string
//   - "metrics_count":             decimal string of len(.Spec.Metrics)
//   - "metrics_json":              deterministic JSON; absent when no metrics
//   - "status_current_replicas":   decimal string
//   - "status_desired_replicas":   decimal string
func HorizontalPodAutoscalerToEvent(hpa *autoscalingv2.HorizontalPodAutoscaler, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(hpa.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindHorizontalPodAutoscaler, hpa.Name)

	meta["scale_target_kind"] = hpa.Spec.ScaleTargetRef.Kind
	meta["scale_target_name"] = hpa.Spec.ScaleTargetRef.Name
	meta["scale_target_api_version"] = hpa.Spec.ScaleTargetRef.APIVersion

	if hpa.Spec.MinReplicas != nil {
		meta["min_replicas"] = strconv.FormatInt(int64(*hpa.Spec.MinReplicas), 10)
	} else {
		meta["min_replicas"] = ""
	}
	meta["max_replicas"] = strconv.FormatInt(int64(hpa.Spec.MaxReplicas), 10)

	meta["metrics_count"] = strconv.Itoa(len(hpa.Spec.Metrics))
	if len(hpa.Spec.Metrics) > 0 {
		raw, err := marshalHPAMetrics(hpa.Spec.Metrics)
		if err != nil {
			meta["metrics_json"] = ""
		} else {
			meta["metrics_json"] = raw
		}
	}

	meta["status_current_replicas"] = strconv.FormatInt(int64(hpa.Status.CurrentReplicas), 10)
	meta["status_desired_replicas"] = strconv.FormatInt(int64(hpa.Status.DesiredReplicas), 10)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindHorizontalPodAutoscaler, namespace, hpa.Name),
		URI:         uriFor(clusterName, namespace, EntityKindHorizontalPodAutoscaler, hpa.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// hpaMetricView is the deterministic JSON shape for one MetricSpec. The
// `type` discriminator selects which sub-field is populated. We render
// only the essential subset of each variant — enough for graph linking
// and threshold understanding, not the full metric source config.
type hpaMetricView struct {
	Type              string                  `json:"type"`
	Resource          *hpaResourceMetricView  `json:"resource,omitempty"`
	Pods              *hpaPodsMetricView      `json:"pods,omitempty"`
	Object            *hpaObjectMetricView    `json:"object,omitempty"`
	External          *hpaExternalMetricView  `json:"external,omitempty"`
	ContainerResource *hpaContainerResourceView `json:"containerResource,omitempty"`
}

type hpaResourceMetricView struct {
	Name   string            `json:"name"`
	Target hpaMetricTargetView `json:"target"`
}

type hpaPodsMetricView struct {
	MetricName string            `json:"metricName"`
	Target     hpaMetricTargetView `json:"target"`
}

type hpaObjectMetricView struct {
	MetricName       string            `json:"metricName"`
	DescribedKind    string            `json:"describedKind"`
	DescribedName    string            `json:"describedName"`
	Target           hpaMetricTargetView `json:"target"`
}

type hpaExternalMetricView struct {
	MetricName string            `json:"metricName"`
	Target     hpaMetricTargetView `json:"target"`
}

type hpaContainerResourceView struct {
	Name      string            `json:"name"`
	Container string            `json:"container"`
	Target    hpaMetricTargetView `json:"target"`
}

type hpaMetricTargetView struct {
	Type               string `json:"type"`
	AverageUtilization string `json:"averageUtilization,omitempty"`
	AverageValue       string `json:"averageValue,omitempty"`
	Value              string `json:"value,omitempty"`
}

// marshalHPAMetrics renders []MetricSpec as deterministic JSON. Metrics are
// sorted by their JSON byte-form so two operators emit identical bytes
// regardless of source ordering.
func marshalHPAMetrics(metrics []autoscalingv2.MetricSpec) (string, error) {
	views := make([]hpaMetricView, 0, len(metrics))
	for _, m := range metrics {
		views = append(views, viewFromMetricSpec(m))
	}

	type sortable struct {
		view hpaMetricView
		key  string
	}
	keyed := make([]sortable, 0, len(views))
	for _, v := range views {
		b, err := json.Marshal(v)
		if err != nil {
			return "", err
		}
		keyed = append(keyed, sortable{view: v, key: string(b)})
	}
	sort.Slice(keyed, func(i, j int) bool { return keyed[i].key < keyed[j].key })

	out := make([]hpaMetricView, 0, len(keyed))
	for _, k := range keyed {
		out = append(out, k.view)
	}

	b, err := json.Marshal(out)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// viewFromMetricSpec converts one autoscaling MetricSpec into the
// closed-shape view, populating exactly one variant pointer based on .Type.
// Unknown / future metric types render as type-only views (no variant
// pointer) so we don't silently drop them.
func viewFromMetricSpec(m autoscalingv2.MetricSpec) hpaMetricView {
	v := hpaMetricView{Type: string(m.Type)}
	switch m.Type {
	case autoscalingv2.ResourceMetricSourceType:
		if m.Resource != nil {
			v.Resource = &hpaResourceMetricView{
				Name:   string(m.Resource.Name),
				Target: targetView(m.Resource.Target),
			}
		}
	case autoscalingv2.PodsMetricSourceType:
		if m.Pods != nil {
			v.Pods = &hpaPodsMetricView{
				MetricName: m.Pods.Metric.Name,
				Target:     targetView(m.Pods.Target),
			}
		}
	case autoscalingv2.ObjectMetricSourceType:
		if m.Object != nil {
			v.Object = &hpaObjectMetricView{
				MetricName:    m.Object.Metric.Name,
				DescribedKind: m.Object.DescribedObject.Kind,
				DescribedName: m.Object.DescribedObject.Name,
				Target:        targetView(m.Object.Target),
			}
		}
	case autoscalingv2.ExternalMetricSourceType:
		if m.External != nil {
			v.External = &hpaExternalMetricView{
				MetricName: m.External.Metric.Name,
				Target:     targetView(m.External.Target),
			}
		}
	case autoscalingv2.ContainerResourceMetricSourceType:
		if m.ContainerResource != nil {
			v.ContainerResource = &hpaContainerResourceView{
				Name:      string(m.ContainerResource.Name),
				Container: m.ContainerResource.Container,
				Target:    targetView(m.ContainerResource.Target),
			}
		}
	}
	return v
}

// targetView renders MetricTarget. Numeric fields render as decimal strings
// so the JSON shape stays uniformly string-typed.
func targetView(t autoscalingv2.MetricTarget) hpaMetricTargetView {
	v := hpaMetricTargetView{Type: string(t.Type)}
	if t.AverageUtilization != nil {
		v.AverageUtilization = strconv.FormatInt(int64(*t.AverageUtilization), 10)
	}
	if t.AverageValue != nil {
		v.AverageValue = t.AverageValue.String()
	}
	if t.Value != nil {
		v.Value = t.Value.String()
	}
	return v
}
