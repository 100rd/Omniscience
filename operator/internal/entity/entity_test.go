package entity_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

// fixedNow is the deterministic clock pinned for assertions. Pinned in UTC
// so the JSON serialisation comparison is timezone-stable.
var fixedNow = time.Date(2026, 4, 24, 12, 0, 0, 0, time.UTC)

// fixedWorkspace is a fixed UUID used across the tests so externalID and
// sourceID derivations are reproducible.
var fixedWorkspace = uuid.MustParse("11111111-2222-3333-4444-555555555555")

// fixedClusterID is a deterministic UUID used by every mapper test so the
// re-keyed external_ids (issue #167) are reproducible across runs.
var fixedClusterID = uuid.MustParse("99999999-8888-7777-6666-555555555555")

func newPod(namespace, name string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace,
			Name:      name,
			Labels: map[string]string{
				"app":                          "frontend",
				"adversarial.example.com/spy":  "should-be-ignored",
			},
		},
		Spec: corev1.PodSpec{
			NodeName: "node-a",
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
		},
	}
}

func TestPodToEvent_FieldsAreCorrectAndStamped(t *testing.T) {
	pod := newPod("team-a", "checkout-7d9f")
	ev := entity.PodToEvent(pod, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev.SourceType != entity.SourceType {
		t.Fatalf("source_type = %q, want %q", ev.SourceType, entity.SourceType)
	}
	if ev.WorkspaceID != fixedWorkspace {
		t.Fatalf("workspace_id = %s, want %s", ev.WorkspaceID, fixedWorkspace)
	}
	if ev.Action != entity.ActionCreated {
		t.Fatalf("action = %q, want %q", ev.Action, entity.ActionCreated)
	}
	wantExternal := "k8s_resource/99999999-8888-7777-6666-555555555555/Pod/team-a/checkout-7d9f"
	if ev.ExternalID != wantExternal {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, wantExternal)
	}
	wantURI := "kube://prod-eu/team-a/Pod/checkout-7d9f"
	if ev.URI != wantURI {
		t.Fatalf("uri = %q, want %q", ev.URI, wantURI)
	}
	if !ev.EmittedAt.Equal(fixedNow) {
		t.Fatalf("emitted_at = %s, want %s", ev.EmittedAt, fixedNow)
	}
}

func TestPodToEvent_DoesNotLeakUserLabels(t *testing.T) {
	// ACL invariant: user-controlled labels must not flow into event
	// metadata. ADR-0007 §ACL is explicit on this point — labels are
	// tenant-writable state and a workspace-leaking attack surface.
	pod := newPod("team-a", "checkout-7d9f")
	ev := entity.PodToEvent(pod, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	for k, v := range ev.Metadata {
		if k == "app" || k == "adversarial.example.com/spy" {
			t.Fatalf("metadata leaked user label %q=%q", k, v)
		}
	}
	// Spot-check the fields that ARE expected — the closed allow-list.
	expected := map[string]string{
		"cluster":   "prod-eu", "cluster_id": "99999999-8888-7777-6666-555555555555",
		"namespace": "team-a",
		"name":      "checkout-7d9f",
		"kind":      "Pod",
		"node":      "node-a",
		"phase":     string(corev1.PodRunning),
		"emitter":   "k8s-operator",
	}
	for k, v := range expected {
		if got, ok := ev.Metadata[k]; !ok || got != v {
			t.Fatalf("metadata[%q] = %q, want %q", k, got, v)
		}
	}
	if len(ev.Metadata) != len(expected) {
		t.Fatalf("metadata has %d keys (%v), want exactly %d (%v)",
			len(ev.Metadata), ev.Metadata, len(expected), expected)
	}
}

func TestPodToEvent_SourceIDIsDeterministicAcrossCalls(t *testing.T) {
	// Restart-stable lineage: the same (workspace, cluster) MUST produce
	// the same source_id every time. The pod identity must NOT influence
	// it (the source represents the operator instance, not a single pod).
	pod1 := newPod("team-a", "checkout-7d9f")
	pod2 := newPod("team-b", "billing-aa11")

	ev1 := entity.PodToEvent(pod1, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	ev2 := entity.PodToEvent(pod2, entity.ActionDeleted, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	if ev1.SourceID != ev2.SourceID {
		t.Fatalf("source_id should be stable across pods within (workspace, cluster); got %s vs %s",
			ev1.SourceID, ev2.SourceID)
	}
}

func TestPodToEvent_SourceIDChangesWithCluster(t *testing.T) {
	// Different clusters under the same workspace must produce DIFFERENT
	// source_ids — otherwise per-cluster scoping on the server side breaks.
	pod := newPod("team-a", "checkout-7d9f")
	a := entity.PodToEvent(pod, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow).SourceID
	b := entity.PodToEvent(pod, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-us", fixedNow).SourceID
	if a == b {
		t.Fatalf("source_id should differ across clusters; got %s == %s", a, b)
	}
}

func TestPodToEvent_NamespacelessPodFallsBackSafely(t *testing.T) {
	pod := newPod("", "rogue-pod")
	ev := entity.PodToEvent(pod, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	want := "k8s_resource/99999999-8888-7777-6666-555555555555/Pod/default/rogue-pod"
	if ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q (defaulted namespace)", ev.ExternalID, want)
	}
}

func TestPodToEvent_JSONShapeIsStableForConsumer(t *testing.T) {
	// The Pydantic consumer side validates against snake_case fields. This
	// test is a contract guard against accidental rename / casing drift.
	pod := newPod("team-a", "checkout-7d9f")
	ev := entity.PodToEvent(pod, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	raw, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	required := []string{
		"source_id", "source_type", "external_id", "uri",
		"action", "workspace_id", "emitted_at", "metadata",
	}
	for _, k := range required {
		if _, ok := parsed[k]; !ok {
			t.Fatalf("required JSON field %q missing in payload: %s", k, raw)
		}
	}
	// Negative: no camelCase leakage.
	if _, ok := parsed["sourceId"]; ok {
		t.Fatalf("camelCase 'sourceId' leaked into payload — Pydantic consumer expects snake_case")
	}
}
