package entity_test

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/Omniscience/operator/internal/entity"
)

// adversarialLabels is a fixture that simulates a tenant attempting to
// inject workspace-affecting state via labels/annotations. Every test in
// this file decorates fixtures with this map and asserts that NONE of the
// keys end up in the emitted Event. Per ADR-0007 §ACL labels and
// annotations are tenant-writable and must never influence tenancy.
var adversarialLabels = map[string]string{
	"omniscience.io/workspace":     "evil-workspace",
	"tenant":                       "other",
	"app":                          "frontend",
	"adversarial.example.com/spy":  "should-be-ignored",
}

// adversarialAnnotations mirrors adversarialLabels for the annotation-side
// guard. Any leak surfaces here as a metadata key the closed allow-list
// does not name.
var adversarialAnnotations = map[string]string{
	"kubectl.kubernetes.io/last-applied-configuration": `{"x":1}`,
	"adversarial.example.com/leak":                     "no",
}

func newDeployment(namespace, name string, replicas, available int32, owners []metav1.OwnerReference) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:       namespace,
			Name:            name,
			Labels:          adversarialLabels,
			Annotations:     adversarialAnnotations,
			OwnerReferences: owners,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
		},
		Status: appsv1.DeploymentStatus{
			AvailableReplicas: available,
		},
	}
}

