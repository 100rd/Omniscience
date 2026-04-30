// Package entity — DRA ResourceSlice mapping.
//
// ResourceSlice (resource.k8s.io/v1beta1) is a cluster-scoped object that
// the DRA driver publishes to advertise the devices a node (or pool of nodes)
// makes available. The slice spec carries the driver name, the pool, and the
// list of devices being advertised. Schedulers and ResourceClaim allocation
// read from these slices to decide where a claim can be satisfied.
//
// ACL allow-list (issue #162, ADR-0007 §ACL): cluster, name, kind,
// device_count, driver_name, pool, emitter. The pool name is included
// because it is part of the immutable identity of a slice (driver+pool+
// generation), not tenant-writable state.
//
// Topology edges:
//   - IN_CLUSTER     -> Cluster anchor (same shape as Node, PV, StorageClass).
//   - ADVERTISED_BY  -> Node, when spec.nodeName is set. NodeSelector and
//     AllNodes forms (the cluster-wide pool case) intentionally produce no
//     edge — the operator does not resolve selectors to concrete nodes
//     (matches NetworkPolicy / Service behaviour: selectors are tenant-
//     mutable; resolution lives server-side).
//
// Notes on edges that issue #162 mentions but are NOT emitted here:
//   - ADVERTISES_CLASS (-> DeviceClass): in v1beta1 the link from a slice's
//     advertised devices to a DeviceClass is *implicit* — there is no
//     deviceClassName field on Device or BasicDevice. DeviceClass selectors
//     are evaluated by the scheduler against device attributes/capacity at
//     allocation time. Emitting this edge from the slice mapper would
//     require evaluating selectors locally, which is exactly what ADR-0007
//     §causal-fidelity forbids. Documented as a follow-up; the edge is
//     instead emitted from the ResourceClaim side via ALLOCATES, which
//     is K8s-native (deviceClassName is a literal field on the request).
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	resourcev1beta1 "k8s.io/api/resource/v1beta1"
)

// EntityKindResourceSlice is the K8s kind segment in ResourceSlice
// external_ids.
const EntityKindResourceSlice = "ResourceSlice"

// EntityKindNode is the K8s kind segment for Node external_ids — used by the
// ADVERTISED_BY edge target builder. Co-located here rather than in node.go
// because the existing node.go pre-dates the EntityKind* convention; lifting
// it back is out of scope for #162.
const EntityKindNode = "Node"

// EdgeKindAdvertisedBy is the predicate for the ResourceSlice -> Node edge.
// Emitted only when spec.nodeName is set; cluster-wide pools (NodeSelector /
// AllNodes) do not produce an edge.
const EdgeKindAdvertisedBy = "ADVERTISED_BY"

// ResourceSliceToEvent maps a v1beta1.ResourceSlice into an Event. Pure
// function — no client, no clock injection beyond `now`.
//
// ResourceSlice is cluster-scoped, so the external_id has no namespace
// component: k8s_resource/ResourceSlice/{name}.
func ResourceSliceToEvent(rs *resourcev1beta1.ResourceSlice, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	externalID := EntityKindPrefix + "/" + EntityKindResourceSlice + "/" + rs.Name
	uri := "kube://" + clusterName + "/" + EntityKindResourceSlice + "/" + rs.Name

	meta := map[string]string{
		"cluster":      clusterName,
		"name":         rs.Name,
		"kind":         EntityKindResourceSlice,
		"device_count": strconv.Itoa(len(rs.Spec.Devices)),
		"emitter":      EmitterLabel,
	}
	if rs.Spec.Driver != "" {
		meta["driver_name"] = rs.Spec.Driver
	}
	if rs.Spec.Pool.Name != "" {
		meta["pool"] = rs.Spec.Pool.Name
	}
	if rs.Spec.NodeName != "" {
		meta["node"] = rs.Spec.NodeName
	}

	edges := []EdgeRef{inClusterEdge(clusterID, clusterName)}
	if rs.Spec.NodeName != "" {
		edges = append(edges, EdgeRef{
			Kind:             EdgeKindAdvertisedBy,
			TargetExternalID: EntityKindPrefix + "/" + EntityKindNode + "/" + rs.Spec.NodeName,
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
		TopologyEdges: edges,
	}
}
