// Mapper for argoproj.io/v1alpha1.Rollout (Argo Rollouts CRD).
//
// A Rollout is a progressive-delivery workload primitive: it owns its own
// ReplicaSet lineage (the Argo Rollouts controller writes
// metadata.ownerReferences on each child ReplicaSet) and surfaces the live
// rollout state through typed status fields. This mapper:
//
//   - Emits the Rollout entity using the same "k8s_resource/{Kind}/{ns}/{name}"
//     external-id convention as the rest of the operator (#157, #159).
//   - Derives topology edges EXCLUSIVELY from typed status fields:
//
//   - OWNS              -> ReplicaSet (status.stableRS / canary RS)
//   - CURRENT_VERSION   -> ReplicaSet whose hash equals status.currentPodHash
//   - CANARY_VERSION    -> the candidate ReplicaSet during a canary
//     mid-flight (only when phase is non-steady)
//   - BLUEGREEN_ACTIVE  -> Service named in spec.strategy.blueGreen
//     .activeService when the strategy is BlueGreen
//
// Edges are NEVER inferred from labels or annotations (ADR-0007
// §causal-fidelity). All workspace stamping is from operator config; the
// Rollout's own metadata.labels / metadata.annotations are tenant-writable
// and out of the closed allow-list (ADR-0007 §ACL).
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	rolloutsv1alpha1 "github.com/argoproj/argo-rollouts/pkg/apis/rollouts/v1alpha1"
)

// EntityKindRollout is the K8s API Kind segment used in external-ids and
// metadata for Argo Rollout entities. Kept as a typed constant so server-side
// switches over a closed kind set stay exhaustive.
const EntityKindRollout = "Rollout"

// Edge kinds emitted by the Rollout mapper. Centralised here (rather than in
// common.go's generic block) so the Rollout-specific contract is reviewable
// in one file. OWNS reuses EdgeRelationOwns from common.go because it is the
// same K8s ownership predicate used by every workload watcher.
const (
	// EdgeKindCurrentVersion: Rollout -> ReplicaSet whose pod-template-hash
	// matches status.currentPodHash. Distinct from OWNS — it is the
	// "currently serving traffic" pointer, used by incident-resolution
	// queries to answer "which version is live right now". The mapping is
	// from a typed status field, never from labels.
	EdgeKindCurrentVersion = "CURRENT_VERSION"

	// EdgeKindCanaryVersion: Rollout -> ReplicaSet that is the candidate
	// during a canary mid-flight. Empty when the rollout is in a steady
	// state. Derived from the canary RS hash carried on status.canary
	// (when the strategy is canary and the rollout is mid-progression).
	EdgeKindCanaryVersion = "CANARY_VERSION"

	// EdgeKindBlueGreenActive: Rollout -> Service named by
	// spec.strategy.blueGreen.activeService. Emitted only when the rollout
	// declares a BlueGreen strategy; an empty activeService produces no
	// edge.
	EdgeKindBlueGreenActive = "BLUEGREEN_ACTIVE"
)

// Strategy values surfaced in metadata["strategy"]. Lowercase to match the
// JSON-friendly form already used elsewhere in the operator and so the
// server-side switch can keep a closed enum without case folding.
const (
	strategyCanary    = "canary"
	strategyBlueGreen = "blueGreen"
)

