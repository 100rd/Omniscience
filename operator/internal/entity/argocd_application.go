// argocd_application.go: argoproj.io/v1alpha1/Application -> Event mapper.
//
// Topology edges (issue #160 §F — derived from K8s-native fields, never
// inferred):
//
//   - Application -[DEPLOYS]-> {Deployment | Service | Ingress | …}, derived
//     from status.resources[]. Each entry is a {group, version, kind,
//     namespace, name} the ArgoCD controller itself populated after applying
//     the rendered manifests. Targets use the canonical operator external-id
//     form k8s_resource/{Kind}/{namespace}/{name} so the server-side linker
//     auto-creates cross-references with the workload watchers (#157, #158).
//   - Application -[OWNED_BY]-> {Project} (logical) — emitted as a
//     metadata field, not a topology edge to a K8s resource. ArgoCD
//     AppProject is out-of-scope (RBAC clearance only, no controller).
//     The project value still lands in metadata so the server-side join
//     against future #166 AppProject coverage is trivial.
//   - Application -[IN_CLUSTER]-> Cluster — every K8s-resource entity carries
//     this, per #159's anchor pattern.
//
// ACL invariant: spec.destination.namespace and spec.destination.server are
// fields ArgoCD's customers populate via the CR. They are *not* tenant
// boundaries; workspace_id comes from operator config alone (ADR-0007 §ACL).
//
// Empty-field tolerance: ArgoCD CRDs ship with optional fields that only
// newer versions populate (status.applicationStatus, etc.). Every field
// access here is nil/zero-safe — missing optional fields produce missing
// metadata entries / missing edges, never a panic. This satisfies the
// CRD-version-skew acceptance criterion.
package entity

import (
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/100rd/omniscience/operator/internal/argocd"
)

// EntityKindApplication is the K8s Kind segment for ArgoCD Applications.
// Mixed case to match the Pod / workload convention.
const EntityKindApplication = "Application"

// EdgeKindDeploys: Application -> {Deployment | Service | …}, sourced from
// status.resources[]. Distinct from EdgeRelationOwns (which is for
// metadata.ownerReferences only).
const EdgeKindDeploys = "DEPLOYS"

// ApplicationToEvent maps an argocd.Application plus an action into an
// Event. Pure function: no clients, no clock injection beyond `now`.
//
// Metadata allow-list (issue §G):
//   - cluster, namespace, name, kind, emitter (base)
//   - sync_status, health_status, target_revision, operation_phase
//   - source_repo_url, source_path
//   - dest_namespace, dest_server
//   - project
//
// Empty optional fields are OMITTED rather than emitted as empty strings
// so the consumer can distinguish "not set" from "explicitly empty".
func ApplicationToEvent(
	app *argocd.Application,
	action Action,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	namespace := resolveNamespace(app.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindApplication, app.Name)

	// spec.project — logical owner (AppProject). Empty when unset; ArgoCD
	// defaults to "default" but we surface what the CR actually carries.
	if app.Spec.Project != "" {
		meta["project"] = app.Spec.Project
	}

	// spec.source.* — pointer-safe (older CR shapes use a *Source; nil-safe).
	if app.Spec.Source != nil {
		if v := app.Spec.Source.RepoURL; v != "" {
			meta["source_repo_url"] = v
		}
		if v := app.Spec.Source.Path; v != "" {
			meta["source_path"] = v
		}
		if v := app.Spec.Source.TargetRevision; v != "" {
			meta["target_revision"] = v
		}
	}

	// spec.destination.*
	if v := app.Spec.Destination.Namespace; v != "" {
		meta["dest_namespace"] = v
	}
	if v := app.Spec.Destination.Server; v != "" {
		meta["dest_server"] = v
	}

	// status — every nested field is zero-safe.
	if v := app.Status.Sync.Status; v != "" {
		meta["sync_status"] = v
	}
	if v := app.Status.Health.Status; v != "" {
		meta["health_status"] = v
	}
	if app.Status.OperationState != nil && app.Status.OperationState.Phase != "" {
		meta["operation_phase"] = app.Status.OperationState.Phase
	}

	extID := externalIDFor(clusterID, EntityKindApplication, namespace, app.Name)

	edges := buildApplicationDeploysEdges(clusterID, app.Status.Resources)
	edges = append(edges, inClusterEdge(clusterID, clusterName))

	ev := &Event{
		SourceID:      DeriveSourceID(workspaceID, clusterName),
		SourceType:    SourceType,
		ExternalID:    extID,
		URI:           uriFor(clusterName, namespace, EntityKindApplication, app.Name),
		Action:        action,
		WorkspaceID:   workspaceID,
		EmittedAt:     now.UTC(),
		Metadata:      meta,
		TopologyEdges: edges,
	}

	// Owner-edges from metadata.ownerReferences. Applications are typically
	// top-of-chain but can be created by an ApplicationSet — when that
	// happens, the ApplicationSet controller writes the ownerReference on
	// the child Application. Honour whatever K8s populates; do not invent.
	ev.Edges = ownerEdges(clusterID, app.OwnerReferences, namespace, extID)

	return ev
}

// buildApplicationDeploysEdges emits one DEPLOYS edge per managed resource
// in status.resources[]. Resources without a Kind are skipped (defensive —
// status.resources entries are populated by ArgoCD itself, but we don't
// trust the CR to be well-formed). Cluster-scoped resources (no namespace)
// inherit the standard "default" fallback so external-ids are well-formed.
//
// Edges to resources outside the operator's coverage (e.g. CRDs not in
// the watch set) emit dangling edges; per the issue body the server-side
// ingestion worker handles dangling-edge resolution.
func buildApplicationDeploysEdges(clusterID uuid.UUID, resources []argocd.ResourceStatus) []EdgeRef {
	if len(resources) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(resources))
	var edges []EdgeRef
	for _, r := range resources {
		if r.Kind == "" || r.Name == "" {
			// Malformed entry — cannot build a well-formed external-id.
			continue
		}
		ns := resolveNamespace(r.Namespace)
		ext := externalIDFor(clusterID, strings.TrimSpace(r.Kind), ns, r.Name)
		if _, ok := seen[ext]; ok {
			continue
		}
		seen[ext] = struct{}{}
		edges = append(edges, EdgeRef{Kind: EdgeKindDeploys, TargetExternalID: ext})
	}
	return edges
}
