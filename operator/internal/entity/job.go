// Mapper for batchv1.Job.
//
// A Job owns Pods (job-controller writes ownerReferences on each Pod).
// Pod-side reconciliation emits Job→Pod edges from the child side. This
// mapper emits the Job's own ownerReferences (typically empty, but a
// CronJob will appear here when CronJob coverage lands in a follow-up
// sub-issue — until then a Job from a CronJob still surfaces the edge
// because K8s itself populates the CronJob ownerReference). See
// ADR-0007 §causal-fidelity.
package entity

import (
	"time"

	"github.com/google/uuid"
	batchv1 "k8s.io/api/batch/v1"
)

const kindJob = "Job"

// JobToEvent maps a batchv1.Job plus an action into an Event.
//
// Replicas semantics: Job has spec.parallelism (how many Pods may run
// concurrently) and status.active / status.succeeded. We map:
//
//   - "replicas" = spec.parallelism (desired concurrency)
//   - "available_replicas" = status.active (currently running)
//
// This deliberately collapses succeeded/failed counts because the workload
// metadata allow-list is closed (ADR-0007 §ACL). Operators that need
// completion telemetry should consume the raw Job events on a follow-up
// sub-issue (#165 — metrics/dashboards).
func JobToEvent(
	j *batchv1.Job,
	action Action,
	workspaceID uuid.UUID,
	clusterName string,
	now time.Time,
) *Event {
	ev := newWorkloadEvent(kindJob, j.Namespace, j.Name, action, workspaceID, clusterName, now)

	if j.Spec.Parallelism != nil {
		ev.Metadata["replicas"] = formatReplicas(*j.Spec.Parallelism)
	}
	ev.Metadata["available_replicas"] = formatReplicas(j.Status.Active)

	ev.Edges = ownerEdges(j.OwnerReferences, defaultedNamespace(j.Namespace), ev.ExternalID)

	return ev
}
