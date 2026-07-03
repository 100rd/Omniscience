// The Worker type and its run loop. The worker:
//
//   - Implements sigs.k8s.io/controller-runtime/pkg/manager.Runnable so the
//     manager can start it as a managed goroutine.
//   - Implements manager.LeaderElectionRunnable returning true so it
//     participates in the existing controller-runtime leader-election lease
//     (#101). Two operator replicas with leader-elect enabled will see only
//     the leader's worker tick — the second never holds the lease.
//   - Wakes on a configurable ticker (OMNISCIENCE_RECONCILE_INTERVAL).
//   - For each kind: lists in-cluster, lists in-graph (HTTP), diffs, and
//     either emits corrective events or, when dry-run is enabled, only
//     records the metrics.
//
// ACL invariant restated:
//   - The HTTP client carries a Secret-mounted bearer token. The server
//     decides whether the token's workspace matches the requested
//     workspace_id query parameter and 403s on mismatch.
//   - Diff is a pure set operation. The reconciler MUST NEVER cross-
//     reference data outside the queried (workspace_id, cluster_id) slice.
//   - Logs and metrics include `kind` but never `workspace_id`. Dry-run
//     log lines name only the in-cluster resource (already tenanted by
//     the operator's own watch coverage); the in-graph payload is NEVER
//     logged in the missing-in-cluster direction — only the count.
package reconciler

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/go-logr/logr"
	"github.com/google/uuid"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/Omniscience/operator/internal/entity"
	"github.com/100rd/Omniscience/operator/internal/publisher"
)

// Worker is the reconciliation worker singleton. One per operator process.
// Construction is via New; the zero value is invalid.
type Worker struct {
	// k8sClient lists in-cluster resources. The controller-runtime cached
	// client is reused — it shares the manager's informer cache, so the
	// reconciler does not pay an extra API-server List on every cycle for
	// kinds the watcher already informers (Pod, Deployment, ...).
	k8sClient client.Client

	// apiClient queries the Omniscience read API for in-graph entities.
	apiClient EntitiesAPIClient

	// pub re-enters the existing publisher path; the reconciler does not
	// know or care that publishes go to NATS — it just calls Publish.
	pub publisher.Publisher

	// workspaceID, clusterID, clusterName are the identity tuple stamped
	// on every emitted event. Sourced from operator config (Secret-
	// mounted) per ADR-0007 §ACL — never from cluster state.
	workspaceID uuid.UUID
	clusterID   uuid.UUID
	clusterName string

	// interval is the ticker period between reconcile cycles. Default 15m
	// per issue #163; tunable via OMNISCIENCE_RECONCILE_INTERVAL.
	interval time.Duration

	// dryRun toggles emission. When true, the worker logs and increments
	// metrics but emits zero events. Used for first-time-on-customer-
	// cluster validation before flipping to live.
	dryRun bool

	// kinds is the per-kind handler list. Defaults to kindRegistry();
	// tests inject a narrowed slice for kind-specific assertions.
	kinds []KindHandler

	// now is injected for deterministic test timestamps. Production uses
	// time.Now.
	now func() time.Time
}

// Options is the constructor input. All fields except Kinds are required.
type Options struct {
	K8sClient   client.Client
	APIClient   EntitiesAPIClient
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Interval    time.Duration
	DryRun      bool
	// Kinds, when non-nil, overrides the default kindRegistry. Production
	// callers leave this nil; tests narrow to one or two kinds.
	Kinds []KindHandler
	// Now overrides the clock. Production callers leave this nil.
	Now func() time.Time
}

