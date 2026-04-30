// Package reconciler implements the operator's drift-detection worker.
//
// It periodically compares the set of external_ids the operator emits for a
// given (workspace_id, cluster_id, kind) tuple in the Omniscience graph
// against the actual list of resources in the cluster, and re-asserts the
// difference by emitting synthetic events through the existing publisher.
//
// The reconciler is the *graph-vs-cluster* drift detector. It is distinct
// from controller-runtime's per-resource Reconcile loop, which handles
// individual watch events. The two cooperate: watchers keep the graph live
// at single-event granularity; the reconciler closes any drift that opened
// up during operator downtime, NATS outages, or transient API-server
// hiccups.
//
// Field-level drift (a Pod's phase differs between graph and cluster) is
// out of scope here per ADR-0007 §Decision and issue #163 — this worker
// reconciles topology only.
//
// ACL invariant: every external_id passed to Diff is already scoped to the
// caller's (workspace_id, cluster_id) pair — see issue #163 §ACL. Diff is a
// pure set operation with no notion of workspace; the caller MUST NOT mix
// inputs from different (workspace_id, cluster_id) pairs.
package reconciler

import "sort"

// Diff computes the symmetric set difference between in-cluster and
// in-graph external_id sets and returns the two corrective sides:
//
//   - missingInGraph: external_ids present in the cluster but absent from
//     the graph. The reconciler should emit a synthetic "created" event
//     for each.
//   - missingInCluster: external_ids present in the graph but absent from
//     the cluster. The reconciler should emit a "deleted" event for each.
//
// Both result slices are sorted lexicographically so the caller's emit
// order is deterministic — easier to reason about in logs and tests.
//
// Empty inputs are valid and produce empty outputs symmetrically. The two
// inputs MUST already share the same (workspace_id, cluster_id, kind)
// scope; Diff treats them as opaque strings.
func Diff(inCluster, inGraph []string) (missingInGraph, missingInCluster []string) {
	// Build sets once; keys are external_ids which are short strings, so
	// the map overhead is trivial on cluster sizes the operator targets
	// (10k Pods → ~1MiB transient).
	cluster := make(map[string]struct{}, len(inCluster))
	for _, id := range inCluster {
		if id == "" {
			// Defensive: reject empty external_ids so a publisher bug
			// upstream cannot poison the diff. Empty would round-trip
			// as "everything missing" which is spectacularly wrong.
			continue
		}
		cluster[id] = struct{}{}
	}

	graph := make(map[string]struct{}, len(inGraph))
	for _, id := range inGraph {
		if id == "" {
			continue
		}
		graph[id] = struct{}{}
	}

	for id := range cluster {
		if _, ok := graph[id]; !ok {
			missingInGraph = append(missingInGraph, id)
		}
	}
	for id := range graph {
		if _, ok := cluster[id]; !ok {
			missingInCluster = append(missingInCluster, id)
		}
	}

	sort.Strings(missingInGraph)
	sort.Strings(missingInCluster)
	return missingInGraph, missingInCluster
}
