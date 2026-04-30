package entity_test

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

// newReadyNode returns a Node fixture with a Ready condition and populated
// node-info fields. Tests modify the returned struct as needed.
func newReadyNode(name string) *corev1.Node {
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
			Labels: map[string]string{
				// ACL-hostile labels: must NOT leak into metadata.
				"topology.kubernetes.io/zone":   "us-east-1a",
				"node-role.kubernetes.io/worker": "",
				"adversarial.example.com/spy":   "should-be-ignored",
			},
		},
		Status: corev1.NodeStatus{
			Conditions: []corev1.NodeCondition{
				{Type: corev1.NodeReady, Status: corev1.ConditionTrue},
			},
			NodeInfo: corev1.NodeSystemInfo{
				KubeletVersion: "v1.31.1",
				OSImage:        "Ubuntu 22.04",
				Architecture:   "amd64",
			},
		},
	}
}

func TestNodeToEvent_HappyPath(t *testing.T) {
	node := newReadyNode("kind-control-plane")
	ev := entity.NodeToEvent(node, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Node/kind-control-plane" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if strings.Contains(ev.ExternalID, "//") {
		t.Fatalf("external_id contains leading // (cluster-scoped resources have no namespace component): %q", ev.ExternalID)
	}
	if got := ev.Metadata["ready"]; got != "true" {
		t.Fatalf("ready = %q, want \"true\"", got)
	}
	for _, k := range []string{"kubelet_version", "os_image", "arch"} {
		if _, ok := ev.Metadata[k]; !ok {
			t.Fatalf("metadata missing %q", k)
		}
	}
	// Allow-list assertion: closed set, no labels.
	for k := range ev.Metadata {
		if strings.HasPrefix(k, "topology.") || strings.HasPrefix(k, "node-role.") || strings.HasPrefix(k, "adversarial.") {
			t.Fatalf("metadata leaked label-shaped key %q", k)
		}
	}
}

func TestNodeToEvent_NotReadyConditionMissing(t *testing.T) {
	node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: "node-x"}}
	ev := entity.NodeToEvent(node, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if got := ev.Metadata["ready"]; got != "false" {
		t.Fatalf("ready = %q, want \"false\" (no Ready condition present)", got)
	}
}

func TestNodeToEvent_NoTaintsNoLabelsStillEmits(t *testing.T) {
	// Edge case: a Node with literally no taints, labels, or status
	// fields populated. Must still produce a well-formed event.
	node := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: "minimal"}}
	ev := entity.NodeToEvent(node, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Node/minimal" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if _, ok := ev.Metadata["kubelet_version"]; ok {
		t.Fatalf("kubelet_version should be absent when not populated, got %q", ev.Metadata["kubelet_version"])
	}
}

func TestNodeToEvent_EmitsInClusterEdge(t *testing.T) {
	node := newReadyNode("node-a")
	ev := entity.NodeToEvent(node, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.TopologyEdges) != 1 {
		t.Fatalf("expected 1 edge, got %d: %+v", len(ev.TopologyEdges), ev.TopologyEdges)
	}
	e := ev.TopologyEdges[0]
	if e.Kind != entity.RelationInCluster {
		t.Fatalf("relation = %q, want %q", e.Kind, entity.RelationInCluster)
	}
	if e.TargetExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Cluster/prod-eu" {
		t.Fatalf("target = %q, want %q", e.TargetExternalID, "k8s_resource/99999999-8888-7777-6666-555555555555/Cluster/prod-eu")
	}
}
