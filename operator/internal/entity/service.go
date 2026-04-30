// service.go: corev1.Service -> Event mapper.
//
// Topology note: Service does NOT emit SELECTS edges. Resolving "which Pods
// does this Service select" is the Endpoints controller's job (it consumes
// the API server's authoritative subsets list, instead of re-evaluating the
// label selector locally). See issue #158 §Topology edges.
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// EntityKindService is the K8s kind segment in Service external_ids.
const EntityKindService = "Service"

// ServiceToEvent maps a corev1.Service plus an action into an Event. Pure;
// no I/O, no clients. The metadata allow-list is closed and does not include
// user-controlled labels or annotations — see issue #158 §ACL invariant.
//
// Carried metadata (allow-list, exact):
//   - cluster, namespace, name, kind, emitter (base)
//   - service_type ("ClusterIP", "LoadBalancer", …)
//   - cluster_ip (the kube-allocated VIP; not user input)
//   - selector (deterministic "k=v,…" rendering of spec.selector — empty for
//     headless services with no selector)
//   - port_count (count of spec.ports as a string)
//
// Notably absent: spec.externalIPs, spec.loadBalancerIP, status.loadBalancer
// .ingress[].ip — all settable by tenant code paths and out of v0.2 scope.
func ServiceToEvent(svc *corev1.Service, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(svc.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindService, svc.Name)
	meta["service_type"] = string(svc.Spec.Type)
	meta["cluster_ip"] = svc.Spec.ClusterIP
	meta["selector"] = renderStringMapSorted(svc.Spec.Selector)
	meta["port_count"] = strconv.Itoa(len(svc.Spec.Ports))

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindService, namespace, svc.Name),
		URI:         uriFor(clusterName, namespace, EntityKindService, svc.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// renderStringMapSorted is the small string-map -> "k=v,…" helper used by
// ServiceToEvent. Defined locally rather than in common.go because the
// LabelSelector path uses a richer renderer (matchExpressions) — this is the
// flat case.
func renderStringMapSorted(m map[string]string) string {
	keys := sortedKeys(m)
	out := ""
	for i, k := range keys {
		if i > 0 {
			out += ","
		}
		out += k + "=" + m[k]
	}
	return out
}
