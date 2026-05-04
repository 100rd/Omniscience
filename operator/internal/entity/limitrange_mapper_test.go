// limitrange_mapper_test.go: pure unit tests for LimitRangeToEvent (#202).
//
// The four fixture shapes mirror the spec.limits[].type values that show up
// in real clusters: Container (defaults+min+max), Pod (min+max only),
// PersistentVolumeClaim (min+max storage), and a maxLimitRequestRatio
// shape. Each is exercised end-to-end through the mapper; we assert on
// envelope (workspace_id stamp, external_id, source_id, URI) and on the
// limits payload (count, deterministic JSON).
package entity_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	"github.com/100rd/omniscience/operator/internal/entity"
)

var (
	lrTestWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	lrTestClusterID   = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	lrTestClusterName = "prod-eu"
	lrTestNow         = time.Date(2026, 4, 24, 12, 0, 0, 0, time.UTC)
)

func qty(s string) resource.Quantity { return resource.MustParse(s) }

// ─── Constructor / envelope ────────────────────────────────────────────────

func TestLimitRangeToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "default-limits",
			// Adversarial labels — must NOT affect workspace stamp or
			// metadata. The ACL invariant: tenancy comes from operator
			// config, never from cluster state.
			Labels: map[string]string{
				"omniscience.io/workspace": "evil-ws",
				"team":                     "platform",
			},
			Annotations: map[string]string{
				"omniscience.io/index-data": "true", // not honoured here
			},
		},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{Type: corev1.LimitTypeContainer}}},
	}
	ev := entity.LimitRangeToEvent(lr, entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)

	if ev.WorkspaceID != lrTestWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, lrTestWorkspace)
	}
	if ev.SourceType != entity.SourceType {
		t.Fatalf("source_type = %q, want %q", ev.SourceType, entity.SourceType)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/LimitRange/team-a/default-limits" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/LimitRange/default-limits" {
		t.Fatalf("uri = %q", ev.URI)
	}
	if !ev.EmittedAt.Equal(lrTestNow) {
		t.Fatalf("emitted_at = %s, want %s", ev.EmittedAt, lrTestNow)
	}
	if ev.Action != entity.ActionUpdated {
		t.Fatalf("action = %q", ev.Action)
	}
}

// ACL invariant: only the closed allow-list of metadata keys is emitted.
// User labels and annotations must NOT leak through.
func TestLimitRangeToEvent_ACL_NoUserLabelsOrAnnotationsLeak(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "default-limits",
			Labels: map[string]string{
				"team":          "platform",
				"cost-center":   "eng-42",
				"app.k8s.io/of": "checkout",
			},
			Annotations: map[string]string{
				"description": "team-a default container limits",
				"owner":       "alice@example.com",
			},
		},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{Type: corev1.LimitTypeContainer}}},
	}
	ev := entity.LimitRangeToEvent(lr, entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"limits_count": {}, "limits_json": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q) — ACL allow-list violated", k, ev.Metadata[k])
		}
	}
	for forbidden := range lr.Labels {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user label %q leaked into metadata as %q", forbidden, v)
		}
	}
	for forbidden := range lr.Annotations {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user annotation %q leaked into metadata as %q", forbidden, v)
		}
	}
}

// ─── Fixture shape 1: Container with default + defaultRequest + min + max ──

