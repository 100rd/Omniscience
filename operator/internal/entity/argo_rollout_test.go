package entity_test

import (
	"testing"

	rolloutsv1alpha1 "github.com/argoproj/argo-rollouts/pkg/apis/rollouts/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/Omniscience/operator/internal/entity"
)

// rolloutFixture is a small builder so each scenario reads as a flat list of
// the fields it actually asserts on. Adversarial labels/annotations are
// stamped in on every fixture; tests verify they never leak through.
func rolloutFixture(
	namespace, name string,
	strategy *rolloutsv1alpha1.RolloutStrategy,
	status rolloutsv1alpha1.RolloutStatus,
	owners []metav1.OwnerReference,
) *rolloutsv1alpha1.Rollout {
	r := &rolloutsv1alpha1.Rollout{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:       namespace,
			Name:            name,
			Labels:          adversarialLabels,
			Annotations:     adversarialAnnotations,
			OwnerReferences: owners,
		},
		Status: status,
	}
	if strategy != nil {
		r.Spec.Strategy = *strategy
	}
	return r
}

// findEdge returns the first EdgeRef whose Kind matches; nil if none.
func findEdge(edges []entity.EdgeRef, kind string) *entity.EdgeRef {
	for i := range edges {
		if edges[i].Kind == kind {
			return &edges[i]
		}
	}
	return nil
}

func countEdges(edges []entity.EdgeRef, kind string) int {
	n := 0
	for _, e := range edges {
		if e.Kind == kind {
			n++
		}
	}
	return n
}

// ---------------------------------------------------------------------------
// Scenario 1 — Canary, healthy steady state.
//
// status.phase == Healthy, currentPodHash == stableRS. Expected edges:
//   - OWNS            -> rs whose hash equals stableRS (single RS)
//   - CURRENT_VERSION -> same rs
//
// No CANARY_VERSION (steady state). No BLUEGREEN_ACTIVE (canary strategy).
// ---------------------------------------------------------------------------
func TestRolloutToEvent_CanaryHealthySteadyState(t *testing.T) {
	r := rolloutFixture(
		"team-a", "checkout",
		&rolloutsv1alpha1.RolloutStrategy{
			Canary: &rolloutsv1alpha1.CanaryStrategy{
				CanaryService: "checkout-canary",
				StableService: "checkout-stable",
			},
		},
		rolloutsv1alpha1.RolloutStatus{
			Phase:             rolloutsv1alpha1.RolloutPhaseHealthy,
			StableRS:          "abc123",
			CurrentPodHash:    "abc123",
			Replicas:          5,
			UpdatedReplicas:   5,
			ReadyReplicas:     5,
			AvailableReplicas: 5,
		},
		nil,
	)

	ev := entity.RolloutToEvent(r, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.WorkspaceID != fixedWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, fixedWorkspace)
	}
	if want := "k8s_resource/99999999-8888-7777-6666-555555555555/Rollout/team-a/checkout"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	if got := ev.Metadata["strategy"]; got != "canary" {
		t.Fatalf("metadata[strategy] = %q, want %q", got, "canary")
	}
	if got := ev.Metadata["phase"]; got != "Healthy" {
		t.Fatalf("metadata[phase] = %q, want %q", got, "Healthy")
	}
	if got := ev.Metadata["replicas"]; got != "5" {
		t.Fatalf("metadata[replicas] = %q, want %q", got, "5")
	}
	if got := ev.Metadata["available_replicas"]; got != "5" {
		t.Fatalf("metadata[available_replicas] = %q, want %q", got, "5")
	}
	if got := ev.Metadata["current_pod_hash"]; got != "abc123" {
		t.Fatalf("metadata[current_pod_hash] = %q, want %q", got, "abc123")
	}

	// Edges: exactly one OWNS + one CURRENT_VERSION, both pointing at the
	// stable RS. No CANARY_VERSION (steady state).
	owns := countEdges(ev.TopologyEdges, "OWNS")
	cur := countEdges(ev.TopologyEdges, "CURRENT_VERSION")
	can := countEdges(ev.TopologyEdges, "CANARY_VERSION")
	bg := countEdges(ev.TopologyEdges, "BLUEGREEN_ACTIVE")
	if owns != 1 || cur != 1 || can != 0 || bg != 0 {
		t.Fatalf("edge counts owns=%d cur=%d can=%d bg=%d, want 1/1/0/0; edges=%#v",
			owns, cur, can, bg, ev.TopologyEdges)
	}
	wantTarget := "k8s_resource/99999999-8888-7777-6666-555555555555/ReplicaSet/team-a/checkout-abc123"
	if e := findEdge(ev.TopologyEdges, "OWNS"); e == nil || e.TargetExternalID != wantTarget {
		t.Fatalf("OWNS edge target = %v, want %q", e, wantTarget)
	}
	if e := findEdge(ev.TopologyEdges, "CURRENT_VERSION"); e == nil || e.TargetExternalID != wantTarget {
		t.Fatalf("CURRENT_VERSION edge target = %v, want %q", e, wantTarget)
	}

	// ACL invariant: no adversarial labels/annotations leaked through.
	for k := range ev.Metadata {
		if _, isAdvLabel := adversarialLabels[k]; isAdvLabel {
			t.Fatalf("metadata leaked adversarial label key %q", k)
		}
		if _, isAdvAnno := adversarialAnnotations[k]; isAdvAnno {
			t.Fatalf("metadata leaked adversarial annotation key %q", k)
		}
	}
}

