// ingress.go: networkingv1.Ingress -> Event mapper.
//
// Topology: Ingress -[ROUTES_TO]-> Service, derived from
// spec.rules[].http.paths[].backend.service.name AND spec.defaultBackend
// .service.name. Never inferred. See issue #158 §Topology edges.
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	networkingv1 "k8s.io/api/networking/v1"
)

// EntityKindIngress is the K8s kind segment in Ingress external_ids.
const EntityKindIngress = "Ingress"

// IngressToEvent maps a networkingv1.Ingress plus an action into an Event.
//
// Carried metadata (allow-list):
//   - cluster, namespace, name, kind, emitter (base)
//   - ingress_class (spec.ingressClassName; "" if unset)
//   - rule_count (number of rules; "0" for default-backend-only ingresses)
//   - tls_count (number of TLS blocks)
//
// Edges: one ROUTES_TO edge per distinct Service named in the spec. We
// deduplicate so a multi-path ingress that fans out to the same service
// produces a single edge.
func IngressToEvent(ing *networkingv1.Ingress, action Action, workspaceID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(ing.Namespace)
	meta := baseMetadata(clusterName, namespace, EntityKindIngress, ing.Name)
	meta["ingress_class"] = derefString(ing.Spec.IngressClassName)
	meta["rule_count"] = strconv.Itoa(len(ing.Spec.Rules))
	meta["tls_count"] = strconv.Itoa(len(ing.Spec.TLS))

	edges := buildRoutesToEdges(ing, namespace)

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(EntityKindIngress, namespace, ing.Name),
		URI:         uriFor(clusterName, namespace, EntityKindIngress, ing.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:      meta,
		TopologyEdges: edges,
	}
}

// derefString returns *p, or "" if p == nil.
func derefString(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// buildRoutesToEdges scans defaultBackend + rules[].http.paths[].backend for
// service backends and emits one ROUTES_TO edge per distinct Service.
//
// External Name backends (backend.resource pointing at a CRD) are not Service
// references; we skip them. Same-named services in the same namespace are
// deduplicated.
func buildRoutesToEdges(ing *networkingv1.Ingress, namespace string) []EdgeRef {
	seen := map[string]struct{}{}
	var edges []EdgeRef
	add := func(serviceName string) {
		if serviceName == "" {
			return
		}
		ext := externalIDFor(EntityKindService, namespace, serviceName)
		if _, ok := seen[ext]; ok {
			return
		}
		seen[ext] = struct{}{}
		edges = append(edges, EdgeRef{Kind: EdgeKindRoutesTo, TargetExternalID: ext})
	}
	if ing.Spec.DefaultBackend != nil && ing.Spec.DefaultBackend.Service != nil {
		add(ing.Spec.DefaultBackend.Service.Name)
	}
	for _, rule := range ing.Spec.Rules {
		if rule.HTTP == nil {
			continue
		}
		for _, path := range rule.HTTP.Paths {
			if path.Backend.Service == nil {
				continue
			}
			add(path.Backend.Service.Name)
		}
	}
	return edges
}
