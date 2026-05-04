// resourcequota_mapper.go: corev1.ResourceQuota -> Event mapper (issue #204).
//
// ResourceQuota holds per-namespace caps on aggregate resource consumption
// (`spec.hard`) and optionally narrows the workloads it applies to via
// `spec.scopes` and `spec.scopeSelector`. The mapper emits ONLY the closed
// allow-list of fields from baseMetadata plus a kind-specific payload.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through. The
//     ACL invariant (ADR-0007 §ACL) is that workspace_id flows from operator
//     config; nothing on the watched resource influences tenancy.
//
//   - `namespace` appears in baseMetadata + external_id ONLY. It is a graph-
//     linking field and MUST NOT become a metric label dimension. The
//     corresponding metric (RecordEmit) is wired in the controller and uses
//     kind only — see #198/#201.
//
//   - ResourceQuota spec contains operational quantities and (optionally) a
//     scope selector that may reference scopes by name. Selector values are
//     emitted as deterministic JSON so consumers can trace which workloads
//     a quota applies to, but no labels / annotations are copied through.
package entity

import (
	"encoding/json"
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindResourceQuota is the K8s kind segment in ResourceQuota external_ids.
const EntityKindResourceQuota = "ResourceQuota"

// ResourceQuotaToEvent maps a corev1.ResourceQuota plus an action into an
// Event. Pure function — no I/O, no clients, no clock beyond `now`.
//
// Emitted metadata:
//
//   - Base allow-list: cluster, cluster_id, namespace, name, kind, emitter
//   - "hard_count":           decimal string of len(spec.hard)
//   - "hard_json":            deterministic JSON of spec.hard, sorted by key.
//                             Absent when spec.hard is empty.
//   - "scopes_json":          deterministic JSON of spec.scopes, sorted.
//                             Absent when spec.scopes is empty.
//   - "scope_selector_json":  deterministic JSON of spec.scopeSelector
//                             (sorted by scopeName then operator). Absent when
//                             spec.scopeSelector is nil or has no expressions.
//
// All JSON renderings are byte-stable so two operators observing the same
// ResourceQuota emit identical bytes regardless of map iteration order.
func ResourceQuotaToEvent(rq *corev1.ResourceQuota, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(rq.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindResourceQuota, rq.Name)

	meta["hard_count"] = strconv.Itoa(len(rq.Spec.Hard))
	if len(rq.Spec.Hard) > 0 {
		raw, err := marshalResourceList(rq.Spec.Hard)
		if err != nil {
			meta["hard_json"] = ""
		} else {
			meta["hard_json"] = raw
		}
	}

	if len(rq.Spec.Scopes) > 0 {
		raw, err := marshalScopes(rq.Spec.Scopes)
		if err != nil {
			meta["scopes_json"] = ""
		} else {
			meta["scopes_json"] = raw
		}
	}

	if rq.Spec.ScopeSelector != nil && len(rq.Spec.ScopeSelector.MatchExpressions) > 0 {
		raw, err := marshalScopeSelector(rq.Spec.ScopeSelector)
		if err != nil {
			meta["scope_selector_json"] = ""
		} else {
			meta["scope_selector_json"] = raw
		}
	}

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindResourceQuota, namespace, rq.Name),
		URI:         uriFor(clusterName, namespace, EntityKindResourceQuota, rq.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// marshalResourceList renders a ResourceList (map[ResourceName]Quantity) as
// deterministic JSON: a string-keyed map (sorted by encoding/json since Go
// 1.12) with quantities rendered via .String() (kubectl-canonical: 500m, 1Gi).
// Returns empty string for nil/empty input.
func marshalResourceList(rl corev1.ResourceList) (string, error) {
	if len(rl) == 0 {
		return "", nil
	}
	out := make(map[string]string, len(rl))
	for k, v := range rl {
		out[string(k)] = v.String()
	}
	b, err := json.Marshal(out)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// marshalScopes renders []ResourceQuotaScope as a sorted JSON array of strings.
// Sorting by scope name keeps the output stable across operator restarts.
func marshalScopes(scopes []corev1.ResourceQuotaScope) (string, error) {
	out := make([]string, 0, len(scopes))
	for _, s := range scopes {
		out = append(out, string(s))
	}
	sort.Strings(out)
	b, err := json.Marshal(out)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// scopeSelectorMatchView is the deterministic JSON shape we emit for one
// scopeSelector match expression. Values are sorted lexicographically.
type scopeSelectorMatchView struct {
	ScopeName string   `json:"scopeName"`
	Operator  string   `json:"operator"`
	Values    []string `json:"values,omitempty"`
}

// marshalScopeSelector renders a *ScopeSelector as deterministic JSON. Match
// expressions are sorted by (scopeName, operator) so two operators emit the
// same bytes regardless of slice order in the source object.
func marshalScopeSelector(sel *corev1.ScopeSelector) (string, error) {
	views := make([]scopeSelectorMatchView, 0, len(sel.MatchExpressions))
	for _, me := range sel.MatchExpressions {
		values := append([]string(nil), me.Values...)
		sort.Strings(values)
		views = append(views, scopeSelectorMatchView{
			ScopeName: string(me.ScopeName),
			Operator:  string(me.Operator),
			Values:    values,
		})
	}
	sort.Slice(views, func(i, j int) bool {
		if views[i].ScopeName != views[j].ScopeName {
			return views[i].ScopeName < views[j].ScopeName
		}
		return views[i].Operator < views[j].Operator
	})
	b, err := json.Marshal(views)
	if err != nil {
		return "", err
	}
	return string(b), nil
}
