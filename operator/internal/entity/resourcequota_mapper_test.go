// resourcequota_mapper_test.go: pure unit tests for ResourceQuotaToEvent (#204).
package entity_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

var (
	rqTestWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	rqTestClusterID   = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	rqTestClusterName = "prod-eu"
	rqTestNow         = time.Date(2026, 5, 4, 12, 0, 0, 0, time.UTC)
)

func rqQty(s string) resource.Quantity { return resource.MustParse(s) }

// ─── Envelope ─────────────────────────────────────────────────────────────

func TestResourceQuotaToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	rq := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "compute-quota",
			// Adversarial labels — ACL invariant: tenancy from operator config
			// not from cluster state.
			Labels: map[string]string{
				"omniscience.io/workspace": "evil-ws",
				"team":                     "platform",
			},
			Annotations: map[string]string{
				"omniscience.io/index-data": "true",
			},
		},
		Spec: corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourceCPU: rqQty("10")}},
	}
	ev := entity.ResourceQuotaToEvent(rq, entity.ActionUpdated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)

	if ev.WorkspaceID != rqTestWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, rqTestWorkspace)
	}
	if ev.SourceType != entity.SourceType {
		t.Fatalf("source_type = %q", ev.SourceType)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ResourceQuota/team-a/compute-quota" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/ResourceQuota/compute-quota" {
		t.Fatalf("uri = %q", ev.URI)
	}
	if !ev.EmittedAt.Equal(rqTestNow) {
		t.Fatalf("emitted_at = %s", ev.EmittedAt)
	}
	if ev.Action != entity.ActionUpdated {
		t.Fatalf("action = %q", ev.Action)
	}
}

// ACL invariant: only the closed allow-list of metadata keys is emitted.
func TestResourceQuotaToEvent_ACL_NoUserLabelsOrAnnotationsLeak(t *testing.T) {
	rq := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "compute-quota",
			Labels: map[string]string{
				"team":          "platform",
				"cost-center":   "eng-42",
				"app.k8s.io/of": "checkout",
			},
			Annotations: map[string]string{
				"description": "team-a compute quota",
				"owner":       "alice@example.com",
			},
		},
		Spec: corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourceCPU: rqQty("10")}},
	}
	ev := entity.ResourceQuotaToEvent(rq, entity.ActionUpdated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"hard_count": {}, "hard_json": {}, "scopes_json": {}, "scope_selector_json": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q) — ACL allow-list violated", k, ev.Metadata[k])
		}
	}
	for forbidden := range rq.Labels {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user label %q leaked into metadata as %q", forbidden, v)
		}
	}
	for forbidden := range rq.Annotations {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user annotation %q leaked into metadata as %q", forbidden, v)
		}
	}
}

// ─── Fixture 1: hard-limits-only (no scopes, no selector) ─────────────────

func TestResourceQuotaToEvent_HardLimitsOnly(t *testing.T) {
	rq := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "compute"},
		Spec: corev1.ResourceQuotaSpec{
			Hard: corev1.ResourceList{
				corev1.ResourceCPU:    rqQty("10"),
				corev1.ResourceMemory: rqQty("20Gi"),
				corev1.ResourcePods:   rqQty("50"),
			},
		},
	}
	ev := entity.ResourceQuotaToEvent(rq, entity.ActionUpdated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)

	if ev.Metadata["hard_count"] != "3" {
		t.Fatalf("hard_count = %q, want 3", ev.Metadata["hard_count"])
	}
	var got map[string]string
	if err := json.Unmarshal([]byte(ev.Metadata["hard_json"]), &got); err != nil {
		t.Fatalf("hard_json not valid JSON: %v\n%s", err, ev.Metadata["hard_json"])
	}
	if got["cpu"] != "10" || got["memory"] != "20Gi" || got["pods"] != "50" {
		t.Fatalf("hard_json = %#v", got)
	}
	if _, present := ev.Metadata["scopes_json"]; present {
		t.Fatalf("scopes_json must be absent when no scopes")
	}
	if _, present := ev.Metadata["scope_selector_json"]; present {
		t.Fatalf("scope_selector_json must be absent when nil")
	}
}

// ─── Fixture 2: with scopes (BestEffort + NotTerminating) ─────────────────

func TestResourceQuotaToEvent_WithScopes(t *testing.T) {
	rq := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "best-effort"},
		Spec: corev1.ResourceQuotaSpec{
			Hard:   corev1.ResourceList{corev1.ResourcePods: rqQty("10")},
			Scopes: []corev1.ResourceQuotaScope{corev1.ResourceQuotaScopeNotTerminating, corev1.ResourceQuotaScopeBestEffort},
		},
	}
	ev := entity.ResourceQuotaToEvent(rq, entity.ActionCreated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)

	var scopes []string
	if err := json.Unmarshal([]byte(ev.Metadata["scopes_json"]), &scopes); err != nil {
		t.Fatalf("scopes_json not valid JSON: %v", err)
	}
	if len(scopes) != 2 || scopes[0] != "BestEffort" || scopes[1] != "NotTerminating" {
		t.Fatalf("scopes_json = %#v (must be sorted)", scopes)
	}
}

// ─── Fixture 3: with scopeSelector (PriorityClass IN [high, critical]) ────

