// Package entity — DRA ResourceClaimTemplate mapping.
//
// ResourceClaimTemplate (resource.k8s.io/v1beta1) is the namespaced object
// that defines the *shape* of a ResourceClaim that the K8s control plane
// generates for each Pod that references the template via
// pod.spec.resourceClaims[].resourceClaimTemplateName. It is structurally
// similar to a ResourceClaim — its spec.spec carries the same DeviceClaim
// shape — and the same ALLOCATES edges to DeviceClass apply.
//
// ACL allow-list (issue #162, ADR-0007 §ACL): cluster, namespace, name, kind,
// emitter. Template metadata is NOT copied through; tenancy is operator
// config. (No phase / device_count — templates have no status.)
//
// Topology edges:
//   - IN_CLUSTER -> Cluster anchor.
//   - ALLOCATES   -> DeviceClass(es) named by spec.spec.devices.requests[].
//     The template doesn't *allocate* anything itself, but the resolved
//     class set on the generated ResourceClaim is fixed by the template,
//     so emitting the edge here gives consumers a stable identity-time
//     pointer to the class even before any Pod is scheduled.
package entity

import (
	"sort"
	"time"

	"github.com/google/uuid"
	resourcev1beta1 "k8s.io/api/resource/v1beta1"
)

// EntityKindResourceClaimTemplate is the K8s kind segment in
// ResourceClaimTemplate external_ids.
const EntityKindResourceClaimTemplate = "ResourceClaimTemplate"

// ResourceClaimTemplateToEvent maps a v1beta1.ResourceClaimTemplate into an
// Event. Pure function — no client, no clock injection beyond `now`.
func ResourceClaimTemplateToEvent(tpl *resourcev1beta1.ResourceClaimTemplate, action Action, workspaceID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := defaultedNamespace(tpl.Namespace)
	externalID := externalID(EntityKindResourceClaimTemplate, namespace, tpl.Name)
	uri := resourceURI(clusterName, namespace, EntityKindResourceClaimTemplate, tpl.Name)

	meta := baseMetadata(clusterName, namespace, EntityKindResourceClaimTemplate, tpl.Name)

	edges := []EdgeRef{inClusterEdge(clusterName)}
	for _, target := range templateAllocatesEdgeTargets(tpl) {
		edges = append(edges, EdgeRef{
			Kind:             EdgeKindAllocates,
			TargetExternalID: target,
		})
	}

	return &Event{
		SourceID:      deriveSourceID(workspaceID, clusterName),
		SourceType:    SourceType,
		ExternalID:    externalID,
		URI:           uri,
		Action:        action,
		WorkspaceID:   workspaceID,
		EmittedAt:     now.UTC(),
		Metadata:      meta,
		Edges:         ownerEdges(tpl.OwnerReferences, namespace, externalID),
		TopologyEdges: edges,
	}
}

// templateAllocatesEdgeTargets is the symmetric of allocatesEdgeTargets in
// dra_resourceclaim.go but for the template's embedded spec. Kept as a
// separate function (rather than refactored to share) because the input
// types differ in struct shape; the inlined form is shorter than a generic.
func templateAllocatesEdgeTargets(tpl *resourcev1beta1.ResourceClaimTemplate) []string {
	seen := make(map[string]struct{})
	for _, req := range tpl.Spec.Spec.Devices.Requests {
		name := req.DeviceClassName
		if name == "" {
			continue
		}
		seen[name] = struct{}{}
	}
	if len(seen) == 0 {
		return nil
	}
	out := make([]string, 0, len(seen))
	for name := range seen {
		out = append(out, EntityKindPrefix+"/"+EntityKindDeviceClass+"/"+name)
	}
	sort.Strings(out)
	return out
}
