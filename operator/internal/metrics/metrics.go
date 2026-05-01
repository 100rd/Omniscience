// Package metrics is the central Prometheus metric surface for the Omniscience
// operator (issue #165).
//
// All metrics are registered exactly once with the controller-runtime metrics
// registry (sigs.k8s.io/controller-runtime/pkg/metrics) so they are exposed on
// the existing :8080/metrics endpoint — no second metrics server is started.
//
// ACL invariant (ADR-0007 §ACL, issue #165):
//
//   - The metrics endpoint is public to the cluster (anyone with network
//     access to :8080 can scrape it).
//   - Therefore NO label may carry tenancy-revealing data.
//   - workspace_id is NEVER a label on any metric. The
//     omniscience_operator_workspace_id_info gauge carries cluster_name and
//     operator_version only — both are customer-chosen display strings, not
//     tenancy boundaries.
//   - Resource names (Pod / Deployment names, etc.) are NEVER labels — they
//     would be both high-cardinality and tenant-revealing. Per-controller
//     emit metrics aggregate by kind+action+result only.
//   - namespace is NEVER a label. It is high-cardinality on a 10k-Pod
//     cluster and frequently encodes tenancy on customer clusters
//     (e.g. ns-${tenant}). If namespace-level visibility is needed in the
//     future, hash it: sha256(namespace)[:8].
//
// The unit tests in metrics_test.go assert these invariants by inspecting
// every metric's label set at the registration boundary.
package metrics

import (
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
)

// Subsystem and namespace prefix. The full metric name is therefore
// "omniscience_operator_<name>". Centralising it here makes a future rename
// a one-line change. Do not vary this per metric.
const (
	metricNamespace = "omniscience"
	metricSubsystem = "operator"
)

// Result label values. Closed enum — the controller call-sites must use one
// of these constants, never an arbitrary string. Adding a new value is a
// deliberate change that requires a dashboard panel update.
const (
	ResultSuccess      = "success"
	ResultPublishError = "publish_error"
	ResultSkipped      = "skipped"
)

// Action label values. Mirrors entity.ActionCreated/Updated/Deleted; the
// metric package re-declares them so it has no dependency on the entity
// package and so the closed enum is self-evident here.
const (
	ActionCreated = "created"
	ActionUpdated = "updated"
	ActionDeleted = "deleted"
)

// Publisher error_type label values. Closed enum aligned with the issue's
// alerting catalog (OmniscienceOperatorPublishErrorBurst groups by these
// values implicitly via the {error_type} label).
const (
	PublisherErrorConnect          = "connect"
	PublisherErrorPublish          = "publish"
	PublisherErrorAckTimeout       = "ack_timeout"
	PublisherErrorNATSNoResponders = "nats_no_responders"
)

// Per-controller emit metrics. Labels: kind, action, result.
//
// Cardinality budget: ~25 kinds * 3 actions * 3 results = 225 series. Bounded
// and stable. No high-cardinality fields.
var (
	EventsEmittedTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Namespace: metricNamespace,
			Subsystem: metricSubsystem,
			Name:      "events_emitted_total",
			Help: "Total events emitted to the publisher, by Kubernetes kind, " +
				"action (created|updated|deleted), and result " +
				"(success|publish_error|skipped). " +
				"Aggregated by kind only; namespace and resource names are " +
				"intentionally omitted to keep cardinality bounded and to " +
				"prevent tenancy leakage on customer clusters (ADR-0007 §ACL).",
		},
		[]string{"kind", "action", "result"},
	)

	// EventEmitDurationSeconds measures the publisher hop only — the time
	// spent between the controller calling publisher.Publish and the
	// JetStream ack returning. It is NOT the full reconcile duration; that
	// is what controller-runtime's controller_runtime_reconcile_time_seconds
	// reports.
	//
	// Buckets cover the realistic latency range: 10ms (local NATS, hot
	// path) to 30s (cross-region, ack_wait drift). The default Prometheus
	// buckets are not appropriate — they top out at 10s.
	EventEmitDurationSeconds = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Namespace: metricNamespace,
			Subsystem: metricSubsystem,
			Name:      "event_emit_duration_seconds",
			Help: "Latency of the publisher hop (controller call to ack), " +
				"by Kubernetes kind. Excludes informer / cache / reconcile " +
				"work. Buckets span 10ms..30s.",
			Buckets: []float64{0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30},
		},
		[]string{"kind"},
	)

	// PublisherInflight is a gauge of events the publisher has accepted but
	// not yet acked. NATS JetStream's Publish is synchronous in the current
	// implementation (publisher.go calls js.Publish), so this gauge is 0
	// or 1 in practice. Kept as a gauge so a future async batch publisher
	// surfaces backlog without an API change.
	PublisherInflight = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Namespace: metricNamespace,
			Subsystem: metricSubsystem,
			Name:      "publisher_inflight",
			Help: "Events the publisher has accepted but not yet acked, by " +
				"Kubernetes kind. With the synchronous JetStream publish " +
				"path this is 0 or 1 per kind; the gauge exists so a future " +
				"async batch publisher surfaces backlog without an API " +
				"change.",
		},
		[]string{"kind"},
	)

	// PublisherErrorsTotal counts publisher failures by error_type. The
	// closed enum is documented above. This metric is the alerting source
	// for OmniscienceOperatorPublishErrorBurst.
	PublisherErrorsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Namespace: metricNamespace,
			Subsystem: metricSubsystem,
			Name:      "publisher_errors_total",
			Help: "Publisher failures by error_type " +
				"(connect|publish|ack_timeout|nats_no_responders). " +
				"Used by OmniscienceOperatorPublishErrorBurst.",
		},
		[]string{"error_type"},
	)
)