// New validates inputs and returns a configured Worker. The validation
// surface mirrors the existing controller constructors (NewPodReconciler
// etc.) so misconfiguration fails the same way at the same layer.
func New(o Options) (*Worker, error) {
	if o.K8sClient == nil {
		return nil, errors.New("reconciler: k8s client must not be nil")
	}
	if o.APIClient == nil {
		return nil, errors.New("reconciler: api client must not be nil")
	}
	if o.Publisher == nil {
		return nil, errors.New("reconciler: publisher must not be nil")
	}
	if o.WorkspaceID == uuid.Nil {
		return nil, errors.New("reconciler: workspace id must not be the zero UUID")
	}
	if o.ClusterID == uuid.Nil {
		return nil, errors.New("reconciler: cluster id must not be the zero UUID")
	}
	if o.ClusterName == "" {
		return nil, errors.New("reconciler: cluster name is required")
	}
	if o.Interval <= 0 {
		return nil, errors.New("reconciler: interval must be positive")
	}
	kinds := o.Kinds
	if kinds == nil {
		kinds = kindRegistry()
	}
	now := o.Now
	if now == nil {
		now = time.Now
	}
	return &Worker{
		k8sClient:   o.K8sClient,
		apiClient:   o.APIClient,
		pub:         o.Publisher,
		workspaceID: o.WorkspaceID,
		clusterID:   o.ClusterID,
		clusterName: o.ClusterName,
		interval:    o.Interval,
		dryRun:      o.DryRun,
		kinds:       kinds,
		now:         now,
	}, nil
}

// NeedLeaderElection makes Worker a manager.LeaderElectionRunnable so the
// controller-runtime manager only ticks the worker on the leader replica.
// Returning true means: "I should run only when this replica holds the
// leader-election lease" — the standard pattern for singleton background
// workers in a controller.
func (w *Worker) NeedLeaderElection() bool {
	return true
}

// Start runs the reconcile loop until ctx is cancelled. Implements
// manager.Runnable so the manager can mgr.Add(worker).
//
// The loop performs an immediate first cycle, then waits one interval
// between subsequent cycles. The immediate first cycle keeps acceptance
// tests reasonable (no need to wait 15 minutes after operator startup) and
// matches the watcher path's "publish on initial sync" behaviour.
//
// Errors during a cycle are logged and counted but do NOT abort the loop —
// a transient API-server or NATS hiccup must not wedge reconciliation
// indefinitely. A pathological persistent error surface is observed via
// the runs_total{result="error"} metric, which dashboards page on.
func (w *Worker) Start(ctx context.Context) error {
	logger := log.FromContext(ctx).WithName("reconciler")
	logger.Info(
		"reconciler.start",
		"interval", w.interval.String(),
		"dry_run", w.dryRun,
		"kinds", len(w.kinds),
		"cluster", w.clusterName,
		// Deliberately do NOT log workspace_id at INFO — see ACL invariant
		// #4. It is available in operator config logs at startup; logging
		// here on every Start would put it on every line of the worker.
	)

	// Run one cycle immediately. If ctx is cancelled before the cycle
	// completes, the per-cycle context propagates the cancel and the
	// cycle exits naturally.
	w.runCycle(ctx, logger)

	t := time.NewTicker(w.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			logger.Info("reconciler.stop", "reason", ctx.Err().Error())
			return nil
		case <-t.C:
			w.runCycle(ctx, logger)
		}
	}
}

// RunOnce executes a single reconcile cycle synchronously. Exposed for
// integration tests that need deterministic per-cycle assertions without
// racing the ticker. Production code goes through Start.
func (w *Worker) RunOnce(ctx context.Context) {
	w.runCycle(ctx, log.FromContext(ctx).WithName("reconciler"))
}

// runCycle reconciles every kind once. Errors per kind are logged and the
// metric error counter increments; the loop continues to the next kind.
func (w *Worker) runCycle(ctx context.Context, logger logr.Logger) {
	for _, h := range w.kinds {
		if ctx.Err() != nil {
			return
		}
		w.runKind(ctx, logger, h)
	}
}

