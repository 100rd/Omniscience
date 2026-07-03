package entity_test

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/Omniscience/operator/internal/entity"
)

func TestNamespaceToEvent_HappyPath(t *testing.T) {
	ns := &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{
			Name: "team-a",
			Labels: map[string]string{
				"pod-security.kubernetes.io/enforce": "restricted",
				"adversarial.example.com/spy":        "should-be-ignored",
			},
		},
		Status: corev1.NamespaceStatus{Phase: corev1.NamespaceActive},
	}
	ev := entity.NamespaceToEvent(ns, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Namespace/team-a" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if strings.Contains(ev.ExternalID, "//") {
		t.Fatalf("external_id contains leading //: %q", ev.ExternalID)
	}
	expected := map[string]string{
		"cluster": "prod-eu", "cluster_id": "99999999-8888-7777-6666-555555555555",
		"name":    "team-a",
		"kind":    "Namespace",
		"phase":   "Active",
		"emitter": "k8s-operator",
	}
	for k, v := range expected {
		if got := ev.Metadata[k]; got != v {
			t.Fatalf("metadata[%q] = %q, want %q", k, got, v)
		}
	}
	if len(ev.Metadata) != len(expected) {
		t.Fatalf("metadata has %d keys (%v), want exactly %d (closed allow-list)", len(ev.Metadata), ev.Metadata, len(expected))
	}
}

func TestNamespaceToEvent_NoLabelsStillEmits(t *testing.T) {
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "default"}}
	ev := entity.NamespaceToEvent(ns, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Namespace/default" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
}

func TestNamespaceToEvent_EmitsInClusterEdge(t *testing.T) {
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "team-a"}}
	ev := entity.NamespaceToEvent(ns, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.TopologyEdges) != 1 {
		t.Fatalf("expected 1 edge, got %d", len(ev.TopologyEdges))
	}
	if ev.TopologyEdges[0].Kind != entity.RelationInCluster {
		t.Fatalf("relation = %q", ev.TopologyEdges[0].Kind)
	}
	if ev.TopologyEdges[0].TargetExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Cluster/prod-eu" {
		t.Fatalf("target = %q", ev.TopologyEdges[0].TargetExternalID)
	}
}
