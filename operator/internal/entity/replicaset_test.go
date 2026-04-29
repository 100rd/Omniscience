package entity_test

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func newReplicaSet(namespace, name string, replicas, available int32, owners []metav1.OwnerReference) *appsv1.ReplicaSet {
	return &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:       namespace,
			Name:            name,
			Labels:          adversarialLabels,
			Annotations:     adversarialAnnotations,
			OwnerReferences: owners,
		},
		Spec: appsv1.ReplicaSetSpec{
			Replicas: &replicas,
		},
		Status: appsv1.ReplicaSetStatus{
			AvailableReplicas: available,
		},
	}
}

func TestReplicaSetToEvent_FieldsAreCorrect(t *testing.T) {
	rs := newReplicaSet("team-a", "checkout-7d9f", 4, 4, nil)
	ev := entity.ReplicaSetToEvent(rs, entity.ActionUpdated, fixedWorkspace, "prod-eu", fixedNow)

	if want := "k8s_resource/ReplicaSet/team-a/checkout-7d9f"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	if want := "kube://prod-eu/team-a/ReplicaSet/checkout-7d9f"; ev.URI != want {
		t.Fatalf("uri = %q, want %q", ev.URI, want)
	}
	if got := ev.Metadata["kind"]; got != "ReplicaSet" {
		t.Fatalf("metadata[kind] = %q, want %q", got, "ReplicaSet")
	}
	if got := ev.Metadata["replicas"]; got != "4" {
		t.Fatalf("metadata[replicas] = %q, want %q", got, "4")
	}
	if got := ev.Metadata["available_replicas"]; got != "4" {
		t.Fatalf("metadata[available_replicas] = %q, want %q", got, "4")
	}
}

func TestReplicaSetToEvent_OwnerEdgeFromDeployment(t *testing.T) {
	// Canonical chain: Deployment -[OWNS]-> ReplicaSet. The K8s
	// control plane writes ownerReferences on the ReplicaSet pointing back
	// to the Deployment; this is where the operator picks the edge up.
	owners := []metav1.OwnerReference{
		{
			APIVersion: "apps/v1",
			Kind:       "Deployment",
			Name:       "checkout",
			UID:        "dep-uid-1",
		},
	}
	rs := newReplicaSet("team-a", "checkout-7d9f", 4, 4, owners)
	ev := entity.ReplicaSetToEvent(rs, entity.ActionCreated, fixedWorkspace, "prod-eu", fixedNow)

	if len(ev.Edges) != 1 {
		t.Fatalf("edges count = %d, want 1; %#v", len(ev.Edges), ev.Edges)
	}
	if got := ev.Edges[0].FromExternalID; got != "k8s_resource/Deployment/team-a/checkout" {
		t.Fatalf("edge from_external_id = %q", got)
	}
	if got := ev.Edges[0].ToExternalID; got != ev.ExternalID {
		t.Fatalf("edge to_external_id = %q, want %q", got, ev.ExternalID)
	}
	if got := ev.Edges[0].Relation; got != entity.EdgeRelationOwns {
		t.Fatalf("edge relation = %q, want %q", got, entity.EdgeRelationOwns)
	}
	if got := ev.Edges[0].FromUID; got != "dep-uid-1" {
		t.Fatalf("edge from_uid = %q", got)
	}
}

func TestReplicaSetToEvent_AdversarialLabelDoesNotChangeWorkspace(t *testing.T) {
	rs := newReplicaSet("team-a", "checkout-7d9f", 1, 1, nil)
	ev := entity.ReplicaSetToEvent(rs, entity.ActionCreated, fixedWorkspace, "prod-eu", fixedNow)
	if ev.WorkspaceID != fixedWorkspace {
		t.Fatalf("workspace_id = %s, want %s", ev.WorkspaceID, fixedWorkspace)
	}
	for k := range ev.Metadata {
		switch k {
		case "cluster", "namespace", "name", "kind", "emitter", "replicas", "available_replicas":
			// allowed
		default:
			t.Fatalf("metadata leaked unauthorised key %q (full map: %v)", k, ev.Metadata)
		}
	}
}
