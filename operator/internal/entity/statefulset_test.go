package entity_test

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func newStatefulSet(namespace, name string, replicas, available int32, owners []metav1.OwnerReference) *appsv1.StatefulSet {
	return &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:       namespace,
			Name:            name,
			Labels:          adversarialLabels,
			Annotations:     adversarialAnnotations,
			OwnerReferences: owners,
		},
		Spec: appsv1.StatefulSetSpec{
			Replicas: &replicas,
		},
		Status: appsv1.StatefulSetStatus{
			AvailableReplicas: available,
		},
	}
}

func TestStatefulSetToEvent_FieldsAreCorrect(t *testing.T) {
	ss := newStatefulSet("team-a", "kafka", 5, 4, nil)
	ev := entity.StatefulSetToEvent(ss, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if want := "k8s_resource/99999999-8888-7777-6666-555555555555/StatefulSet/team-a/kafka"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	if got := ev.Metadata["kind"]; got != "StatefulSet" {
		t.Fatalf("metadata[kind] = %q, want %q", got, "StatefulSet")
	}
	if got := ev.Metadata["replicas"]; got != "5" {
		t.Fatalf("metadata[replicas] = %q", got)
	}
	if got := ev.Metadata["available_replicas"]; got != "4" {
		t.Fatalf("metadata[available_replicas] = %q", got)
	}
}

func TestStatefulSetToEvent_OwnerEdgesFromOwnerReferences(t *testing.T) {
	owners := []metav1.OwnerReference{
		{Kind: "Application", Name: "kafka-app", UID: "app-uid"},
	}
	ss := newStatefulSet("team-a", "kafka", 1, 1, owners)
	ev := entity.StatefulSetToEvent(ss, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.Edges) != 1 || ev.Edges[0].FromKind != "Application" {
		t.Fatalf("edges = %#v, want 1 Application edge", ev.Edges)
	}
}

func TestStatefulSetToEvent_NoLabelLeakage(t *testing.T) {
	ss := newStatefulSet("team-a", "kafka", 1, 1, nil)
	ev := entity.StatefulSetToEvent(ss, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	for k := range ev.Metadata {
		switch k {
		case "cluster", "cluster_id", "namespace", "name", "kind", "emitter", "replicas", "available_replicas":
		default:
			t.Fatalf("leaked metadata key %q", k)
		}
	}
}
