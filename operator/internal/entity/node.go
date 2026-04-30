// Package entity — Node mapping.
//
// Nodes are cluster-scoped, so the external_id has no namespace component
// (k8s_resource/Node/{name}). The metadata allow-list is locked to the
// fields named in issue #159: cluster, name, kind, ready, kubelet_version,
// os_image, arch, emitter. User-supplied labels and annotations are NEVER
// copied — see ADR-0007 §ACL: a Node's labels (zone, role, etc.) are
// tenant-writable state and must not flow into the entity payload, even
// though the temptation to copy node-role labels is high.
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
)

// NodeToEvent maps a corev1.Node into an Event. Pure; takes no clients.
func NodeToEvent(node *corev1.Node, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	externalID := externalIDForClusterScoped(clusterID, "Node", node.Name)
	uri := "kube://" + clusterName + "/Node/" + node.Name

	meta := map[string]string{
		"cluster":    clusterName,
		"cluster_id": clusterID.String(),
		"name":       node.Name,
		"kind":       "Node",
		"ready":      strconv.FormatBool(nodeIsReady(node)),
		"emitter":    "k8s-operator",
	}
	// Optional fields: only emitted when the API server has populated them.
	// Missing keys are clearer to the consumer than empty-string keys.
	if v := node.Status.NodeInfo.KubeletVersion; v != "" {
		meta["kubelet_version"] = v
	}
	if v := node.Status.NodeInfo.OSImage; v != "" {
		meta["os_image"] = v
	}
	if v := node.Status.NodeInfo.Architecture; v != "" {
		meta["arch"] = v
	}

	// Topology edge: Node IN_CLUSTER {cluster_name}. The Cluster anchor
	// is the inverse-direction CONTAINS view — emitted from the startup
	// hook for Lead-side bookkeeping; the per-Node IN_CLUSTER edge is
	// what the consumer actually walks.
	edges := []EdgeRef{inClusterEdge(clusterID, clusterName)}

	return &Event{
		SourceID:    deriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalID,
		URI:         uri,
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
		TopologyEdges:       edges,
	}
}

// nodeIsReady inspects the conditions slice for the Ready condition and
// returns true iff its status is "True". Returns false if the condition is
// absent — a Node without a Ready condition is treated as not-ready, which
// matches kubectl get nodes' display behaviour.
func nodeIsReady(node *corev1.Node) bool {
	for _, c := range node.Status.Conditions {
		if c.Type == corev1.NodeReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}
