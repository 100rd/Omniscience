// Package entity — DRA ResourceClaim mapping.
//
// ResourceClaim (resource.k8s.io/v1beta1) is the namespaced object that
// describes a request for one or more devices (typically GPUs / NICs / FPGAs)
// in the Dynamic Resource Allocation API. Each Pod that needs DRA-managed
// devices declares the claim it depends on via spec.resourceClaims[]; the
// scheduler resolves the claim to a concrete allocation against a DeviceClass
// and one or more ResourceSlices.
//
// ACL allow-list (issue #162, ADR-0007 §ACL): cluster, namespace, name, kind,
// device_count, phase, emitter. User-supplied labels and annotations are
// NEVER copied through; tenancy is set from operator config.
//
// Topology edges emitted:
//   - IN_CLUSTER -> Cluster anchor (mirrors every workload mapper).
//   - ALLOCATES   -> DeviceClass(es) named by spec.devices.requests[].deviceClassName.
//
// The Pod -[CLAIMS]-> ResourceClaim edge belongs on the Pod side
// (pod.spec.resourceClaims[]) — see pod_controller.go. Issue #162 explicitly
// flags this as a follow-up; this mapper does not synthesise reverse edges.
//
// Notes on edges that the spec mentions but are NOT emitted here:
//   - USES_TEMPLATE: ResourceClaim has no spec.resourceClaimTemplate field
//     in v1beta1. The link from a generated claim back to its template is
//     only recoverable via the owning Pod's spec.resourceClaims[].
//     resourceClaimTemplateName. Edge derivation thus belongs on the Pod
//     side — emitting from here would require inferring relationships,
//     which violates ADR-0007 §causal-fidelity. Documented as a follow-up.
package entity

import (
	"sort"
	"strconv"
	"time"

	"github.com/google/uuid"
	resourcev1beta1 "k8s.io/api/resource/v1beta1"
)

// EntityKindResourceClaim is the K8s kind segment in ResourceClaim
// external_ids.
const EntityKindResourceClaim = "ResourceClaim"

// EntityKindDeviceClass is the K8s kind segment in DeviceClass external_ids.
// Declared here (not in dra_deviceclass.go) so the ALLOCATES edge target
// builder can be co-located with the claim mapper that emits it.
const EntityKindDeviceClass = "DeviceClass"

// EdgeKindAllocates is the predicate for the ResourceClaim -> DeviceClass
// edge. Server-side ingestion materialises this as the ALLOCATES edge in the
// bitemporal graph; the value is the literal string the consumer switches on.
const EdgeKindAllocates = "ALLOCATES"

// ResourceClaimToEvent maps a v1beta1.ResourceClaim plus an action into an
// Event. Pure function — no client, no clock injection beyond `now`.
//
// The phase metadata field is derived from spec/status:
//   - Allocation == nil                        -> "Pending"
//   - Allocation != nil && len(ReservedFor)==0 -> "Allocated"
//   - Allocation != nil && len(ReservedFor)>0  -> "Reserved"
//
// device_count counts the allocated devices in status.allocation.devices.results.
// When unallocated (Pending) it falls back to the requested count from
// spec.devices.requests so callers can still see "how many were asked for".
func ResourceClaimToEvent(rc *resourcev1beta1.ResourceClaim, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := defaultedNamespace(rc.Namespace)
	externalID := externalID(clusterID, EntityKindResourceClaim, namespace, rc.Name)
	uri := resourceURI(clusterName, namespace, EntityKindResourceClaim, rc.Name)

	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindResourceClaim, rc.Name)
	meta["device_count"] = strconv.Itoa(deviceCount(rc))
	meta["phase"] = string(claimPhase(rc))

	edges := []EdgeRef{inClusterEdge(clusterID, clusterName)}
	for _, target := range allocatesEdgeTargets(rc) {
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
		Edges:         ownerEdges(clusterID, rc.OwnerReferences, namespace, externalID),
		TopologyEdges: edges,
	}
}

// ResourceClaimPhase is the operator-side discriminated phase. K8s does not
// expose a single phase field on ResourceClaim — the phase is derived from
// the presence of an allocation and from the reservedFor list. Centralising
// the derivation here means tests can pin the mapping in one place.
type ResourceClaimPhase string

const (
	// ResourceClaimPhasePending = no allocation yet; scheduler has not bound
	// devices to this claim.
	ResourceClaimPhasePending ResourceClaimPhase = "Pending"
	// ResourceClaimPhaseAllocated = allocation is set, but no consumer has
	// reserved the devices yet.
	ResourceClaimPhaseAllocated ResourceClaimPhase = "Allocated"
	// ResourceClaimPhaseReserved = allocation is set AND at least one
	// consumer (Pod) is reserved against the claim.
	ResourceClaimPhaseReserved ResourceClaimPhase = "Reserved"
)

// claimPhase returns the discriminated phase. See ResourceClaimPhase.
func claimPhase(rc *resourcev1beta1.ResourceClaim) ResourceClaimPhase {
	if rc.Status.Allocation == nil {
		return ResourceClaimPhasePending
	}
	if len(rc.Status.ReservedFor) == 0 {
		return ResourceClaimPhaseAllocated
	}
	return ResourceClaimPhaseReserved
}

// deviceCount returns the number of allocated devices on the claim. When the
// claim is still Pending we report the number of *requested* devices instead
// (one per spec.devices.requests entry) so the operator can surface "asked for
// 4 GPUs" before the scheduler binds them.
func deviceCount(rc *resourcev1beta1.ResourceClaim) int {
	if rc.Status.Allocation != nil {
		return len(rc.Status.Allocation.Devices.Results)
	}
	return len(rc.Spec.Devices.Requests)
}

// allocatesEdgeTargets returns the deduplicated, sorted list of DeviceClass
// external_ids referenced by the claim's requests. DeviceClass is cluster-
// scoped so the external_id has no namespace component.
//
// Sorting + dedup gives stable serialisation across reconciles; two claims
// with the same effective set of classes produce identical edge lists.
func allocatesEdgeTargets(rc *resourcev1beta1.ResourceClaim) []string {
	seen := make(map[string]struct{})
	for _, req := range rc.Spec.Devices.Requests {
		name := req.DeviceClassName
		if name == "" {
			// DeviceClassName is required by the API server; an empty value
			// would be malformed input. Skip rather than emit a dangling
			// edge to "k8s_resource/DeviceClass/" — see persistentvolume.go
			// for the same defensive pattern with empty StorageClassName.
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
