// Tests for the pure Diff function. The matrix below is the one called
// out as required in issue #163 acceptance.
package reconciler_test

import (
	"reflect"
	"testing"

	"github.com/100rd/Omniscience/operator/internal/reconciler"
)

func TestDiff_BothEmpty(t *testing.T) {
	mig, mic := reconciler.Diff(nil, nil)
	if len(mig) != 0 || len(mic) != 0 {
		t.Fatalf("expected zero-zero diff, got mig=%v mic=%v", mig, mic)
	}
}

func TestDiff_Identical(t *testing.T) {
	in := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
	}
	mig, mic := reconciler.Diff(in, in)
	if len(mig) != 0 || len(mic) != 0 {
		t.Fatalf("expected zero-zero diff for identical sets, got mig=%v mic=%v", mig, mic)
	}
}

func TestDiff_MissingInGraph_OneSide(t *testing.T) {
	cluster := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
	}
	graph := []string{
		"k8s_resource/cid/Pod/ns/a",
	}
	mig, mic := reconciler.Diff(cluster, graph)
	if !reflect.DeepEqual(mig, []string{"k8s_resource/cid/Pod/ns/b"}) {
		t.Fatalf("missing-in-graph mismatch: %v", mig)
	}
	if len(mic) != 0 {
		t.Fatalf("expected empty missing-in-cluster, got %v", mic)
	}
}

func TestDiff_MissingInCluster_OneSide(t *testing.T) {
	cluster := []string{
		"k8s_resource/cid/Pod/ns/a",
	}
	graph := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
	}
	mig, mic := reconciler.Diff(cluster, graph)
	if len(mig) != 0 {
		t.Fatalf("expected empty missing-in-graph, got %v", mig)
	}
	if !reflect.DeepEqual(mic, []string{"k8s_resource/cid/Pod/ns/b"}) {
		t.Fatalf("missing-in-cluster mismatch: %v", mic)
	}
}

func TestDiff_BothDirections_MassDrift(t *testing.T) {
	cluster := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
		"k8s_resource/cid/Pod/ns/c",
		"k8s_resource/cid/Pod/ns/d",
	}
	graph := []string{
		"k8s_resource/cid/Pod/ns/c",
		"k8s_resource/cid/Pod/ns/d",
		"k8s_resource/cid/Pod/ns/e",
		"k8s_resource/cid/Pod/ns/f",
	}
	mig, mic := reconciler.Diff(cluster, graph)
	wantMig := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
	}
	wantMic := []string{
		"k8s_resource/cid/Pod/ns/e",
		"k8s_resource/cid/Pod/ns/f",
	}
	if !reflect.DeepEqual(mig, wantMig) {
		t.Fatalf("missing-in-graph mismatch: got=%v want=%v", mig, wantMig)
	}
	if !reflect.DeepEqual(mic, wantMic) {
		t.Fatalf("missing-in-cluster mismatch: got=%v want=%v", mic, wantMic)
	}
}

func TestDiff_EmptyClusterDeletesEverything(t *testing.T) {
	graph := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
	}
	mig, mic := reconciler.Diff(nil, graph)
	if len(mig) != 0 {
		t.Fatalf("expected empty missing-in-graph, got %v", mig)
	}
	if len(mic) != 2 {
		t.Fatalf("expected 2 deletions, got %v", mic)
	}
}

func TestDiff_EmptyGraphCreatesEverything(t *testing.T) {
	cluster := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
	}
	mig, mic := reconciler.Diff(cluster, nil)
	if len(mig) != 2 {
		t.Fatalf("expected 2 creations, got %v", mig)
	}
	if len(mic) != 0 {
		t.Fatalf("expected empty missing-in-cluster, got %v", mic)
	}
}

func TestDiff_IgnoresEmptyStrings(t *testing.T) {
	// Defensive: an upstream bug that produces an empty external_id MUST
	// NOT cause "everything looks missing" because empty would map to a
	// distinct set element on one side only.
	cluster := []string{
		"k8s_resource/cid/Pod/ns/a",
		"",
	}
	graph := []string{
		"k8s_resource/cid/Pod/ns/a",
	}
	mig, mic := reconciler.Diff(cluster, graph)
	if len(mig) != 0 || len(mic) != 0 {
		t.Fatalf("expected zero diff with empty filtered, got mig=%v mic=%v", mig, mic)
	}
}

func TestDiff_DeterministicOrder(t *testing.T) {
	cluster := []string{
		"k8s_resource/cid/Pod/ns/c",
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
	}
	mig, _ := reconciler.Diff(cluster, nil)
	want := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
		"k8s_resource/cid/Pod/ns/c",
	}
	if !reflect.DeepEqual(mig, want) {
		t.Fatalf("expected sorted output: got=%v want=%v", mig, want)
	}
}
