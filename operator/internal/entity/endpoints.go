// endpoints.go: corev1.Endpoints -> Event mapper.
//
// The Endpoints controller is what produces Service -[SELECTS]-> Pod edges:
// the API server populates Endpoints.subsets[].addresses[].targetRef from
// the *current* selector match, so we get the authoritative resolution
// without re-evaluating selectors. The Service controller never produces
// these edges — see issue #158 §Topology edges.
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindEndpoints is the K8s kind segment in Endpoints external_ids.
//
// Endpoints share name with their owning Service (this is how the API server
// pairs them) — the external_id segments differ on Kind, so collisions are
// impossible.
const EntityKindEndpoints = "Endpoints"

// EndpointsToEvent maps a corev1.Endpoints plus an action into an Event.
//
// The mapper does two things:
//  1. Emits an Endpoints entity (the carrier; reviewers can confirm what
//     was observed at the API server boundary).
//  2. Emits SELECTS edges from the *paired Service* (same namespace+name) to
//     each Pod referenced by subsets[].addresses[].targetRef where targetRef
//     .kind == "Pod". This is the topology truth — derived from K8s-native
//     pointers, never re-inferred.
func EndpointsToEvent(ep *corev1.Endpoints, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(ep.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindEndpoints, ep.Name)
	meta["subset_count"] = strconv.Itoa(len(ep.Subsets))
	meta["address_count"] = strconv.Itoa(countEndpointAddresses(ep))

	edges := buildSelectsEdges(clusterID, ep, namespace)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindEndpoints, namespace, ep.Name),
		URI:         uriFor(clusterName, namespace, EntityKindEndpoints, ep.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:      meta,
		TopologyEdges: edges,
	}
}

// countEndpointAddresses returns the total number of addresses (ready +
// not-ready) across all subsets. Used as a coarse health signal in metadata
// without leaking individual IPs (which can be sensitive on private
// networks).
func countEndpointAddresses(ep *corev1.Endpoints) int {
	n := 0
	for i := range ep.Subsets {
		n += len(ep.Subsets[i].Addresses)
		n += len(ep.Subsets[i].NotReadyAddresses)
	}
	return n
}

// buildSelectsEdges walks Endpoints.subsets[].addresses[].targetRef and
// returns one EdgeRef per Pod target. The "from" side of the edge is implicit
// in the Endpoints' name (= Service name) — the server-side ingester pairs
// the Endpoints entity with the Service entity by namespace+name on insert.
//
// We deliberately emit edges for both ready and not-ready addresses: the
// Endpoints document records the membership decision at this point in time;
// readiness is a separate dimension and changes more frequently than the
// selector match.
func buildSelectsEdges(clusterID uuid.UUID, ep *corev1.Endpoints, namespace string) []EdgeRef {
	var edges []EdgeRef
	for _, sub := range ep.Subsets {
		edges = appendPodEdges(clusterID, edges, sub.Addresses, namespace)
		edges = appendPodEdges(clusterID, edges, sub.NotReadyAddresses, namespace)
	}
	return edges
}

// appendPodEdges appends a SELECTS edge for every address whose targetRef
// points to a Pod. Other targetRef kinds (rare; e.g. legacy clusters that
// reference Services directly) are skipped — we only emit edges we can
// confidently express as `k8s_resource/Pod/{namespace}/{name}`.
func appendPodEdges(clusterID uuid.UUID, edges []EdgeRef, addrs []corev1.EndpointAddress, fallbackNamespace string) []EdgeRef {
	for i := range addrs {
		ref := addrs[i].TargetRef
		if ref == nil || ref.Kind != "Pod" || ref.Name == "" {
			continue
		}
		ns := ref.Namespace
		if ns == "" {
			ns = fallbackNamespace
		}
		edges = append(edges, EdgeRef{
			Kind:             EdgeKindSelects,
			TargetExternalID: externalIDFor(clusterID, "Pod", ns, ref.Name),
		})
	}
	return edges
}
