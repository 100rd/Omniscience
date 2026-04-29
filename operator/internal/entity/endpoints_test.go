package entity_test

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func TestEndpointsToEvent_BuildsSelectsEdges(t *testing.T) {
	ep := &corev1.Endpoints{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "checkout"},
		Subsets: []corev1.EndpointSubset{
			{
				Addresses: []corev1.EndpointAddress{
					{IP: "10.0.0.1", TargetRef: &corev1.ObjectReference{Kind: "Pod", Namespace: "team-a", Name: "checkout-7d9f"}},
					{IP: "10.0.0.2", TargetRef: &corev1.ObjectReference{Kind: "Pod", Namespace: "team-a", Name: "checkout-aa11"}},
				},
				NotReadyAddresses: []corev1.EndpointAddress{
					{IP: "10.0.0.3", TargetRef: &corev1.ObjectReference{Kind: "Pod", Namespace: "team-a", Name: "checkout-bb22"}},
				},
			},
		},
	}
	ev := entity.EndpointsToEvent(ep, entity.ActionUpdated, fixedWorkspace, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/Endpoints/team-a/checkout" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["subset_count"] != "1" {
		t.Fatalf("subset_count = %q", ev.Metadata["subset_count"])
	}
	if ev.Metadata["address_count"] != "3" {
		t.Fatalf("address_count = %q", ev.Metadata["address_count"])
	}
	if got := len(ev.TopologyEdges); got != 3 {
		t.Fatalf("edges count = %d, want 3", got)
	}
	wantTargets := map[string]bool{
		"k8s_resource/Pod/team-a/checkout-7d9f": false,
		"k8s_resource/Pod/team-a/checkout-aa11": false,
		"k8s_resource/Pod/team-a/checkout-bb22": false,
	}
	for _, e := range ev.TopologyEdges {
		if e.Kind != entity.EdgeKindSelects {
			t.Fatalf("edge kind = %q, want %q", e.Kind, entity.EdgeKindSelects)
		}
		if _, ok := wantTargets[e.TargetExternalID]; !ok {
			t.Fatalf("unexpected edge target %q", e.TargetExternalID)
		}
		wantTargets[e.TargetExternalID] = true
	}
	for ext, seen := range wantTargets {
		if !seen {
			t.Fatalf("expected edge to %q not emitted", ext)
		}
	}
}

func TestEndpointsToEvent_NoSubsetsEmitsZeroEdges(t *testing.T) {
	ep := &corev1.Endpoints{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "headless"},
	}
	ev := entity.EndpointsToEvent(ep, entity.ActionUpdated, fixedWorkspace, "prod-eu", fixedNow)
	if len(ev.TopologyEdges) != 0 {
		t.Fatalf("expected 0 edges, got %d", len(ev.TopologyEdges))
	}
	if ev.Metadata["subset_count"] != "0" || ev.Metadata["address_count"] != "0" {
		t.Fatalf("zero counts: subset=%q address=%q", ev.Metadata["subset_count"], ev.Metadata["address_count"])
	}
}

func TestEndpointsToEvent_SkipsNonPodTargetRefs(t *testing.T) {
	// Some legacy clusters (or operators producing custom Endpoints) put non-
	// Pod targetRefs in subsets. We must only emit Pod edges so the resulting
	// graph stays well-typed.
	ep := &corev1.Endpoints{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "weird"},
		Subsets: []corev1.EndpointSubset{
			{
				Addresses: []corev1.EndpointAddress{
					{IP: "10.0.0.1"}, // no targetRef
					{IP: "10.0.0.2", TargetRef: &corev1.ObjectReference{Kind: "Service", Name: "external"}},
					{IP: "10.0.0.3", TargetRef: &corev1.ObjectReference{Kind: "Pod", Name: "real-pod"}},
				},
			},
		},
	}
	ev := entity.EndpointsToEvent(ep, entity.ActionUpdated, fixedWorkspace, "prod-eu", fixedNow)
	if len(ev.TopologyEdges) != 1 {
		t.Fatalf("expected 1 edge, got %d", len(ev.TopologyEdges))
	}
	if ev.TopologyEdges[0].TargetExternalID != "k8s_resource/Pod/team-a/real-pod" {
		t.Fatalf("edge target = %q", ev.TopologyEdges[0].TargetExternalID)
	}
}