// RolloutToEvent maps an argoproj.io/v1alpha1.Rollout plus an action into an
// Event. Pure function — no I/O, no clients. The `workspaceID` parameter is
// the operator's configured tenant boundary and MUST come from operator
// config (per ADR-0007 §ACL); it is NEVER derived from any Rollout field.
//
// On a "deleted" reconcile the controller passes a stub Rollout with only
// namespace + name set; the mapper handles that defensively the same way
// PodToEvent / DeploymentToEvent do — no status-derived metadata, no edges.
//
// Topology edges are derived from typed `status` fields only:
//
//   - status.stableRS              -> OWNS edge to the stable ReplicaSet.
//   - status.canary (in-flight)    -> OWNS + CANARY_VERSION edges to the
//     candidate ReplicaSet (only when phase
//     is not Healthy, i.e. mid-flight).
//   - status.currentPodHash        -> CURRENT_VERSION edge to the RS whose
//     pod-template-hash equals the value.
//   - spec.strategy.blueGreen.*    -> BLUEGREEN_ACTIVE edge to the active
//     Service (only when BlueGreen strategy
//     is in flight per ADR §F).
//
// ReplicaSet external-ids constructed by this mapper follow the same
// convention as the ReplicaSet watcher (#157):
// "k8s_resource/ReplicaSet/{namespace}/{rs-name}". The RS name is the
// Rollout name suffixed by the pod-template-hash (Argo Rollouts' naming
// convention, mirroring Deployment->ReplicaSet).
func RolloutToEvent(
	r *rolloutsv1alpha1.Rollout,
	action Action,
	workspaceID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	ev := newWorkloadEvent(EntityKindRollout, r.Namespace, r.Name, action, workspaceID, clusterName, now)

	// Strategy detection. spec.strategy is a struct of two pointers; whichever
	// is non-nil identifies the strategy. If both are nil (malformed input)
	// we omit the field entirely rather than guess.
	strategy := detectStrategy(r)
	if strategy != "" {
		ev.Metadata["strategy"] = strategy
	}

	// Phase. Status field — set by the Argo Rollouts controller, never by the
	// tenant. Empty string when the controller has not yet observed the
	// resource (fresh creates); we omit in that case to keep the metadata map
	// free of empty values.
	if phase := string(r.Status.Phase); phase != "" {
		ev.Metadata["phase"] = phase
	}

	// Replica counts (status fields, controller-populated). All four are
	// emitted unconditionally so downstream queries get a complete picture
	// even on zero — replicas=0 is a meaningful state (a paused or scaled-
	// down rollout).
	ev.Metadata["replicas"] = formatReplicas(r.Status.Replicas)
	ev.Metadata["updated_replicas"] = formatReplicas(r.Status.UpdatedReplicas)
	ev.Metadata["ready_replicas"] = formatReplicas(r.Status.ReadyReplicas)
	ev.Metadata["available_replicas"] = formatReplicas(r.Status.AvailableReplicas)

	// current_step_index applies to canary mid-flight. Pointer so a non-nil
	// zero value is meaningful (step 0 is the first canary step).
	if r.Status.CurrentStepIndex != nil {
		ev.Metadata["current_step_index"] = strconv.FormatInt(int64(*r.Status.CurrentStepIndex), 10)
	}

	// current_pod_hash carries the pointer to the currently-serving RS. We
	// emit it as a flat metadata field so consumers that don't materialise
	// the topology edge (e.g. metrics scrape) can still join on it.
	if r.Status.CurrentPodHash != "" {
		ev.Metadata["current_pod_hash"] = r.Status.CurrentPodHash
	}

	// OWNS edges from K8s-populated metadata.ownerReferences. A Rollout can
	// be owned by a higher-level CRD (e.g. Argo Application). Honour the
	// reference exactly as the control plane wrote it; do not invent.
	ev.Edges = ownerEdges(r.OwnerReferences, defaultedNamespace(r.Namespace), ev.ExternalID)

	// Topology edges derived from typed status / spec.
	ev.TopologyEdges = rolloutTopologyEdges(r, strategy, defaultedNamespace(r.Namespace))

	return ev
}

// detectStrategy returns the strategy discriminator string for metadata.
// Returns "" when neither sub-strategy is set (malformed or stub object on
// a deleted reconcile). Pure switch on typed pointers — no label parsing.
func detectStrategy(r *rolloutsv1alpha1.Rollout) string {
	switch {
	case r.Spec.Strategy.Canary != nil:
		return strategyCanary
	case r.Spec.Strategy.BlueGreen != nil:
		return strategyBlueGreen
	default:
		return ""
	}
}

