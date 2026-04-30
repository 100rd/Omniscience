// networkpolicy.go: networkingv1.NetworkPolicy -> Event mapper.
//
// Topology: NetworkPolicy -[APPLIES_TO]-> Pod is conceptually derived from
// spec.podSelector, but resolving "which Pods match this selector right now"
// requires a label index that joins across watcher streams. Per issue #158
// §Non-goals, that resolution belongs to the reconciliation worker (#163).
//
// What we DO emit here: the rendered selector string in metadata, so the
// server side can perform the join when the index is ready, without the
// operator having to re-emit anything.
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	networkingv1 "k8s.io/api/networking/v1"
)

// EntityKindNetworkPolicy is the K8s kind segment in NetworkPolicy external_ids.
const EntityKindNetworkPolicy = "NetworkPolicy"

// NetworkPolicyToEvent maps a networkingv1.NetworkPolicy plus an action into
// an Event.
//
// Carried metadata (allow-list):
//   - cluster, namespace, name, kind, emitter (base)
//   - pod_selector (rendered selector string; empty matches everything in ns)
//   - ingress_rule_count, egress_rule_count
//   - policy_types ("Ingress", "Egress", or "Ingress,Egress" sorted)
func NetworkPolicyToEvent(np *networkingv1.NetworkPolicy, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(np.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindNetworkPolicy, np.Name)
	meta["pod_selector"] = formatLabelSelector(&np.Spec.PodSelector)
	meta["ingress_rule_count"] = strconv.Itoa(len(np.Spec.Ingress))
	meta["egress_rule_count"] = strconv.Itoa(len(np.Spec.Egress))
	meta["policy_types"] = renderPolicyTypes(np.Spec.PolicyTypes)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindNetworkPolicy, namespace, np.Name),
		URI:         uriFor(clusterName, namespace, EntityKindNetworkPolicy, np.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
		// Edges intentionally nil — APPLIES_TO is resolved server-side. See
		// the package doc above.
	}
}

// renderPolicyTypes returns the sorted comma-joined string form of the
// PolicyTypes slice. K8s allows "Ingress" / "Egress" / both; we sort to make
// the metadata stable across reconciles even if the API server reorders.
func renderPolicyTypes(types []networkingv1.PolicyType) string {
	if len(types) == 0 {
		return ""
	}
	strs := make([]string, len(types))
	for i, t := range types {
		strs[i] = string(t)
	}
	return joinSorted(strs)
}
