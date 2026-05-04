// role_mapper.go: rbacv1.Role + rbacv1.ClusterRole -> Event mappers (issue #212).
//
// Both kinds carry a []PolicyRule on .Rules. The mapper renders rules as
// deterministic JSON with each per-rule string slice sorted, then rules
// sorted by their JSON byte-form. ClusterRole is the cluster-scoped
// counterpart — namespace is empty in baseMetadata for that kind.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through.
//   - `namespace` appears in baseMetadata + external_id ONLY (Role).
//   - PolicyRule contents are RBAC permission grants, not secret material;
//     they describe what subjects bound to this role can do.
//   - resourceNames lists are emitted as-is (sorted) — these are object
//     names like "kube-system" or specific configmap names. They're not
//     tenancy-revealing per ADR-0007 §ACL because they live in the
//     graph/JSON payload, not as metric labels.
package entity

import (
	"encoding/json"
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	rbacv1 "k8s.io/api/rbac/v1"
)

// EntityKindRole / EntityKindClusterRole are the K8s kind segments.
const (
	EntityKindRole        = "Role"
	EntityKindClusterRole = "ClusterRole"
)

// RoleToEvent maps a rbacv1.Role plus an action into an Event.
func RoleToEvent(r *rbacv1.Role, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(r.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindRole, r.Name)
	addRulesMetadata(meta, r.Rules)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindRole, namespace, r.Name),
		URI:         uriFor(clusterName, namespace, EntityKindRole, r.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// ClusterRoleToEvent maps a rbacv1.ClusterRole plus an action into an Event.
// Cluster-scoped — namespace is empty (resolveNamespace returns "default" as
// graph-fallback so external_id is well-formed; the graph consumer must
// distinguish ClusterRole from Role via the kind field, not the namespace).
func ClusterRoleToEvent(cr *rbacv1.ClusterRole, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace("") // ClusterRole has no namespace
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindClusterRole, cr.Name)
	addRulesMetadata(meta, cr.Rules)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindClusterRole, namespace, cr.Name),
		URI:         uriFor(clusterName, namespace, EntityKindClusterRole, cr.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// addRulesMetadata writes rules_count + rules_json (when non-empty) into
// the supplied metadata map. Shared by Role + ClusterRole because they
// emit the same payload shape — only the kind label differs.
func addRulesMetadata(meta map[string]string, rules []rbacv1.PolicyRule) {
	meta["rules_count"] = strconv.Itoa(len(rules))
	if len(rules) > 0 {
		raw, err := marshalPolicyRules(rules)
		if err != nil {
			meta["rules_json"] = ""
		} else {
			meta["rules_json"] = raw
		}
	}
}

// policyRuleView is the deterministic JSON shape for one PolicyRule.
// All slice fields are sorted; omitempty keeps optional fields out of the
// JSON when absent.
type policyRuleView struct {
	Verbs           []string `json:"verbs"`
	APIGroups       []string `json:"apiGroups,omitempty"`
	Resources       []string `json:"resources,omitempty"`
	ResourceNames   []string `json:"resourceNames,omitempty"`
	NonResourceURLs []string `json:"nonResourceURLs,omitempty"`
}

// marshalPolicyRules renders []PolicyRule as deterministic JSON. Each rule
// has its string slices sorted; the rules array itself is sorted by the
// per-rule JSON byte-form so two operators emit identical bytes regardless
// of source ordering.
func marshalPolicyRules(rules []rbacv1.PolicyRule) (string, error) {
	views := make([]policyRuleView, 0, len(rules))
	for _, r := range rules {
		views = append(views, policyRuleView{
			Verbs:           sortedCopy(r.Verbs),
			APIGroups:       sortedCopy(r.APIGroups),
			Resources:       sortedCopy(r.Resources),
			ResourceNames:   sortedCopy(r.ResourceNames),
			NonResourceURLs: sortedCopy(r.NonResourceURLs),
		})
	}

	// Sort rules by their JSON serialization for determinism. This is more
	// expensive than struct-field sorting but it's the only stable way to
	// order policy rules without inventing an arbitrary canonical key.
	type sortable struct {
		rule policyRuleView
		key  string
	}
	keyed := make([]sortable, 0, len(views))
	for _, v := range views {
		b, err := json.Marshal(v)
		if err != nil {
			return "", err
		}
		keyed = append(keyed, sortable{rule: v, key: string(b)})
	}
	sort.Slice(keyed, func(i, j int) bool { return keyed[i].key < keyed[j].key })

	out := make([]policyRuleView, 0, len(keyed))
	for _, k := range keyed {
		out = append(out, k.rule)
	}

	b, err := json.Marshal(out)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// sortedCopy returns a sorted copy of the input. Returns nil for empty
// input so the caller's omitempty tag suppresses the field.
func sortedCopy(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}
