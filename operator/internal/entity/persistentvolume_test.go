package entity_test

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func newPV(name, storageClass string) *corev1.PersistentVolume {
	return &corev1.PersistentVolume{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: corev1.PersistentVolumeSpec{
			Capacity: corev1.ResourceList{
				corev1.ResourceStorage: resource.MustParse("10Gi"),
			},
			AccessModes: []corev1.PersistentVolumeAccessMode{
				corev1.ReadWriteOnce,
			},
			PersistentVolumeReclaimPolicy: corev1.PersistentVolumeReclaimRetain,
			StorageClassName:              storageClass,
		},
		Status: corev1.PersistentVolumeStatus{Phase: corev1.VolumeBound},
	}
}

func TestPersistentVolumeToEvent_HappyPath(t *testing.T) {
	pv := newPV("pv-001", "gp3")
	ev := entity.PersistentVolumeToEvent(pv, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/PersistentVolume/pv-001" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if strings.Contains(ev.ExternalID, "//") {
		t.Fatalf("external_id contains leading //: %q", ev.ExternalID)
	}
	expected := map[string]string{
		"cluster":        "prod-eu", "cluster_id": "99999999-8888-7777-6666-555555555555",
		"name":           "pv-001",
		"kind":           "PersistentVolume",
		"capacity":       "10Gi",
		"access_modes":   "ReadWriteOnce",
		"reclaim_policy": "Retain",
		"storage_class":  "gp3",
		"phase":          "Bound",
		"emitter":        "k8s-operator",
	}
	for k, v := range expected {
		if got := ev.Metadata[k]; got != v {
			t.Fatalf("metadata[%q] = %q, want %q", k, got, v)
		}
	}
}

func TestPersistentVolumeToEvent_AccessModesSorted(t *testing.T) {
	pv := newPV("pv-multi", "gp3")
	pv.Spec.AccessModes = []corev1.PersistentVolumeAccessMode{
		corev1.ReadWriteMany,
		corev1.ReadOnlyMany,
		corev1.ReadWriteOnce,
	}
	ev := entity.PersistentVolumeToEvent(pv, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if got := ev.Metadata["access_modes"]; got != "ReadOnlyMany,ReadWriteMany,ReadWriteOnce" {
		t.Fatalf("access_modes = %q, want sorted", got)
	}
}

func TestPersistentVolumeToEvent_NoStorageClassName_NoOfClassEdge(t *testing.T) {
	// Deprecated implicit-class case: spec.storageClassName is empty.
	// The mapper must NOT emit a dangling OF_CLASS edge to the empty
	// string — that would create a phantom StorageClass entity on the
	// consumer side.
	pv := newPV("pv-legacy", "")
	ev := entity.PersistentVolumeToEvent(pv, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if _, ok := ev.Metadata["storage_class"]; ok {
		t.Fatalf("storage_class metadata should be absent for empty storageClassName")
	}
	for _, e := range ev.TopologyEdges {
		if e.Kind == entity.RelationOfClass {
			t.Fatalf("OF_CLASS edge should be absent for empty storageClassName, got %+v", e)
		}
	}
}

func TestPersistentVolumeToEvent_EmitsInClusterAndOfClassEdges(t *testing.T) {
	pv := newPV("pv-001", "gp3")
	ev := entity.PersistentVolumeToEvent(pv, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	var sawInCluster, sawOfClass bool
	for _, e := range ev.TopologyEdges {
		switch e.Kind {
		case entity.RelationInCluster:
			sawInCluster = true
			if e.TargetExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Cluster/prod-eu" {
				t.Fatalf("in_cluster target = %q", e.TargetExternalID)
			}
		case entity.RelationOfClass:
			sawOfClass = true
			if e.TargetExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/StorageClass/gp3" {
				t.Fatalf("of_class target = %q", e.TargetExternalID)
			}
		}
	}
	if !sawInCluster {
		t.Fatalf("missing in_cluster edge: %+v", ev.TopologyEdges)
	}
	if !sawOfClass {
		t.Fatalf("missing of_class edge: %+v", ev.TopologyEdges)
	}
}