// ---------------------------------------------------------------------------
// Scenario 2 — Canary mid-flight (Progressing).
//
// status.phase == Progressing, stableRS != currentPodHash. Expected edges:
//   - OWNS            -> stable RS
//   - OWNS            -> candidate RS (the "new" version being canaried)
//   - CURRENT_VERSION -> candidate RS (currentPodHash)
//   - CANARY_VERSION  -> candidate RS
//
// current_step_index is surfaced.
// ---------------------------------------------------------------------------
func TestRolloutToEvent_CanaryMidFlight(t *testing.T) {
	stepIdx := int32(2)
	r := rolloutFixture(
		"team-a", "checkout",
		&rolloutsv1alpha1.RolloutStrategy{
			Canary: &rolloutsv1alpha1.CanaryStrategy{
				CanaryService: "checkout-canary",
				StableService: "checkout-stable",
			},
		},
		rolloutsv1alpha1.RolloutStatus{
			Phase:             rolloutsv1alpha1.RolloutPhaseProgressing,
			StableRS:          "abc123",
			CurrentPodHash:    "def456",
			CurrentStepIndex:  &stepIdx,
			Replicas:          6,
			UpdatedReplicas:   2,
			ReadyReplicas:     5,
			AvailableReplicas: 5,
		},
		nil,
	)

	ev := entity.RolloutToEvent(r, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if got := ev.Metadata["strategy"]; got != "canary" {
		t.Fatalf("metadata[strategy] = %q, want %q", got, "canary")
	}
	if got := ev.Metadata["phase"]; got != "Progressing" {
		t.Fatalf("metadata[phase] = %q, want %q", got, "Progressing")
	}
	if got := ev.Metadata["current_step_index"]; got != "2" {
		t.Fatalf("metadata[current_step_index] = %q, want %q", got, "2")
	}

	// Two distinct OWNS edges (stable + candidate).
	if got := countEdges(ev.TopologyEdges, "OWNS"); got != 2 {
		t.Fatalf("OWNS edge count = %d, want 2; edges=%#v", got, ev.TopologyEdges)
	}
	wantStable := "k8s_resource/99999999-8888-7777-6666-555555555555/ReplicaSet/team-a/checkout-abc123"
	wantCandidate := "k8s_resource/99999999-8888-7777-6666-555555555555/ReplicaSet/team-a/checkout-def456"

	// Confirm both stable + candidate appear among OWNS targets.
	seen := map[string]bool{}
	for _, e := range ev.TopologyEdges {
		if e.Kind == "OWNS" {
			seen[e.TargetExternalID] = true
		}
	}
	if !seen[wantStable] || !seen[wantCandidate] {
		t.Fatalf("OWNS targets missing one of {stable,candidate}: %v", seen)
	}

	// CURRENT_VERSION points at the candidate (currentPodHash).
	if e := findEdge(ev.TopologyEdges, "CURRENT_VERSION"); e == nil || e.TargetExternalID != wantCandidate {
		t.Fatalf("CURRENT_VERSION target = %v, want %q", e, wantCandidate)
	}
	// CANARY_VERSION points at the candidate.
	if e := findEdge(ev.TopologyEdges, "CANARY_VERSION"); e == nil || e.TargetExternalID != wantCandidate {
		t.Fatalf("CANARY_VERSION target = %v, want %q", e, wantCandidate)
	}
	// No BlueGreen edge.
	if got := countEdges(ev.TopologyEdges, "BLUEGREEN_ACTIVE"); got != 0 {
		t.Fatalf("BLUEGREEN_ACTIVE count = %d, want 0", got)
	}
}

// ---------------------------------------------------------------------------
// Scenario 3 — Canary paused.
//
// Paused mid-flight: the candidate is held but the rollout is not making
// progress. Edge shape mirrors Progressing — phase metadata is the only
// difference. Verifies the mapper does not gate edge emission on phase.
// ---------------------------------------------------------------------------
func TestRolloutToEvent_CanaryPaused(t *testing.T) {
	stepIdx := int32(3)
	r := rolloutFixture(
		"team-a", "checkout",
		&rolloutsv1alpha1.RolloutStrategy{
			Canary: &rolloutsv1alpha1.CanaryStrategy{StableService: "checkout"},
		},
		rolloutsv1alpha1.RolloutStatus{
			Phase:            rolloutsv1alpha1.RolloutPhasePaused,
			StableRS:         "stable1",
			CurrentPodHash:   "candidate2",
			CurrentStepIndex: &stepIdx,
		},
		nil,
	)

	ev := entity.RolloutToEvent(r, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if got := ev.Metadata["phase"]; got != "Paused" {
		t.Fatalf("metadata[phase] = %q, want %q", got, "Paused")
	}
	// Same edge expectations as mid-flight: 2x OWNS, 1x CURRENT_VERSION,
	// 1x CANARY_VERSION.
	if got := countEdges(ev.TopologyEdges, "OWNS"); got != 2 {
		t.Fatalf("OWNS count = %d, want 2", got)
	}
	if got := countEdges(ev.TopologyEdges, "CANARY_VERSION"); got != 1 {
		t.Fatalf("CANARY_VERSION count = %d, want 1", got)
	}
	if got := ev.Metadata["current_step_index"]; got != "3" {
		t.Fatalf("current_step_index = %q, want 3", got)
	}
}

// ---------------------------------------------------------------------------
// Scenario 4 — Degraded.
//
// status.phase == Degraded. Verifies the mapper carries the degraded phase
// metadata even when stableRS / currentPodHash are equal (the rollout is
// stuck at the stable version because the canary failed analysis).
// ---------------------------------------------------------------------------
func TestRolloutToEvent_CanaryDegraded(t *testing.T) {
	r := rolloutFixture(
		"team-a", "checkout",
		&rolloutsv1alpha1.RolloutStrategy{
			Canary: &rolloutsv1alpha1.CanaryStrategy{},
		},
		rolloutsv1alpha1.RolloutStatus{
			Phase:          rolloutsv1alpha1.RolloutPhaseDegraded,
			StableRS:       "abc123",
			CurrentPodHash: "abc123",
		},
		nil,
	)

	ev := entity.RolloutToEvent(r, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if got := ev.Metadata["phase"]; got != "Degraded" {
		t.Fatalf("metadata[phase] = %q, want %q", got, "Degraded")
	}
	// Steady state edge shape (stable == current).
	if got := countEdges(ev.TopologyEdges, "OWNS"); got != 1 {
		t.Fatalf("OWNS count = %d, want 1", got)
	}
	if got := countEdges(ev.TopologyEdges, "CANARY_VERSION"); got != 0 {
		t.Fatalf("CANARY_VERSION count = %d, want 0 (stable == current)", got)
	}
}

// ---------------------------------------------------------------------------
// Scenario 5 — BlueGreen mid-flight.
//
// strategy = BlueGreen, status.phase == Progressing. Expected:
//   - metadata[strategy] == "blueGreen"
//   - OWNS              -> stable RS
//   - CURRENT_VERSION   -> RS at currentPodHash
//   - BLUEGREEN_ACTIVE  -> Service named in spec.strategy.blueGreen.activeService
//   - NO CANARY_VERSION edge (different strategy)
// ---------------------------------------------------------------------------
func TestRolloutToEvent_BlueGreenMidFlight(t *testing.T) {
	r := rolloutFixture(
		"team-a", "checkout",
		&rolloutsv1alpha1.RolloutStrategy{
			BlueGreen: &rolloutsv1alpha1.BlueGreenStrategy{
				ActiveService:  "checkout-active",
				PreviewService: "checkout-preview",
			},
		},
		rolloutsv1alpha1.RolloutStatus{
			Phase:             rolloutsv1alpha1.RolloutPhaseProgressing,
			StableRS:          "blue1",
			CurrentPodHash:    "green2",
			Replicas:          4,
			UpdatedReplicas:   4,
			ReadyReplicas:     4,
			AvailableReplicas: 4,
		},
		nil,
	)

	ev := entity.RolloutToEvent(r, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if got := ev.Metadata["strategy"]; got != "blueGreen" {
		t.Fatalf("metadata[strategy] = %q, want %q", got, "blueGreen")
	}

	// CANARY_VERSION must NOT be emitted for blueGreen.
	if got := countEdges(ev.TopologyEdges, "CANARY_VERSION"); got != 0 {
		t.Fatalf("CANARY_VERSION count = %d, want 0 (blueGreen strategy)", got)
	}
	// BLUEGREEN_ACTIVE present and points at the active Service.
	bg := findEdge(ev.TopologyEdges, "BLUEGREEN_ACTIVE")
	if bg == nil {
		t.Fatalf("BLUEGREEN_ACTIVE missing; edges=%#v", ev.TopologyEdges)
	}
	want := "k8s_resource/99999999-8888-7777-6666-555555555555/Service/team-a/checkout-active"
	if bg.TargetExternalID != want {
		t.Fatalf("BLUEGREEN_ACTIVE target = %q, want %q", bg.TargetExternalID, want)
	}
	// CURRENT_VERSION points at the current RS.
	cur := findEdge(ev.TopologyEdges, "CURRENT_VERSION")
	if cur == nil || cur.TargetExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ReplicaSet/team-a/checkout-green2" {
		t.Fatalf("CURRENT_VERSION target = %v, want checkout-green2", cur)
	}
}

// ---------------------------------------------------------------------------
// Defensive scenarios — small but load-bearing.
// ---------------------------------------------------------------------------

func TestRolloutToEvent_DeletionStubProducesWellFormedEvent(t *testing.T) {
	stub := &rolloutsv1alpha1.Rollout{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "checkout"},
	}
	ev := entity.RolloutToEvent(stub, entity.ActionDeleted, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.Action != entity.ActionDeleted {
		t.Fatalf("action = %q, want %q", ev.Action, entity.ActionDeleted)
	}
	if want := "k8s_resource/99999999-8888-7777-6666-555555555555/Rollout/team-a/checkout"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	// No status fields → no edges.
	if len(ev.TopologyEdges) != 0 {
		t.Fatalf("topology_edges should be empty on a stub; got %#v", ev.TopologyEdges)
	}
	// strategy / phase keys should not be in metadata for a stub.
	if _, ok := ev.Metadata["strategy"]; ok {
		t.Fatalf("metadata should not carry strategy on a stub")
	}
	if _, ok := ev.Metadata["phase"]; ok {
		t.Fatalf("metadata should not carry phase on a stub")
	}
	if _, ok := ev.Metadata["current_pod_hash"]; ok {
		t.Fatalf("metadata should not carry current_pod_hash on a stub")
	}
}

func TestRolloutToEvent_AdversarialWorkspaceLabelIgnored(t *testing.T) {
	// ACL invariant — same shape as the Deployment test. A Rollout that
	// claims a different workspace via labels/annotations must not flip the
	// emitted event's workspace_id.
	r := rolloutFixture(
		"team-a", "checkout",
		&rolloutsv1alpha1.RolloutStrategy{Canary: &rolloutsv1alpha1.CanaryStrategy{}},
		rolloutsv1alpha1.RolloutStatus{Phase: rolloutsv1alpha1.RolloutPhaseHealthy},
		nil,
	)
	ev := entity.RolloutToEvent(r, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if ev.WorkspaceID != fixedWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, fixedWorkspace)
	}
}

func TestRolloutToEvent_OwnerEdgesFromOwnerReferences(t *testing.T) {
	// A Rollout owned by an ArgoCD Application — the edge must surface from
	// metadata.ownerReferences exactly as K8s populated it.
	owners := []metav1.OwnerReference{
		{Kind: "Application", Name: "checkout-app", UID: "00000000-1111-2222-3333-444444444444"},
	}
	r := rolloutFixture(
		"team-a", "checkout",
		&rolloutsv1alpha1.RolloutStrategy{Canary: &rolloutsv1alpha1.CanaryStrategy{}},
		rolloutsv1alpha1.RolloutStatus{Phase: rolloutsv1alpha1.RolloutPhaseHealthy},
		owners,
	)
	ev := entity.RolloutToEvent(r, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if len(ev.Edges) != 1 {
		t.Fatalf("ownerReferences edge count = %d, want 1; got %#v", len(ev.Edges), ev.Edges)
	}
	if got := ev.Edges[0].FromKind; got != "Application" {
		t.Fatalf("owner edge from_kind = %q, want %q", got, "Application")
	}
	if got := ev.Edges[0].FromExternalID; got != "k8s_resource/99999999-8888-7777-6666-555555555555/Application/team-a/checkout-app" {
		t.Fatalf("owner edge from_external_id = %q, want k8s_resource/Application/team-a/checkout-app", got)
	}
}

func TestRolloutToEvent_NoStrategyOmitsStrategyMetadata(t *testing.T) {
	// Empty spec.strategy (both pointers nil) — defensive path. We omit
	// metadata[strategy] rather than guessing.
	r := rolloutFixture("team-a", "checkout", nil, rolloutsv1alpha1.RolloutStatus{}, nil)
	ev := entity.RolloutToEvent(r, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if _, ok := ev.Metadata["strategy"]; ok {
		t.Fatalf("metadata[strategy] should be absent when both sub-strategies are nil")
	}
}
