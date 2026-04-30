package entity_test

import (
	"testing"

	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func TestNetworkPolicyToEvent_RendersSelectorAndPolicyTypes(t *testing.T) {
	np := &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "deny-all"},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{
				MatchLabels: map[string]string{"app": "checkout", "tier": "frontend"},
			},
			Ingress:     []networkingv1.NetworkPolicyIngressRule{{}},
			Egress:      []networkingv1.NetworkPolicyEgressRule{{}, {}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeEgress, networkingv1.PolicyTypeIngress},
		},
	}
	ev := entity.NetworkPolicyToEvent(np, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/NetworkPolicy/team-a/deny-all" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["pod_selector"] != "app=checkout,tier=frontend" {
		t.Fatalf("pod_selector = %q", ev.Metadata["pod_selector"])
	}
	if ev.Metadata["ingress_rule_count"] != "1" || ev.Metadata["egress_rule_count"] != "2" {
		t.Fatalf("ingress=%q egress=%q", ev.Metadata["ingress_rule_count"], ev.Metadata["egress_rule_count"])
	}
	if ev.Metadata["policy_types"] != "Egress,Ingress" {
		// Sorted alphabetically — see renderPolicyTypes doc.
		t.Fatalf("policy_types = %q, want sorted form 'Egress,Ingress'", ev.Metadata["policy_types"])
	}
	if len(ev.TopologyEdges) != 0 {
		t.Fatalf("NetworkPolicy must NOT emit edges in v0.2; got %d", len(ev.TopologyEdges))
	}
}

func TestNetworkPolicyToEvent_EmptySelectorRendersEmpty(t *testing.T) {
	np := &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "deny-everything"},
		Spec:       networkingv1.NetworkPolicySpec{},
	}
	ev := entity.NetworkPolicyToEvent(np, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if ev.Metadata["pod_selector"] != "" {
		t.Fatalf("empty selector should render as \"\"; got %q", ev.Metadata["pod_selector"])
	}
	if ev.Metadata["policy_types"] != "" {
		t.Fatalf("empty policy_types: %q", ev.Metadata["policy_types"])
	}
}
