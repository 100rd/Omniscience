// Prometheus metrics for the reconciliation worker.
//
// The five metrics below match issue #163 §Metrics exactly. Every metric
// is registered with controller-runtime's metrics registry so they appear
// on the existing /metrics endpoint (OMNISCIENCE_METRICS_ADDR).
//
// ACL invariant: labels include `kind` and (for drift) `direction`, but
// NEVER `workspace_id`. Tagging with workspace_id would expose tenancy in
// the metrics endpoint — multi-tenant operators in the same Prometheus
// scrape target would leak each other's existence. See issue #163 §ACL
// invariant #4. Labels are append-only with respect to the issue spec; we
// add no others.
package reconciler

import (
	"github.com/prometheus/client_golang/prometheus"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
)

// Drift directions used by the drift counter's `direction` label. Closed
// vocabulary — defined as constants so dashboards can match without a
// free-form-string surface.
const (
	DriftDirectionMissingInGraph   = "missing_in_graph"
	DriftDirectionMissingInCluster = "missing_in_cluster"
)

// Result values used by the runs counter's `result` label.
const (
	RunResultSuccess = "success"
	RunResultError   = "error"
)

var (
	// reconcileRunsTotal counts reconcile cycles per kind, partitioned by
	// outcome. Operators page on this metric — a sustained `error` rate
	// is the primary "reconciler stuck" signal.
	reconcileRunsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "omniscience_operator_reconcile_runs_total",
			Help: "Total number of reconcile runs by kind and outcome.",
		},
		[]string{"kind", "result"},
	)

	// reconcileDurationSeconds tracks per-kind reconcile cycle latency.
	// Buckets cover the expected range from <1s (small clusters) to
	// minutes (10k-pod clusters with paginated reads).
	reconcileDurationSeconds = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "omniscience_operator_reconcile_duration_seconds",
			Help:    "Wall-clock duration of a reconcile cycle, by kind.",
			Buckets: []float64{0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300},
		},
		[]string{"kind"},
	)

	// reconcileDriftTotal counts drift events found per kind and direction.
	// Dashboards graph the rolling sum to catch deletes-only or
	// creates-only patterns indicative of a stuck consumer.
	reconcileDriftTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "omniscience_operator_reconcile_drift_total",
			Help: "Total drift events found by kind and direction.",
		},
		[]string{"kind", "direction"},
	)

	// reconcileInGraphCount is a gauge of the most recent in-graph entity
	// count per kind. Useful for both validation (matches expected
	// cluster size?) and capacity planning.
	reconcileInGraphCount = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "omniscience_operator_reconcile_in_graph_count",
			Help: "Most recent in-graph entity count by kind.",
		},
		[]string{"kind"},
	)

	// reconcileInClusterCount is the cluster-side counterpart.
	reconcileInClusterCount = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "omniscience_operator_reconcile_in_cluster_count",
			Help: "Most recent in-cluster entity count by kind.",
		},
		[]string{"kind"},
	)
)

// init registers every metric with controller-runtime's registry once at
// package import. controller-runtime exposes them at the same /metrics
// endpoint as every other operator metric, so adding a scrape config here
// is unnecessary — the existing one already covers it.
func init() {
	ctrlmetrics.Registry.MustRegister(
		reconcileRunsTotal,
		reconcileDurationSeconds,
		reconcileDriftTotal,
		reconcileInGraphCount,
		reconcileInClusterCount,
	)
}