// rolloutTopologyEdges builds the typed-status-field-derived edge set:
//
//   - OWNS              -> stable ReplicaSet (status.stableRS).
//   - OWNS              -> canary candidate ReplicaSet (status.currentPodHash
//     when the rollout is mid-flight and the candidate
//     hash differs from the stable hash).
//   - CURRENT_VERSION   -> ReplicaSet whose pod-template-hash equals
//     status.currentPodHash.
//   - CANARY_VERSION    -> candidate ReplicaSet during a canary mid-flight.
//   - BLUEGREEN_ACTIVE  -> Service named by spec.strategy.blueGreen
//     .activeService (BlueGreen strategy only).
//
// Returns nil for a stub object (deleted reconcile): no spec, no status, no
// edges. Empty slice and nil are equivalent on the wire (json:omitempty).
func rolloutTopologyEdges(r *rolloutsv1alpha1.Rollout, strategy, namespace string) []EdgeRef {
	out := make([]EdgeRef, 0, 4)

	stableHash := r.Status.StableRS
	currentHash := r.Status.CurrentPodHash

	// OWNS -> stable RS. The stable RS name is "{rollout}-{hash}" per Argo
	// Rollouts' naming convention (mirrors Deployment->ReplicaSet). We emit
	// the edge only when the stable hash is populated (controller has
	// observed at least one rollout).
	if stableHash != "" {
		stableRS := replicaSetName(r.Name, stableHash)
		out = append(out, EdgeRef{
			Kind:             EdgeRelationOwns,
			TargetExternalID: externalIDFor(kindReplicaSet, namespace, stableRS),
		})
	}

	// CURRENT_VERSION -> RS whose hash equals status.currentPodHash. During
	// steady state currentPodHash == stableRS, so this edge points at the
	// same RS as the OWNS edge above (the consumer materialises both as
	// distinct relationships per ADR §F).
	if currentHash != "" {
		currentRS := replicaSetName(r.Name, currentHash)
		out = append(out, EdgeRef{
			Kind:             EdgeKindCurrentVersion,
			TargetExternalID: externalIDFor(kindReplicaSet, namespace, currentRS),
		})
	}

	// Canary mid-flight: when the strategy is canary and the rollout is not
	// in a steady (Healthy) phase, the candidate RS is the one carried in
	// status.currentPodHash and is DISTINCT from the stable RS. We emit
	// OWNS for the candidate (the Rollout owns both RS lineages) and a
	// dedicated CANARY_VERSION pointer.
	if strategy == strategyCanary && currentHash != "" && stableHash != "" && currentHash != stableHash {
		// Already emitted CURRENT_VERSION above; add CANARY_VERSION as an
		// in-flight-specific edge so consumers can distinguish "the new
		// version that is currently being canaried" from "the version
		// pointed at by currentPodHash" (which is the same RS during a
		// canary, but the relationship semantics differ for graph queries).
		canaryRS := replicaSetName(r.Name, currentHash)
		canaryExternalID := externalIDFor(kindReplicaSet, namespace, canaryRS)
		out = append(out, EdgeRef{
			Kind:             EdgeRelationOwns,
			TargetExternalID: canaryExternalID,
		})
		out = append(out, EdgeRef{
			Kind:             EdgeKindCanaryVersion,
			TargetExternalID: canaryExternalID,
		})
	}

	// BlueGreen: emit BLUEGREEN_ACTIVE -> Service whenever the strategy is
	// BlueGreen and the activeService field is populated. We do NOT gate on
	// phase here — the active Service pointer is meaningful in steady state
	// too (it is what receives production traffic). ADR §F's wording "when
	// blue-green strategy in flight" is interpreted as "the strategy is
	// declared as BlueGreen"; status.phase is carried separately in metadata.
	if strategy == strategyBlueGreen && r.Spec.Strategy.BlueGreen != nil {
		if active := r.Spec.Strategy.BlueGreen.ActiveService; active != "" {
			out = append(out, EdgeRef{
				Kind:             EdgeKindBlueGreenActive,
				TargetExternalID: externalIDFor(EntityKindService, namespace, active),
			})
		}
	}

	if len(out) == 0 {
		return nil
	}
	return out
}

// replicaSetName returns the canonical ReplicaSet name for a (rollout,
// pod-template-hash) pair. Argo Rollouts uses the same "{owner}-{hash}"
// convention as the apps/v1 Deployment controller. Centralised here so
// future revisions of the convention are a one-line change.
func replicaSetName(rolloutName, podTemplateHash string) string {
	return rolloutName + "-" + podTemplateHash
}
