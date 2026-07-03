package entity_test

import (
	"strings"
	"testing"

	storagev1 "k8s.io/api/storage/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/Omniscience/operator/internal/entity"
)

func TestStorageClassToEvent_HappyPath(t *testing.T) {
	retain := corev1.PersistentVolumeReclaimRetain
	sc := &storagev1.StorageClass{
		ObjectMeta: metav1.ObjectMeta{
			Name: "gp3",
			Labels: map[string]string{
				"adversarial.example.com/spy": "should-be-ignored",
			},
		},
		Provisioner: "ebs.csi.aws.com",
		Parameters: map[string]string{
			// User-supplied parameters must NOT leak into metadata; some
			// CSI drivers carry encryption keys here.
			"encrypted": "true",
			"kmsKeyId":  "arn:aws:kms:...:secret-key",
		},
		ReclaimPolicy: &retain,
	}
	ev := entity.StorageClassToEvent(sc, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/StorageClass/gp3" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if strings.Contains(ev.ExternalID, "//") {
		t.Fatalf("external_id contains leading //: %q", ev.ExternalID)
	}
	expected := map[string]string{
		"cluster":        "prod-eu", "cluster_id": "99999999-8888-7777-6666-555555555555",
		"name":           "gp3",
		"kind":           "StorageClass",
		"provisioner":    "ebs.csi.aws.com",
		"reclaim_policy": "Retain",
		"emitter":        "k8s-operator",
	}
	for k, v := range expected {
		if got := ev.Metadata[k]; got != v {
			t.Fatalf("metadata[%q] = %q, want %q", k, got, v)
		}
	}
	if len(ev.Metadata) != len(expected) {
		t.Fatalf("metadata has %d keys (%v), want exactly %d (closed allow-list)", len(ev.Metadata), ev.Metadata, len(expected))
	}
	// CSI driver parameters never copied into metadata (KMS key arn would
	// be a small disaster).
	for _, leaky := range []string{"encrypted", "kmsKeyId"} {
		if _, ok := ev.Metadata[leaky]; ok {
			t.Fatalf("metadata leaked CSI parameter %q", leaky)
		}
	}
}

func TestStorageClassToEvent_NoParametersNoReclaimPolicy(t *testing.T) {
	// Edge case: minimal StorageClass with no parameters and no
	// ReclaimPolicy pointer. Must not panic and must produce a
	// well-formed event.
	sc := &storagev1.StorageClass{
		ObjectMeta:  metav1.ObjectMeta{Name: "minimal"},
		Provisioner: "kubernetes.io/no-provisioner",
	}
	ev := entity.StorageClassToEvent(sc, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if _, ok := ev.Metadata["reclaim_policy"]; ok {
		t.Fatalf("reclaim_policy must be absent when ReclaimPolicy is nil")
	}
	if got := ev.Metadata["provisioner"]; got != "kubernetes.io/no-provisioner" {
		t.Fatalf("provisioner = %q", got)
	}
}

func TestStorageClassToEvent_EmitsInClusterEdge(t *testing.T) {
	sc := &storagev1.StorageClass{
		ObjectMeta:  metav1.ObjectMeta{Name: "gp3"},
		Provisioner: "ebs.csi.aws.com",
	}
	ev := entity.StorageClassToEvent(sc, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.TopologyEdges) != 1 {
		t.Fatalf("expected 1 edge, got %d", len(ev.TopologyEdges))
	}
	if ev.TopologyEdges[0].Kind != entity.RelationInCluster {
		t.Fatalf("relation = %q", ev.TopologyEdges[0].Kind)
	}
}
