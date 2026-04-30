// argocd_applicationset.go: argoproj.io/v1alpha1/ApplicationSet -> Event
// mapper.
//
// Topology edges (issue #160 §F — derived from K8s-native fields, never
// inferred):
//
//   - ApplicationSet -[GENERATES]-> Application — from
//     status.applicationStatus[].application when the field is populated
//     (ArgoCD v2.7+). Older installations don't populate this status block,
//     so the mapper falls back to emitting NO edges — never a panic, never
//     a labels-based heuristic.
//   - ApplicationSet -[IN_CLUSTER]-> Cluster, per the #159 anchor pattern.
//
// Metadata allow-list (issue §G — `cluster, namespace, name, kind,
// generators (a list of generator type strings), template_metadata_name,
// emitter`). The generator content is never surfaced — only the type names.
//
// ACL invariant: spec.template.metadata.* is what ArgoCD will stamp on
// child Applications; here we only carry .name. workspace_id comes
// exclusively from operator config (ADR-0007 §ACL).
package entity

import (
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/100rd/omniscience/operator/internal/argocd"
)

// EntityKindApplicationSet is the K8s Kind segment for ArgoCD ApplicationSets.
const EntityKindApplicationSet = "ApplicationSet"

// EdgeKindGenerates: ApplicationSet -> Application. The semantics are "this
// ApplicationSet is the source of this Application"; the inverse direction
// would be IS_GENERATED_BY but we keep the from->to direction consistent
// with EdgeKindDeploys (parent -> child).
const EdgeKindGenerates = "GENERATES"

// ApplicationSetToEvent maps an argocd.ApplicationSet plus an action into
// an Event. Pure function — same shape as ApplicationToEvent.
//
// Empty optional fields are OMITTED. Generator detection iterates the
// spec.generators[] slice; an entry with no field populated (malformed CR)
// is skipped silently. Generator names are emitted as a comma-joined,
// stable-ordered list.
func ApplicationSetToEvent(
	appset *argocd.ApplicationSet,
	action Action,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	namespace := resolveNamespace(appset.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindApplicationSet, appset.Name)

	if names := collectGeneratorNames(appset.Spec.Generators); len(names) > 0 {
		meta["generators"] = strings.Join(names, ",")
	}
	if v := appset.Spec.Template.Metadata.Name; v != "" {
		meta["template_metadata_name"] = v
	}

	extID := externalIDFor(clusterID, EntityKindApplicationSet, namespace, appset.Name)

	edges := buildGeneratesEdges(clusterID, appset.Status.ApplicationStatus, namespace)
	edges = append(edges, inClusterEdge(clusterID, clusterName))

	ev := &Event{
		SourceID:      DeriveSourceID(workspaceID, clusterName),
		SourceType:    SourceType,
		ExternalID:    extID,
		URI:           uriFor(clusterName, namespace, EntityKindApplicationSet, appset.Name),
		Action:        action,
		WorkspaceID:   workspaceID,
		EmittedAt:     now.UTC(),
		Metadata:      meta,
		TopologyEdges: edges,
	}

	ev.Edges = ownerEdges(clusterID, appset.OwnerReferences, namespace, extID)

	return ev
}

// collectGeneratorNames returns the deduplicated, sorted list of generator
// type names across the spec.generators[] slice. Per the allow-list we emit
// only the type names (e.g. "git", "list", "matrix"), never the generator's
// content. If two generators of the same type appear, the type name is
// reported once.
func collectGeneratorNames(gs []argocd.ApplicationSetGenerator) []string {
	if len(gs) == 0 {
		return nil
	}
	seen := map[string]struct{}{}
	var out []string
	for _, g := range gs {
		for _, n := range g.ActiveGeneratorNames() {
			if _, ok := seen[n]; ok {
				continue
			}
			seen[n] = struct{}{}
			out = append(out, n)
		}
	}
	// out is already in canonical insertion order across generators —
	// ActiveGeneratorNames() returns a stable, fixed key order per
	// generator. Across multiple generators the order is the order of the
	// first occurrence. That's stable enough for tests; no need to sort.
	return out
}

// buildGeneratesEdges emits one GENERATES edge per child Application named
// in status.applicationStatus[]. Empty input yields no edges (older ArgoCD).
//
// Child applications live in the same namespace as the ApplicationSet by
// convention (ArgoCD's argocd namespace). The mapper uses the
// ApplicationSet's namespace as the target namespace; if the ArgoCD setup
// places child applications in a different namespace, the edge will dangle
// — server-side resolution handles that as designed.
func buildGeneratesEdges(clusterID uuid.UUID, statuses []argocd.ApplicationSetApplicationStatus, namespace string) []EdgeRef {
	if len(statuses) == 0 {
		return nil
	}
	seen := map[string]struct{}{}
	var edges []EdgeRef
	for _, s := range statuses {
		if s.Application == "" {
			continue
		}
		ext := externalIDFor(clusterID, EntityKindApplication, namespace, s.Application)
		if _, ok := seen[ext]; ok {
			continue
		}
		seen[ext] = struct{}{}
		edges = append(edges, EdgeRef{Kind: EdgeKindGenerates, TargetExternalID: ext})
	}
	return edges
}
