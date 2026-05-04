// role_mapper_test.go: pure unit tests for RoleToEvent + ClusterRoleToEvent (#212).
package entity_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

var (
	rTestWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	rTestClusterID   = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	rTestClusterName = "prod-eu"
	rTestNow         = time.Date(2026, 5, 4, 12, 0, 0, 0, time.UTC)
)

// ─── Role envelope + ACL ──────────────────────────────────────────────────

func TestRoleToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	r := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "viewer",
			Labels: map[string]string{
				"omniscience.io/workspace": "evil-ws",
			},
		},
	}
	ev := entity.RoleToEvent(r, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	if ev.WorkspaceID != rTestWorkspace {
		t.Fatalf("workspace_id = %s — adversarial label honoured!", ev.WorkspaceID)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Role/team-a/viewer" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/Role/viewer" {
		t.Fatalf("uri = %q", ev.URI)
	}
}

func TestRoleToEvent_ACL_NoUserLabelsLeak(t *testing.T) {
	r := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:   "team-a",
			Name:        "viewer",
			Labels:      map[string]string{"team": "platform"},
			Annotations: map[string]string{"description": "read-only"},
		},
	}
	ev := entity.RoleToEvent(r, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"rules_count": {}, "rules_json": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q)", k, ev.Metadata[k])
		}
	}
}

// ─── Role rules: multi-rule with sorted strings + sorted rules ────────────

func TestRoleToEvent_MultipleRules(t *testing.T) {
	r := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "ops"},
		Rules: []rbacv1.PolicyRule{
			// Intentionally reverse-alphabetical inputs — mapper must sort.
			{
				Verbs:     []string{"watch", "list", "get"},
				APIGroups: []string{""},
				Resources: []string{"pods", "configmaps"},
			},
			{
				Verbs:     []string{"create", "delete"},
				APIGroups: []string{"apps"},
				Resources: []string{"deployments"},
			},
		},
	}
	ev := entity.RoleToEvent(r, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	if ev.Metadata["rules_count"] != "2" {
		t.Fatalf("rules_count = %q, want 2", ev.Metadata["rules_count"])
	}

	var got []map[string]any
	if err := json.Unmarshal([]byte(ev.Metadata["rules_json"]), &got); err != nil {
		t.Fatalf("rules_json not valid JSON: %v\n%s", err, ev.Metadata["rules_json"])
	}
	if len(got) != 2 {
		t.Fatalf("got %d rules, want 2", len(got))
	}

	// Each rule's verbs/resources must be sorted within the rule.
	for _, rule := range got {
		verbs := rule["verbs"].([]any)
		for i := 1; i < len(verbs); i++ {
			if verbs[i-1].(string) > verbs[i].(string) {
				t.Fatalf("verbs not sorted in rule %#v", rule)
			}
		}
		if res, ok := rule["resources"].([]any); ok {
			for i := 1; i < len(res); i++ {
				if res[i-1].(string) > res[i].(string) {
					t.Fatalf("resources not sorted in rule %#v", rule)
				}
			}
		}
	}
}

func TestRoleToEvent_DeterministicJSON(t *testing.T) {
	build := func() *rbacv1.Role {
		return &rbacv1.Role{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
			Rules: []rbacv1.PolicyRule{
				{Verbs: []string{"get"}, APIGroups: []string{""}, Resources: []string{"pods"}},
				{Verbs: []string{"list"}, APIGroups: []string{""}, Resources: []string{"configmaps"}},
			},
		}
	}
	a := entity.RoleToEvent(build(), entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	b := entity.RoleToEvent(build(), entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	if a.Metadata["rules_json"] != b.Metadata["rules_json"] {
		t.Fatalf("non-deterministic rules_json:\nA: %s\nB: %s", a.Metadata["rules_json"], b.Metadata["rules_json"])
	}
}

func TestRoleToEvent_EmptyRules(t *testing.T) {
	r := &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "empty"},
	}
	ev := entity.RoleToEvent(r, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	if ev.Metadata["rules_count"] != "0" {
		t.Fatalf("rules_count = %q", ev.Metadata["rules_count"])
	}
	if _, present := ev.Metadata["rules_json"]; present {
		t.Fatalf("rules_json must be absent when rules empty")
	}
}

// ─── ClusterRole ──────────────────────────────────────────────────────────

func TestClusterRoleToEvent_NoNamespace(t *testing.T) {
	cr := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{Name: "cluster-admin"},
		Rules: []rbacv1.PolicyRule{
			{Verbs: []string{"*"}, APIGroups: []string{"*"}, Resources: []string{"*"}},
		},
	}
	ev := entity.ClusterRoleToEvent(cr, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	if ev.Metadata["kind"] != "ClusterRole" {
		t.Fatalf("kind = %q", ev.Metadata["kind"])
	}
	if ev.Metadata["namespace"] != "default" {
		t.Fatalf("namespace = %q (resolveNamespace fallback for cluster-scoped)", ev.Metadata["namespace"])
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ClusterRole/default/cluster-admin" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["rules_count"] != "1" {
		t.Fatalf("rules_count = %q", ev.Metadata["rules_count"])
	}
}

// ─── ClusterRole with non-resource URLs ───────────────────────────────────

func TestClusterRoleToEvent_NonResourceURLs(t *testing.T) {
	cr := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{Name: "metrics-reader"},
		Rules: []rbacv1.PolicyRule{
			{
				Verbs:           []string{"get"},
				NonResourceURLs: []string{"/metrics", "/healthz"},
			},
		},
	}
	ev := entity.ClusterRoleToEvent(cr, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)

	var got []map[string]any
	if err := json.Unmarshal([]byte(ev.Metadata["rules_json"]), &got); err != nil {
		t.Fatalf("rules_json not valid JSON: %v", err)
	}
	urls := got[0]["nonResourceURLs"].([]any)
	if len(urls) != 2 || urls[0] != "/healthz" || urls[1] != "/metrics" {
		t.Fatalf("nonResourceURLs = %#v (must be sorted)", urls)
	}
}

// ─── Default namespace fallback (Role with empty namespace string) ────────

func TestRoleToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	r := &rbacv1.Role{}
	r.Name = "x"
	ev := entity.RoleToEvent(r, entity.ActionUpdated, rTestWorkspace, rTestClusterID, rTestClusterName, rTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Role/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
}
