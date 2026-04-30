package entity_test

import (
	"testing"

	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func ptrString(s string) *string { return &s }

func TestIngressToEvent_RoutesToFromRulesAndDefaultBackend(t *testing.T) {
	ing := &networkingv1.Ingress{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "main"},
		Spec: networkingv1.IngressSpec{
			IngressClassName: ptrString("nginx"),
			DefaultBackend: &networkingv1.IngressBackend{
				Service: &networkingv1.IngressServiceBackend{Name: "default-svc"},
			},
			TLS: []networkingv1.IngressTLS{{Hosts: []string{"a.example.com"}}},
			Rules: []networkingv1.IngressRule{
				{
					Host: "a.example.com",
					IngressRuleValue: networkingv1.IngressRuleValue{
						HTTP: &networkingv1.HTTPIngressRuleValue{
							Paths: []networkingv1.HTTPIngressPath{
								{Path: "/", Backend: networkingv1.IngressBackend{Service: &networkingv1.IngressServiceBackend{Name: "checkout"}}},
								{Path: "/billing", Backend: networkingv1.IngressBackend{Service: &networkingv1.IngressServiceBackend{Name: "billing"}}},
								// duplicate of "checkout" — must be deduped.
								{Path: "/v2", Backend: networkingv1.IngressBackend{Service: &networkingv1.IngressServiceBackend{Name: "checkout"}}},
							},
						},
					},
				},
			},
		},
	}
	ev := entity.IngressToEvent(ing, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Ingress/team-a/main" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["ingress_class"] != "nginx" {
		t.Fatalf("ingress_class = %q", ev.Metadata["ingress_class"])
	}
	if ev.Metadata["rule_count"] != "1" {
		t.Fatalf("rule_count = %q", ev.Metadata["rule_count"])
	}
	if ev.Metadata["tls_count"] != "1" {
		t.Fatalf("tls_count = %q", ev.Metadata["tls_count"])
	}
	wantTargets := map[string]bool{
		"k8s_resource/99999999-8888-7777-6666-555555555555/Service/team-a/default-svc": false,
		"k8s_resource/99999999-8888-7777-6666-555555555555/Service/team-a/checkout":    false,
		"k8s_resource/99999999-8888-7777-6666-555555555555/Service/team-a/billing":     false,
	}
	if got := len(ev.TopologyEdges); got != 3 {
		t.Fatalf("expected 3 deduped edges, got %d (%v)", got, ev.TopologyEdges)
	}
	for _, e := range ev.TopologyEdges {
		if e.Kind != entity.EdgeKindRoutesTo {
			t.Fatalf("edge kind = %q", e.Kind)
		}
		if _, ok := wantTargets[e.TargetExternalID]; !ok {
			t.Fatalf("unexpected edge target %q", e.TargetExternalID)
		}
		wantTargets[e.TargetExternalID] = true
	}
}

func TestIngressToEvent_NoRulesEmitsZeroEdges(t *testing.T) {
	ing := &networkingv1.Ingress{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "empty"},
	}
	ev := entity.IngressToEvent(ing, entity.ActionDeleted, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.TopologyEdges) != 0 {
		t.Fatalf("expected 0 edges, got %d", len(ev.TopologyEdges))
	}
	if ev.Metadata["ingress_class"] != "" {
		t.Fatalf("ingress_class should be empty for nil ClassName, got %q", ev.Metadata["ingress_class"])
	}
}
