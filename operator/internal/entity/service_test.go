package entity_test

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func newService(namespace, name string, selector map[string]string, t corev1.ServiceType) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace,
			Name:      name,
			// Hostile labels & annotations — must be ignored.
			Labels:      map[string]string{"adversarial.example.com/spy": "no"},
			Annotations: map[string]string{"omniscience.io/workspace-id": "00000000-DEAD-BEEF-DEAD-000000000000"},
		},
		Spec: corev1.ServiceSpec{
			Type:      t,
			ClusterIP: "10.0.0.42",
			Selector:  selector,
			Ports: []corev1.ServicePort{
				{Name: "http", Port: 80},
				{Name: "https", Port: 443},
			},
		},
	}
}

func TestServiceToEvent_HappyPath(t *testing.T) {
	svc := newService("team-a", "checkout", map[string]string{"app": "checkout", "tier": "frontend"}, corev1.ServiceTypeClusterIP)
	ev := entity.ServiceToEvent(svc, entity.ActionCreated, fixedWorkspace, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/Service/team-a/checkout" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/Service/checkout" {
		t.Fatalf("uri = %q", ev.URI)
	}
	if ev.Metadata["service_type"] != "ClusterIP" {
		t.Fatalf("service_type = %q", ev.Metadata["service_type"])
	}
	if ev.Metadata["cluster_ip"] != "10.0.0.42" {
		t.Fatalf("cluster_ip = %q", ev.Metadata["cluster_ip"])
	}
	if ev.Metadata["selector"] != "app=checkout,tier=frontend" {
		t.Fatalf("selector = %q", ev.Metadata["selector"])
	}
	if ev.Metadata["port_count"] != "2" {
		t.Fatalf("port_count = %q", ev.Metadata["port_count"])
	}
	if len(ev.TopologyEdges) != 0 {
		t.Fatalf("Service must NOT emit edges; got %d", len(ev.TopologyEdges))
	}
	// Hostile keys must NOT appear in metadata.
	for k := range ev.Metadata {
		if strings.HasPrefix(k, "adversarial") || k == "omniscience.io/workspace-id" {
			t.Fatalf("hostile metadata key leaked: %q", k)
		}
	}
}

func TestServiceToEvent_HeadlessNoSelector(t *testing.T) {
	svc := newService("team-a", "headless", nil, corev1.ServiceTypeClusterIP)
	svc.Spec.ClusterIP = "None"
	ev := entity.ServiceToEvent(svc, entity.ActionUpdated, fixedWorkspace, "prod-eu", fixedNow)
	if ev.Metadata["selector"] != "" {
		t.Fatalf("empty selector should render as \"\"; got %q", ev.Metadata["selector"])
	}
	if ev.Metadata["cluster_ip"] != "None" {
		t.Fatalf("cluster_ip = %q", ev.Metadata["cluster_ip"])
	}
}

func TestServiceToEvent_NamespacelessFallsBack(t *testing.T) {
	svc := newService("", "rogue", nil, corev1.ServiceTypeClusterIP)
	ev := entity.ServiceToEvent(svc, entity.ActionCreated, fixedWorkspace, "prod-eu", fixedNow)
	if ev.ExternalID != "k8s_resource/Service/default/rogue" {
		t.Fatalf("external_id = %q (expected default fallback)", ev.ExternalID)
	}
}