func TestLimitRangeToEvent_ContainerShape(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "container-limits"},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
			Type:           corev1.LimitTypeContainer,
			Default:        corev1.ResourceList{corev1.ResourceCPU: qty("500m"), corev1.ResourceMemory: qty("256Mi")},
			DefaultRequest: corev1.ResourceList{corev1.ResourceCPU: qty("100m"), corev1.ResourceMemory: qty("128Mi")},
			Min:            corev1.ResourceList{corev1.ResourceCPU: qty("50m")},
			Max:            corev1.ResourceList{corev1.ResourceCPU: qty("2"), corev1.ResourceMemory: qty("4Gi")},
		}}},
	}
	ev := entity.LimitRangeToEvent(lr, entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)

	if ev.Metadata["limits_count"] != "1" {
		t.Fatalf("limits_count = %q, want 1", ev.Metadata["limits_count"])
	}
	var got []map[string]any
	if err := json.Unmarshal([]byte(ev.Metadata["limits_json"]), &got); err != nil {
		t.Fatalf("limits_json not valid JSON: %v\n%s", err, ev.Metadata["limits_json"])
	}
	if len(got) != 1 || got[0]["type"] != "Container" {
		t.Fatalf("got %#v", got)
	}
	def := got[0]["default"].(map[string]any)
	if def["cpu"] != "500m" || def["memory"] != "256Mi" {
		t.Fatalf("default = %#v", def)
	}
}

// ─── Fixture shape 2: Pod with min + max only (no defaults) ────────────────

func TestLimitRangeToEvent_PodShape(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "pod-limits"},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
			Type: corev1.LimitTypePod,
			Min:  corev1.ResourceList{corev1.ResourceCPU: qty("100m"), corev1.ResourceMemory: qty("64Mi")},
			Max:  corev1.ResourceList{corev1.ResourceCPU: qty("4"), corev1.ResourceMemory: qty("8Gi")},
		}}},
	}
	ev := entity.LimitRangeToEvent(lr, entity.ActionCreated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)

	var got []limitItemForTest
	if err := json.Unmarshal([]byte(ev.Metadata["limits_json"]), &got); err != nil {
		t.Fatalf("limits_json not valid JSON: %v", err)
	}
	if got[0].Type != "Pod" {
		t.Fatalf("type = %q", got[0].Type)
	}
	if len(got[0].Default) != 0 || len(got[0].DefaultRequest) != 0 {
		t.Fatalf("Pod limit must have empty default/defaultRequest; got default=%v defaultRequest=%v", got[0].Default, got[0].DefaultRequest)
	}
	if got[0].Min["cpu"] != "100m" || got[0].Max["memory"] != "8Gi" {
		t.Fatalf("min=%v max=%v", got[0].Min, got[0].Max)
	}
}

// ─── Fixture shape 3: PersistentVolumeClaim storage min/max ────────────────

func TestLimitRangeToEvent_PVCShape(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "pvc-limits"},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
			Type: corev1.LimitTypePersistentVolumeClaim,
			Min:  corev1.ResourceList{corev1.ResourceStorage: qty("1Gi")},
			Max:  corev1.ResourceList{corev1.ResourceStorage: qty("100Gi")},
		}}},
	}
	ev := entity.LimitRangeToEvent(lr, entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)

	var got []limitItemForTest
	if err := json.Unmarshal([]byte(ev.Metadata["limits_json"]), &got); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if got[0].Type != "PersistentVolumeClaim" {
		t.Fatalf("type = %q", got[0].Type)
	}
	if got[0].Min["storage"] != "1Gi" || got[0].Max["storage"] != "100Gi" {
		t.Fatalf("storage limits = min:%v max:%v", got[0].Min, got[0].Max)
	}
}

// ─── Fixture shape 4: Container with maxLimitRequestRatio ──────────────────

func TestLimitRangeToEvent_RatioShape(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "ratio-limits"},
		Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
			Type:                 corev1.LimitTypeContainer,
			MaxLimitRequestRatio: corev1.ResourceList{corev1.ResourceCPU: qty("4"), corev1.ResourceMemory: qty("2")},
		}}},
	}
	ev := entity.LimitRangeToEvent(lr, entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)

	var got []limitItemForTest
	if err := json.Unmarshal([]byte(ev.Metadata["limits_json"]), &got); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if got[0].MaxLimitRequestRatio["cpu"] != "4" || got[0].MaxLimitRequestRatio["memory"] != "2" {
		t.Fatalf("ratio = %v", got[0].MaxLimitRequestRatio)
	}
}