// Per-controller event-lag metric (freshness SLO probe).
//
// Measures the time between a resource's metadata.creationTimestamp (or, for
// updates, its observed resourceVersion mtime as approximated by the time
// the controller dequeued the event) and emit.
//
// Buckets span 100ms..120s, covering the realistic freshness range. p99 above
// 30s for 15 minutes is the alerting threshold (OmniscienceOperatorEmitLagHigh).
var EventLagSeconds = prometheus.NewHistogramVec(
	prometheus.HistogramOpts{
		Namespace: metricNamespace,
		Subsystem: metricSubsystem,
		Name:      "event_lag_seconds",
		Help: "Time between the resource event timestamp (creationTimestamp " +
			"for adds, dequeue time for updates) and emit, by Kubernetes " +
			"kind. p99 above 30s for 15m is the freshness SLO breach alert " +
			"(OmniscienceOperatorEmitLagHigh).",
		Buckets: []float64{0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120},
	},
	[]string{"kind"},
)

// Resource utilisation: informer cache size per kind. Updated by the watch
// controllers' cache event handlers. Memory-cost proxy.
var InformerCacheObjects = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Namespace: metricNamespace,
		Subsystem: metricSubsystem,
		Name:      "informer_cache_objects",
		Help: "Approximate number of objects in the operator's informer " +
			"cache, by Kubernetes kind. A memory-cost proxy and a leak " +
			"detector — sustained growth without a corresponding cluster " +
			"object growth is a watch-loss bug.",
	},
	[]string{"kind"},
)

// WorkspaceIDInfo is the operator-identity gauge. It is always set to 1; the
// labels carry metadata (cluster_name, operator_version) for cross-operator
// joins in a multi-tenant Prometheus.
//
// CRITICAL: workspace_id (the UUID) is NOT a label. cluster_name is a
// customer-chosen display string. See ADR-0007 §ACL.
//
// When #167 lands the multi-cluster cluster_id, a third label is added; that
// is the only metric change required by #167.
var WorkspaceIDInfo = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Namespace: metricNamespace,
		Subsystem: metricSubsystem,
		Name:      "workspace_id_info",
		Help: "Operator identity gauge (always 1). Labels carry " +
			"cluster_name (customer-chosen display string) and " +
			"operator_version. workspace_id (the UUID) is NEVER a label " +
			"per ADR-0007 §ACL.",
	},
	[]string{"cluster_name", "operator_version"},
)

// Liveness signals — unix-timestamp gauges scraped by the alerts.
//
// LastPublishUnixSeconds is updated on every successful publish. The alert
// OmniscienceOperatorPublishStalled fires when (now - last_publish) > 300s.
//
// Cold-start safety: the gauge is initialised to 0 at startup. The alert
// rule includes a guard "and on() omniscience_operator_last_publish_unix_seconds > 0"
// so a freshly-started operator that has not yet emitted does NOT page.
var (
	LastPublishUnixSeconds = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Namespace: metricNamespace,
			Subsystem: metricSubsystem,
			Name:      "last_publish_unix_seconds",
			Help: "Unix timestamp of the most recent successful publish. " +
				"0 means the operator has not yet emitted (cold start). " +
				"OmniscienceOperatorPublishStalled fires when " +
				"(time() - this) > 300s, gated on this > 0 to avoid " +
				"cold-start false positives.",
		},
	)

	LastReconcileUnixSeconds = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Namespace: metricNamespace,
			Subsystem: metricSubsystem,
			Name:      "last_reconcile_unix_seconds",
			Help: "Unix timestamp of the most recent reconcile pass " +
				"(updated by the reconciler from #163). 0 means no " +
				"reconcile has run yet (cold start).",
		},
	)
)