func TestDeploymentToEvent_FieldsAreCorrect(t *testing.T) {
	d := newDeployment("team-a", "checkout", 3, 2, nil)
	ev := entity.DeploymentToEvent(d, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.SourceType != entity.SourceType {
		t.Fatalf("source_type = %q, want %q", ev.SourceType, entity.SourceType)
	}
	if ev.WorkspaceID != fixedWorkspace {
		t.Fatalf("workspace_id = %s, want %s", ev.WorkspaceID, fixedWorkspace)
	}
	if ev.Action != entity.ActionCreated {
		t.Fatalf("action = %q, want %q", ev.Action, entity.ActionCreated)
	}
	if want := "k8s_resource/99999999-8888-7777-6666-555555555555/Deployment/team-a/checkout"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	if want := "kube://prod-eu/team-a/Deployment/checkout"; ev.URI != want {
		t.Fatalf("uri = %q, want %q", ev.URI, want)
	}
	if !ev.EmittedAt.Equal(fixedNow) {
		t.Fatalf("emitted_at = %s, want %s", ev.EmittedAt, fixedNow)
	}
	if got := ev.Metadata["replicas"]; got != "3" {
		t.Fatalf("metadata[replicas] = %q, want %q", got, "3")
	}
	if got := ev.Metadata["available_replicas"]; got != "2" {
		t.Fatalf("metadata[available_replicas] = %q, want %q", got, "2")
	}
}

func TestDeploymentToEvent_DoesNotLeakUserLabelsOrAnnotations(t *testing.T) {
	d := newDeployment("team-a", "checkout", 3, 2, nil)
	ev := entity.DeploymentToEvent(d, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	expected := map[string]string{
		"cluster":            "prod-eu", "cluster_id": "99999999-8888-7777-6666-555555555555",
		"namespace":          "team-a",
		"name":               "checkout",
		"kind":               "Deployment",
		"emitter":            "k8s-operator",
		"replicas":           "3",
		"available_replicas": "2",
	}
	if len(ev.Metadata) != len(expected) {
		t.Fatalf("metadata has %d keys (%v), want %d (%v)", len(ev.Metadata), ev.Metadata, len(expected), expected)
	}
	for k, v := range expected {
		if got, ok := ev.Metadata[k]; !ok || got != v {
			t.Fatalf("metadata[%q] = %q, want %q", k, got, v)
		}
	}
}

func TestDeploymentToEvent_AdversarialWorkspaceLabelIgnored(t *testing.T) {
	// ACL invariant: a tenant who labels their resource
	// "omniscience.io/workspace=evil" must not flip the event's
	// workspace_id. The mapper takes the tenant boundary as a parameter
	// (operator config) and never reads it from the resource.
	d := newDeployment("team-a", "checkout", 1, 1, nil)
	ev := entity.DeploymentToEvent(d, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.WorkspaceID != fixedWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label was honoured!", ev.WorkspaceID, fixedWorkspace)
	}
}

func TestDeploymentToEvent_OwnerEdgesFromOwnerReferences(t *testing.T) {
	// Edge-from-owners: a Deployment can be owned by a higher-level CRD
	// (Argo Application etc.). Edges come ONLY from K8s-populated
	// ownerReferences — never invented.
	owners := []metav1.OwnerReference{
		{
			APIVersion: "argoproj.io/v1alpha1",
			Kind:       "Application",
			Name:       "checkout-app",
			UID:        "00000000-1111-2222-3333-444444444444",
		},
	}
	d := newDeployment("team-a", "checkout", 3, 3, owners)
	ev := entity.DeploymentToEvent(d, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if len(ev.Edges) != 1 {
		t.Fatalf("edges count = %d, want 1; edges = %#v", len(ev.Edges), ev.Edges)
	}
	got := ev.Edges[0]
	if got.Relation != entity.EdgeRelationOwns {
		t.Fatalf("edge relation = %q, want %q", got.Relation, entity.EdgeRelationOwns)
	}
	if got.FromKind != "Application" {
		t.Fatalf("edge from_kind = %q, want %q", got.FromKind, "Application")
	}
	wantFrom := "k8s_resource/99999999-8888-7777-6666-555555555555/Application/team-a/checkout-app"
	if got.FromExternalID != wantFrom {
		t.Fatalf("edge from_external_id = %q, want %q", got.FromExternalID, wantFrom)
	}
	if got.ToExternalID != ev.ExternalID {
		t.Fatalf("edge to_external_id = %q, want %q (the watched resource)", got.ToExternalID, ev.ExternalID)
	}
	if got.FromUID != "00000000-1111-2222-3333-444444444444" {
		t.Fatalf("edge from_uid = %q, want %q", got.FromUID, "00000000-1111-2222-3333-444444444444")
	}
}

func TestDeploymentToEvent_NoOwnerReferencesEmitsNoEdges(t *testing.T) {
	d := newDeployment("team-a", "checkout", 3, 3, nil)
	ev := entity.DeploymentToEvent(d, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.Edges) != 0 {
		t.Fatalf("edges = %#v, want empty (no ownerReferences)", ev.Edges)
	}
}

func TestDeploymentToEvent_MultipleOwnerReferences(t *testing.T) {
	// K8s allows multiple ownerReferences (rare but legal). Each becomes
	// its own edge.
	owners := []metav1.OwnerReference{
		{Kind: "Application", Name: "app1", UID: "u1"},
		{Kind: "HelmRelease", Name: "rel1", UID: "u2"},
	}
	d := newDeployment("team-a", "checkout", 1, 1, owners)
	ev := entity.DeploymentToEvent(d, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.Edges) != 2 {
		t.Fatalf("edges count = %d, want 2; got %#v", len(ev.Edges), ev.Edges)
	}
}

func TestDeploymentToEvent_DeletionStubEmitsCorrectExternalID(t *testing.T) {
	// On a delete reconcile the controller passes a stub object with only
	// namespace + name. The mapper must still produce a well-formed event.
	stub := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "checkout",
		},
	}
	ev := entity.DeploymentToEvent(stub, entity.ActionDeleted, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.Action != entity.ActionDeleted {
		t.Fatalf("action = %q, want %q", ev.Action, entity.ActionDeleted)
	}
	if want := "k8s_resource/99999999-8888-7777-6666-555555555555/Deployment/team-a/checkout"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	if _, hasReplicas := ev.Metadata["replicas"]; hasReplicas {
		t.Fatalf("metadata should not carry replicas on a stub: %v", ev.Metadata)
	}
}

func TestDeploymentToEvent_NamespacelessFallsBackSafely(t *testing.T) {
	stub := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "rogue"},
	}
	ev := entity.DeploymentToEvent(stub, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if want := "k8s_resource/99999999-8888-7777-6666-555555555555/Deployment/default/rogue"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
}

func TestDeploymentToEvent_MalformedOwnerReferenceSkipped(t *testing.T) {
	// Defensive: an ownerReference with empty Kind or Name cannot be
	// converted to a well-formed external-id. The mapper skips it rather
	// than emit garbage.
	owners := []metav1.OwnerReference{
		{Kind: "", Name: "no-kind", UID: "u1"},
		{Kind: "ReplicaSet", Name: "", UID: "u2"},
		{Kind: "Application", Name: "ok", UID: "u3"},
	}
	d := newDeployment("team-a", "checkout", 1, 1, owners)
	ev := entity.DeploymentToEvent(d, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.Edges) != 1 {
		t.Fatalf("edges count = %d, want 1 (malformed entries skipped); got %#v", len(ev.Edges), ev.Edges)
	}
	if ev.Edges[0].FromKind != "Application" {
		t.Fatalf("kept edge has from_kind = %q, want %q", ev.Edges[0].FromKind, "Application")
	}
}

func TestDeploymentToEvent_NilSpecReplicasOmittedFromMetadata(t *testing.T) {
	// spec.replicas is *int32 — nil means "K8s default 1" but the operator
	// surfaces only what the resource itself says. Nil → key omitted.
	d := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "checkout"},
		Status:     appsv1.DeploymentStatus{AvailableReplicas: 0},
	}
	ev := entity.DeploymentToEvent(d, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if _, ok := ev.Metadata["replicas"]; ok {
		t.Fatalf("metadata[replicas] should be absent when spec.replicas is nil; got %q", ev.Metadata["replicas"])
	}
	if got := ev.Metadata["available_replicas"]; got != "0" {
		t.Fatalf("metadata[available_replicas] = %q, want %q", got, "0")
	}
}

