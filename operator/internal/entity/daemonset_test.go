package entity_test

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/Omniscience/operator/internal/entity"
)

func newDaemonSet(namespace, name string, desired, available int32, owners []metav1.OwnerReference) *appsv1.DaemonSet {
	return &appsv1.DaemonSet{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:       namespace,
			Name:            name,
			Labels:          adversarialLabels,
			Annotations:     adversarialAnnotations,
			OwnerReferences: owners,
		},
		Status: appsv1.DaemonSetStatus{
			DesiredNumberScheduled: desired,
			NumberAvailable:        available,
		},
	}
}

func TestDaemonSetToEvent_FieldsAreCorrect(t *testing.T) {
	// DaemonSet has no spec.replicas; the operator surfaces the K8s status
	// fields renamed into the workload metadata vocabulary.
	ds := newDaemonSet("kube-system", "fluent-bit", 12, 11, nil)
	ev := entity.DaemonSetToEvent(ds, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if want := "k8s_resource/99999999-8888-7777-6666-555555555555/DaemonSet/kube-system/fluent-bit"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	if got := ev.Metadata["kind"]; got != "DaemonSet" {
		t.Fatalf("metadata[kind] = %q, want %q", got, "DaemonSet")
	}
	if got := ev.Metadata["replicas"]; got != "12" {
		t.Fatalf("metadata[replicas] = %q, want %q", got, "12")
	}
	if got := ev.Metadata["available_replicas"]; got != "11" {
		t.Fatalf("metadata[available_replicas] = %q, want %q", got, "11")
	}
}

func TestDaemonSetToEvent_OwnerEdgesFromOwnerReferences(t *testing.T) {
	owners := []metav1.OwnerReference{
		{Kind: "HelmRelease", Name: "logging-stack", UID: "rel-uid"},
	}
	ds := newDaemonSet("kube-system", "fluent-bit", 1, 1, owners)
	ev := entity.DaemonSetToEvent(ds, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.Edges) != 1 || ev.Edges[0].FromKind != "HelmRelease" {
		t.Fatalf("edges = %#v, want 1 HelmRelease edge", ev.Edges)
	}
}

func TestDaemonSetToEvent_ZeroAvailableIsEmitted(t *testing.T) {
	// Zero is meaningful for a DaemonSet (no Pods up yet) — must NOT be
	// elided from metadata.
	ds := newDaemonSet("kube-system", "fluent-bit", 0, 0, nil)
	ev := entity.DaemonSetToEvent(ds, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if got := ev.Metadata["replicas"]; got != "0" {
		t.Fatalf("metadata[replicas] = %q, want %q", got, "0")
	}
}