func TestResourceQuotaToEvent_WithScopeSelector(t *testing.T) {
	rq := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "high-prio"},
		Spec: corev1.ResourceQuotaSpec{
			Hard: corev1.ResourceList{corev1.ResourcePods: rqQty("5")},
			ScopeSelector: &corev1.ScopeSelector{
				MatchExpressions: []corev1.ScopedResourceSelectorRequirement{{
					ScopeName: corev1.ResourceQuotaScopePriorityClass,
					Operator:  corev1.ScopeSelectorOpIn,
					// Intentionally unsorted — mapper must sort.
					Values: []string{"high", "critical"},
				}},
			},
		},
	}
	ev := entity.ResourceQuotaToEvent(rq, entity.ActionUpdated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)

	var got []scopeSelectorMatchForTest
	if err := json.Unmarshal([]byte(ev.Metadata["scope_selector_json"]), &got); err != nil {
		t.Fatalf("scope_selector_json not valid JSON: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d expressions, want 1", len(got))
	}
	if got[0].ScopeName != "PriorityClass" || got[0].Operator != "In" {
		t.Fatalf("expr[0] = %#v", got[0])
	}
	if len(got[0].Values) != 2 || got[0].Values[0] != "critical" || got[0].Values[1] != "high" {
		t.Fatalf("values = %#v (must be sorted)", got[0].Values)
	}
}

// ─── Fixture 4: empty Spec.Hard → no hard_json key, count is "0" ──────────

func TestResourceQuotaToEvent_EmptyHard(t *testing.T) {
	rq := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "empty"},
		Spec:       corev1.ResourceQuotaSpec{Hard: nil},
	}
	ev := entity.ResourceQuotaToEvent(rq, entity.ActionDeleted, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)
	if ev.Metadata["hard_count"] != "0" {
		t.Fatalf("hard_count = %q, want 0", ev.Metadata["hard_count"])
	}
	if _, present := ev.Metadata["hard_json"]; present {
		t.Fatalf("hard_json must be absent when spec.hard is empty")
	}
}

// ─── Determinism: two mappings of the same input produce identical bytes ──

func TestResourceQuotaToEvent_DeterministicJSON(t *testing.T) {
	build := func() *corev1.ResourceQuota {
		return &corev1.ResourceQuota{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
			Spec: corev1.ResourceQuotaSpec{
				Hard: corev1.ResourceList{
					corev1.ResourceMemory: rqQty("4Gi"),
					corev1.ResourceCPU:    rqQty("2"),
					corev1.ResourcePods:   rqQty("10"),
				},
				// Intentionally reverse-alphabetical; mapper must sort.
				Scopes: []corev1.ResourceQuotaScope{
					corev1.ResourceQuotaScopeTerminating,
					corev1.ResourceQuotaScopeBestEffort,
				},
				ScopeSelector: &corev1.ScopeSelector{
					MatchExpressions: []corev1.ScopedResourceSelectorRequirement{
						{ScopeName: corev1.ResourceQuotaScopePriorityClass, Operator: corev1.ScopeSelectorOpExists},
						{ScopeName: corev1.ResourceQuotaScopePriorityClass, Operator: corev1.ScopeSelectorOpDoesNotExist},
					},
				},
			},
		}
	}
	a := entity.ResourceQuotaToEvent(build(), entity.ActionUpdated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)
	b := entity.ResourceQuotaToEvent(build(), entity.ActionUpdated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)

	if a.Metadata["hard_json"] != b.Metadata["hard_json"] {
		t.Fatalf("non-deterministic hard_json:\nA: %s\nB: %s", a.Metadata["hard_json"], b.Metadata["hard_json"])
	}
	if a.Metadata["scopes_json"] != b.Metadata["scopes_json"] {
		t.Fatalf("non-deterministic scopes_json:\nA: %s\nB: %s", a.Metadata["scopes_json"], b.Metadata["scopes_json"])
	}
	if a.Metadata["scope_selector_json"] != b.Metadata["scope_selector_json"] {
		t.Fatalf("non-deterministic scope_selector_json:\nA: %s\nB: %s", a.Metadata["scope_selector_json"], b.Metadata["scope_selector_json"])
	}
	// And selector expressions are sorted: DoesNotExist before Exists.
	if !contains(a.Metadata["scope_selector_json"], `"operator":"DoesNotExist"`) {
		t.Fatalf("expected DoesNotExist in selector json; got %s", a.Metadata["scope_selector_json"])
	}
}

// ─── Default namespace fallback ───────────────────────────────────────────

func TestResourceQuotaToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	rq := &corev1.ResourceQuota{}
	rq.Name = "x"
	ev := entity.ResourceQuotaToEvent(rq, entity.ActionUpdated, rqTestWorkspace, rqTestClusterID, rqTestClusterName, rqTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ResourceQuota/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["namespace"] != "default" {
		t.Fatalf("namespace = %q", ev.Metadata["namespace"])
	}
}

// ─── helpers ──────────────────────────────────────────────────────────────

type scopeSelectorMatchForTest struct {
	ScopeName string   `json:"scopeName"`
	Operator  string   `json:"operator"`
	Values    []string `json:"values,omitempty"`
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