// once gates registration so the package is safe to initialise multiple times
// (test code can re-import this package without panicking on duplicate
// registration; the prometheus AlreadyRegisteredError is the ground truth, but
// sync.Once is friendlier and faster).
var once sync.Once

// MustRegister registers all operator metrics with the controller-runtime
// metric registry. Idempotent — safe to call multiple times in tests.
//
// Called from cmd/manager/main.go before mgr.Start(). Must be called before
// any controller emits a metric, or the first emit will be a no-op (the
// counter is not visible at /metrics until registered).
func MustRegister() {
	once.Do(func() {
		ctrlmetrics.Registry.MustRegister(
			EventsEmittedTotal,
			EventEmitDurationSeconds,
			PublisherInflight,
			PublisherErrorsTotal,
			EventLagSeconds,
			InformerCacheObjects,
			WorkspaceIDInfo,
			LastPublishUnixSeconds,
			LastReconcileUnixSeconds,
		)
	})
}

// SetWorkspaceIDInfo sets the WorkspaceIDInfo gauge to 1 with the given
// metadata labels. cluster_name is a customer-chosen display string;
// operator_version is the build-time version.
//
// Called once at startup from cmd/manager/main.go. Calling it more than once
// (e.g. on chart upgrade with different values) overwrites the previous
// label-set series — Prometheus handles that gracefully via series staleness.
func SetWorkspaceIDInfo(clusterName, operatorVersion string) {
	WorkspaceIDInfo.WithLabelValues(clusterName, operatorVersion).Set(1)
}

// RecordPublishSuccess updates the success counter, latency histogram, and
// last-publish timestamp atomically. Call from the publisher.Publish success
// branch.
//
// kind is the Kubernetes kind (Pod, Deployment, etc.). action is one of the
// Action* constants. duration is the time the publish took (publisher hop
// only).
func RecordPublishSuccess(kind, action string, duration time.Duration) {
	EventsEmittedTotal.WithLabelValues(kind, action, ResultSuccess).Inc()
	EventEmitDurationSeconds.WithLabelValues(kind).Observe(duration.Seconds())
	LastPublishUnixSeconds.Set(float64(time.Now().Unix()))
}

// RecordPublishError updates the error counter for the controller surface
// and the publisher-error counter for the publisher surface.
//
// errorType is one of the PublisherError* constants. kind+action are the
// controller-side context (so dashboards can show "which kind is the
// errors-per-second flowing from").
func RecordPublishError(kind, action, errorType string) {
	EventsEmittedTotal.WithLabelValues(kind, action, ResultPublishError).Inc()
	PublisherErrorsTotal.WithLabelValues(errorType).Inc()
}

// RecordSkipped increments the skipped counter. "Skipped" means the
// controller decided not to emit (e.g. duplicate within a debounce window
// once that lands). Currently unused; reserved for future controllers.
func RecordSkipped(kind, action string) {
	EventsEmittedTotal.WithLabelValues(kind, action, ResultSkipped).Inc()
}

// ObserveEventLag records the freshness lag for an emitted event. Called by
// the controllers immediately before publish, with the time since the
// resource's creationTimestamp (for adds) or dequeue time (for updates).
//
// Negative values are clamped to 0 — a clock skew between the API server
// and the operator can produce a future-dated creationTimestamp; recording
// a negative bucket would corrupt the histogram.
func ObserveEventLag(kind string, lag time.Duration) {
	if lag < 0 {
		lag = 0
	}
	EventLagSeconds.WithLabelValues(kind).Observe(lag.Seconds())
}

// SetInformerCacheObjects updates the informer cache size gauge for a kind.
// Called from a periodic ticker in main.go (every 30s) so the value reflects
// the live cache without instrumenting every cache event.
func SetInformerCacheObjects(kind string, count int) {
	InformerCacheObjects.WithLabelValues(kind).Set(float64(count))
}

// SetLastReconcileUnixSeconds updates the reconcile-liveness gauge. Called
// by the reconciler from #163; safe to call from this package as a no-op
// fallback until #163 lands.
func SetLastReconcileUnixSeconds(t time.Time) {
	LastReconcileUnixSeconds.Set(float64(t.Unix()))
}