// runKind reconciles a single kind. The function is small and named so
// stack traces in production point at the right kind on panic.
func (w *Worker) runKind(ctx context.Context, logger logr.Logger, h KindHandler) {
	kindLogger := logger.WithValues("kind", h.Name)
	start := w.now()
	defer func() {
		reconcileDurationSeconds.WithLabelValues(h.Name).Observe(time.Since(start).Seconds())
	}()

	// 1. List in-cluster.
	inCluster, err := h.ListInCluster(ctx, w.k8sClient, w.clusterID)
	if err != nil {
		reconcileRunsTotal.WithLabelValues(h.Name, RunResultError).Inc()
		kindLogger.Error(err, "reconcile.list_cluster_failed")
		return
	}
	clusterIDs := keysOf(inCluster)
	reconcileInClusterCount.WithLabelValues(h.Name).Set(float64(len(clusterIDs)))

	// 2. List in-graph.
	graphIDs, err := w.apiClient.ListExternalIDs(ctx, w.workspaceID, w.clusterID, h.Name)
	if err != nil {
		reconcileRunsTotal.WithLabelValues(h.Name, RunResultError).Inc()
		kindLogger.Error(err, "reconcile.list_graph_failed")
		return
	}
	reconcileInGraphCount.WithLabelValues(h.Name).Set(float64(len(graphIDs)))

	// 3. Diff.
	missingInGraph, missingInCluster := Diff(clusterIDs, graphIDs)
	reconcileDriftTotal.WithLabelValues(h.Name, DriftDirectionMissingInGraph).Add(float64(len(missingInGraph)))
	reconcileDriftTotal.WithLabelValues(h.Name, DriftDirectionMissingInCluster).Add(float64(len(missingInCluster)))

	// 4. Emit corrective events. Dry-run skips Publish entirely — metrics
	// and logs still record the diff.
	if w.dryRun {
		// Log only counts in the missing-in-cluster direction (per ACL
		// invariant #5: do NOT log payload from the read API). Names are
		// safe in the missing-in-graph direction because they come from
		// our own watch coverage.
		kindLogger.Info(
			"reconcile.dry_run.diff",
			"in_cluster", len(clusterIDs),
			"in_graph", len(graphIDs),
			"missing_in_graph", len(missingInGraph),
			"missing_in_cluster", len(missingInCluster),
		)
		reconcileRunsTotal.WithLabelValues(h.Name, RunResultSuccess).Inc()
		return
	}

	failed := 0
	now := w.now()
	for _, ext := range missingInGraph {
		build := inCluster[ext]
		if build == nil {
			// Defensive: should never happen — the key was just produced
			// by ListInCluster — but log + count rather than panic.
			kindLogger.Error(errors.New("missing builder for in-cluster id"), "reconcile.emit.create",
				"external_id", ext,
			)
			failed++
			continue
		}
		ev := build(w.workspaceID, w.clusterID, w.clusterName, now)
		if perr := w.publishOne(ctx, ev); perr != nil {
			failed++
			kindLogger.Error(perr, "reconcile.emit.create.publish_failed", "external_id", ext)
		}
	}
	for _, ext := range missingInCluster {
		ev, err := h.EmitDeleted(ext, w.workspaceID, w.clusterID, w.clusterName, now)
		if err != nil {
			failed++
			// Do NOT log the external_id contents at INFO if the path is
			// malformed — only the parse error itself, with the kind.
			kindLogger.Error(err, "reconcile.emit.delete.parse_failed")
			continue
		}
		if perr := w.publishOne(ctx, ev); perr != nil {
			failed++
			// Deletion log line carries only the kind, not the in-graph
			// payload, per ACL invariant #5.
			kindLogger.Error(perr, "reconcile.emit.delete.publish_failed")
		}
	}

	if failed > 0 {
		reconcileRunsTotal.WithLabelValues(h.Name, RunResultError).Inc()
	} else {
		reconcileRunsTotal.WithLabelValues(h.Name, RunResultSuccess).Inc()
	}
	kindLogger.Info(
		"reconcile.cycle",
		"in_cluster", len(clusterIDs),
		"in_graph", len(graphIDs),
		"missing_in_graph", len(missingInGraph),
		"missing_in_cluster", len(missingInCluster),
		"failed_emits", failed,
	)
}

// publishOne wraps publisher.Publish with a defence-in-depth workspace
// guard. The publisher itself rejects zero-workspace events; this is a
// second check so any future code path that constructs an event without
// going through the entity helpers cannot leak.
func (w *Worker) publishOne(ctx context.Context, ev *entity.Event) error {
	if ev == nil {
		return errors.New("reconciler: nil event")
	}
	if ev.WorkspaceID != w.workspaceID {
		return fmt.Errorf("reconciler: refusing to publish event with workspace mismatch: ev=%s want=%s", ev.WorkspaceID, w.workspaceID)
	}
	return w.pub.Publish(ctx, ev)
}

// keysOf returns the keys of m in unspecified order. Diff sorts internally
// so we don't pre-sort here.
func keysOf(m map[string]EventBuilder) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
