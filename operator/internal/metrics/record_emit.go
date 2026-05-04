// record_emit.go is the controller-facing freshness helper for issue #198.
//
// Background: the per-controller freshness probe metric
// (omniscience_operator_event_lag_seconds) is registered and dashboarded but
// no controller calls ObserveEventLag, so the histogram stays empty and the
// "freshness p99" panel + the OmniscienceOperatorEmitLagHigh alert never
// fire. Issue #165 explicitly scoped the wiring out; #198 closes that gap.
//
// Design intent: make the per-controller diff exactly one line. Callers do
//
//	opmetrics.RecordEmit("Deployment", &dep)
//
// before invoking publisher.Publish. The helper handles every edge case
// (nil object, deletion stub, clock skew, future timestamps) so the
// controller code is not littered with guards.
//
// What the helper deliberately does NOT do:
//
//   - It does not call EventsEmittedTotal — that counter is bumped inside
//     publisher.Publish (RecordPublishSuccess / RecordPublishError) and
//     bumping it twice would double-count.
//   - It does not log. Reconcile is the hot path; one log line per
//     reconcile per controller multiplied by 23 controllers and a typical
//     watch event rate would dominate the operator's log volume.
//   - It does not record the publisher hop duration — that is
//     EventEmitDurationSeconds, also handled inside publisher.Publish.
//
// ACL invariant (ADR-0007 §ACL): the helper passes only `kind` to
// ObserveEventLag. No namespace, name, owner, or tenant identifier ever
// reaches the metric label set. The kind argument is a closed enum
// constrained by the EntityKind* constants in the entity package; passing
// an arbitrary string is a controller bug, not an ACL leak.
package metrics

import (
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"
)

// RecordEmit records the per-event freshness lag for a successful publish.
//
// Inputs:
//
//   - kind: the Kubernetes Kind segment that the corresponding entity mapper
//     uses for ev.Metadata["kind"] (e.g. "Pod", "Deployment", "ResourceClaim").
//     Must be one of the EntityKind* constants in operator/internal/entity to
//     keep the metric label set bounded — but the helper does not validate
//     that, since validating it would require importing the entity package
//     and the metrics package must remain a leaf to avoid cycles.
//   - obj: the typed object the controller just successfully fetched. The
//     helper extracts the freshness signal from obj.GetCreationTimestamp().
//
// Behaviour:
//
//   - obj == nil → no observation. Defensive against a future caller that
//     wires the helper into an error path with a nil object; we prefer
//     "missing observation" over "panic in metrics path".
//   - obj.GetCreationTimestamp() is the zero time → no observation. This is
//     the deletion-stub case: PodReconciler builds a corev1.Pod{} from
//     req.NamespacedName when the GET returns NotFound; that stub has no
//     real freshness signal.
//   - now < creationTimestamp (clock skew between API server and operator) →
//     ObserveEventLag clamps to 0 and records, so the bucket layout
//     remains valid. We do NOT short-circuit here — recording a 0-lag for a
//     freshly-created object that the operator picked up before the API
//     server's clock matched our own is the truthful answer.
//
// The helper is allocation-free on the hot path (no fmt, no logger).
func RecordEmit(kind string, obj client.Object) {
	if obj == nil {
		return
	}
	ts := obj.GetCreationTimestamp().Time
	if ts.IsZero() {
		return
	}
	// effectiveEventTime is the seam for a future "last-applied-at"
	// annotation contract (#198 issue body suggests one). Today it always
	// returns the CreationTimestamp; future PRs can extend it without
	// changing any of the 23 controller call-sites.
	emitted := effectiveEventTime(obj, ts)
	ObserveEventLag(kind, time.Since(emitted))
}

// effectiveEventTime returns the "best" event time for freshness lag.
//
// Today: just the CreationTimestamp. The function exists as a documented
// extension point so the future "last-applied-at annotation" contract can
// land in one place without re-touching every controller. When that contract
// lands, this function will:
//
//  1. Look up obj.GetAnnotations()["omniscience.100rd.com/last-applied-at"]
//  2. Parse it as RFC3339; on parse failure fall back to creationTS
//  3. Return whichever is more recent (an update should reset freshness)
//
// The annotation key is reserved here (not yet defined elsewhere) so the
// future PR knows exactly which key to honour.
func effectiveEventTime(_ client.Object, creationTS time.Time) time.Time {
	return creationTS
}