// ─── Determinism: two mappings of the same input produce identical bytes ──

func TestLimitRangeToEvent_DeterministicJSON(t *testing.T) {
	// Build the same LimitRange twice with maps that hash differently
	// in Go (extra padding entries can change iteration order).
	build := func() *corev1.LimitRange {
		return &corev1.LimitRange{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
			Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{
				// Intentionally provide types in REVERSE alphabetical order.
				// The mapper must sort the output by type.
				{
					Type: corev1.LimitTypePod,
					Max:  corev1.ResourceList{corev1.ResourceMemory: qty("4Gi"), corev1.ResourceCPU: qty("2")},
				},
				{
					Type: corev1.LimitTypeContainer,
					Min:  corev1.ResourceList{corev1.ResourceMemory: qty("64Mi"), corev1.ResourceCPU: qty("100m")},
				},
			}},
		}
	}
	a := entity.LimitRangeToEvent(build(), entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)
	b := entity.LimitRangeToEvent(build(), entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)
	if a.Metadata["limits_json"] != b.Metadata["limits_json"] {
		t.Fatalf("non-deterministic JSON:\nA: %s\nB: %s", a.Metadata["limits_json"], b.Metadata["limits_json"])
	}
	// And it must be sorted: Container before Pod.
	got := a.Metadata["limits_json"]
	idxC := indexOfType(got, "Container")
	idxP := indexOfType(got, "Pod")
	if idxC < 0 || idxP < 0 || idxC > idxP {
		t.Fatalf("expected Container before Pod in sorted JSON; got %s", got)
	}
}

// ─── Empty Spec.Limits: no limits_json key, count is "0" ───────────────────

func TestLimitRangeToEvent_EmptyLimits(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "empty"},
		Spec:       corev1.LimitRangeSpec{Limits: nil},
	}
	ev := entity.LimitRangeToEvent(lr, entity.ActionDeleted, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)
	if ev.Metadata["limits_count"] != "0" {
		t.Fatalf("limits_count = %q, want 0", ev.Metadata["limits_count"])
	}
	if _, present := ev.Metadata["limits_json"]; present {
		t.Fatalf("limits_json must be absent when spec.limits is empty; got %q", ev.Metadata["limits_json"])
	}
}

// Default namespace fallback: empty namespace becomes "default" so the
// external_id is still well-formed (defensive — LimitRange is namespaced
// by definition, but be consistent with the rest of the entity package).

func TestLimitRangeToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	lr := &corev1.LimitRange{
		ObjectMeta: corev1.LimitRange{}.ObjectMeta,
	}
	lr.Name = "x"
	ev := entity.LimitRangeToEvent(lr, entity.ActionUpdated, lrTestWorkspace, lrTestClusterID, lrTestClusterName, lrTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/LimitRange/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["namespace"] != "default" {
		t.Fatalf("namespace = %q", ev.Metadata["namespace"])
	}
}

// ─── helpers ───────────────────────────────────────────────────────────────

// limitItemForTest mirrors the mapper's internal limitItemView so we can
// decode in tests without exporting the production type. Field names match
// the JSON tags.
type limitItemForTest struct {
	Type                 string            `json:"type"`
	Default              map[string]string `json:"default,omitempty"`
	DefaultRequest       map[string]string `json:"defaultRequest,omitempty"`
	Min                  map[string]string `json:"min,omitempty"`
	Max                  map[string]string `json:"max,omitempty"`
	MaxLimitRequestRatio map[string]string `json:"maxLimitRequestRatio,omitempty"`
}

// indexOfType returns the byte index of the first occurrence of the given
// limit type in `s`, or -1 if absent. Used to assert sort order without
// regex overhead.
func indexOfType(s, typ string) int {
	needle := `"type":"` + typ + `"`
	for i := 0; i+len(needle) <= len(s); i++ {
		if s[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
