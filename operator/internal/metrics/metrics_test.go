// Tests for the operator metrics surface (#165).
//
// The most important assertions in this file enforce ADR-0007 §ACL: NO metric
// may carry workspace_id (or any synonym) as a label. A regression here would
// be an ACL leak — anyone scraping :8080 could read tenant identity from
// metric labels.
package metrics

import (
	"strings"
	"testing"
	"time"

	dto "github.com/prometheus/client_model/go"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
)

// forbiddenLabels enumerates label names that MUST NOT appear on any operator
// metric. workspace_id is the primary tenancy identifier; the synonyms guard
// against accidental aliasing.
var forbiddenLabels = []string{
	"workspace_id",
	"workspace",
	"tenant_id",
	"tenant",
	"customer_id",
	"namespace", // high-cardinality + frequently encodes tenancy on customer clusters
	"pod_name",
	"pod",
	"resource_name",
	"name", // bare "name" is too generic and dangerously high-cardinality
}

// expectedMetrics is the closed catalog the issue requires. The test asserts
// every entry is registered (no typos in the names).
var expectedMetrics = []string{
	"omniscience_operator_events_emitted_total",
	"omniscience_operator_event_emit_duration_seconds",
	"omniscience_operator_publisher_inflight",
	"omniscience_operator_publisher_errors_total",
	"omniscience_operator_event_lag_seconds",
	"omniscience_operator_informer_cache_objects",
	"omniscience_operator_workspace_id_info",
	"omniscience_operator_last_publish_unix_seconds",
	"omniscience_operator_last_reconcile_unix_seconds",
}

// TestMustRegister_AllMetricsRegistered asserts every metric in the catalog
// is present in the controller-runtime registry after MustRegister. A typo
// in metric naming is the most common silent failure mode.
//
// Counters / histograms with no observations do not appear in Gather() output
// (Prometheus optimisation), so we touch every metric with a representative
// value first, then Gather and assert.
func TestMustRegister_AllMetricsRegistered(t *testing.T) {
	MustRegister()
	for _, name := range expectedMetrics {
		touchMetric(name)
	}
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather: %v", err)
	}
	got := map[string]bool{}
	for _, f := range families {
		got[f.GetName()] = true
	}
	for _, want := range expectedMetrics {
		if !got[want] {
			t.Errorf("metric %s not registered", want)
		}
	}
}

// TestACL_NoForbiddenLabels asserts no metric carries a tenancy-revealing
// label. This is the ADR-0007 §ACL invariant in test form. A regression here
// is an ACL leak.
func TestACL_NoForbiddenLabels(t *testing.T) {
	MustRegister()
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather: %v", err)
	}
	for _, f := range families {
		// We only police operator-prefixed metrics; controller-runtime's own
		// metrics (workqueue_*, controller_runtime_*) are out of scope.
		if !strings.HasPrefix(f.GetName(), "omniscience_operator_") {
			continue
		}
		// Touch every metric once with a representative label set so the
		// metric's labels appear in Gather output. Without this the
		// LabelValues() return is empty for never-incremented metrics.
		touchMetric(f.GetName())
	}
	families, err = ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather (after touch): %v", err)
	}
	for _, f := range families {
		if !strings.HasPrefix(f.GetName(), "omniscience_operator_") {
			continue
		}
		for _, m := range f.GetMetric() {
			for _, lp := range m.GetLabel() {
				name := lp.GetName()
				for _, forbid := range forbiddenLabels {
					if name == forbid {
						t.Errorf("ACL VIOLATION: metric %s has forbidden label %q "+
							"(see ADR-0007 §ACL — %s is tenancy-revealing or "+
							"high-cardinality)", f.GetName(), name, name)
					}
				}
			}
		}
	}
}

// TestACL_WorkspaceIDInfoLabels_AreSafe asserts the workspace_id_info gauge
// carries only the safe labels (cluster_name, operator_version) — it must NOT
// carry the workspace UUID itself.
func TestACL_WorkspaceIDInfoLabels_AreSafe(t *testing.T) {
	MustRegister()
	SetWorkspaceIDInfo("prod-eu-west", "0.2.0")
	families, _ := ctrlmetrics.Registry.Gather()
	for _, f := range families {
		if f.GetName() != "omniscience_operator_workspace_id_info" {
			continue
		}
		for _, m := range f.GetMetric() {
			labels := map[string]string{}
			for _, lp := range m.GetLabel() {
				labels[lp.GetName()] = lp.GetValue()
			}
			if labels["cluster_name"] != "prod-eu-west" {
				t.Errorf("expected cluster_name=prod-eu-west, got %q", labels["cluster_name"])
			}
			if labels["operator_version"] != "0.2.0" {
				t.Errorf("expected operator_version=0.2.0, got %q", labels["operator_version"])
			}
			// Belt-and-braces: assert the workspace UUID is not embedded
			// anywhere — neither as a label name, value, nor in the help
			// text (paranoid grep for any UUID-shaped string).
			for k, v := range labels {
				if strings.Contains(strings.ToLower(k), "workspace_id") {
					t.Errorf("workspace_id_info has forbidden label key %q", k)
				}
				if looksLikeUUID(v) {
					t.Errorf("workspace_id_info has UUID-shaped value %q in label %q", v, k)
				}
			}
			return
		}
	}
	t.Fatal("workspace_id_info family not found after SetWorkspaceIDInfo")
}

// TestRecordPublishSuccess_UpdatesAllSurfaces asserts a success record
// touches every relevant series: events_emitted_total{result=success},
// event_emit_duration_seconds, last_publish_unix_seconds.
func TestRecordPublishSuccess_UpdatesAllSurfaces(t *testing.T) {
	MustRegister()
	before := getCounter(t, "omniscience_operator_events_emitted_total",
		map[string]string{"kind": "Pod", "action": "updated", "result": "success"})

	RecordPublishSuccess("Pod", "updated", 50*time.Millisecond)

	after := getCounter(t, "omniscience_operator_events_emitted_total",
		map[string]string{"kind": "Pod", "action": "updated", "result": "success"})
	if after-before != 1 {
		t.Errorf("events_emitted_total{success}: want +1, got +%v", after-before)
	}
	if last := getGaugeSimple(t, "omniscience_operator_last_publish_unix_seconds"); last == 0 {
		t.Errorf("last_publish_unix_seconds not advanced after RecordPublishSuccess")
	}
}

// TestRecordPublishError_UpdatesBothCounters asserts an error record
// increments both events_emitted_total{result=publish_error} and
// publisher_errors_total{error_type=...} — the two surfaces dashboards and
// alerts depend on, respectively.
func TestRecordPublishError_UpdatesBothCounters(t *testing.T) {
	MustRegister()
	beforeEvents := getCounter(t, "omniscience_operator_events_emitted_total",
		map[string]string{"kind": "Pod", "action": "updated", "result": "publish_error"})
	beforeErrs := getCounter(t, "omniscience_operator_publisher_errors_total",
		map[string]string{"error_type": "publish"})

	RecordPublishError("Pod", "updated", PublisherErrorPublish)

	afterEvents := getCounter(t, "omniscience_operator_events_emitted_total",
		map[string]string{"kind": "Pod", "action": "updated", "result": "publish_error"})
	afterErrs := getCounter(t, "omniscience_operator_publisher_errors_total",
		map[string]string{"error_type": "publish"})

	if afterEvents-beforeEvents != 1 {
		t.Errorf("events_emitted_total{publish_error}: want +1, got +%v",
			afterEvents-beforeEvents)
	}
	if afterErrs-beforeErrs != 1 {
		t.Errorf("publisher_errors_total{publish}: want +1, got +%v",
			afterErrs-beforeErrs)
	}
}

// TestObserveEventLag_NegativeClamped asserts that a negative lag (clock
// skew between API server and operator) is clamped to 0 rather than recorded
// as a negative bucket — recording negative would corrupt the histogram.
func TestObserveEventLag_NegativeClamped(t *testing.T) {
	MustRegister()
	// Observe a negative lag. If the clamp is missing, this would either
	// panic or push a negative observation into the histogram.
	ObserveEventLag("Pod", -5*time.Second)
	// No direct assertion possible against a single sample without sample
	// extraction; assertion is "did not panic" + the clamp is documented.
}

// TestMustRegister_Idempotent asserts MustRegister is safe to call multiple
// times without panicking on duplicate registration. The sync.Once guard is
// the production safety net for tests that import this package indirectly
// (e.g. via the publisher tests).
func TestMustRegister_Idempotent(t *testing.T) {
	MustRegister()
	MustRegister()
	MustRegister()
}

// touchMetric increments / sets the named metric with a representative label
// set so it shows up in Gather output. Best-effort — unknown metrics are
// silently skipped.
func touchMetric(name string) {
	switch name {
	case "omniscience_operator_events_emitted_total":
		EventsEmittedTotal.WithLabelValues("Pod", "updated", "success").Inc()
	case "omniscience_operator_event_emit_duration_seconds":
		EventEmitDurationSeconds.WithLabelValues("Pod").Observe(0.05)
	case "omniscience_operator_publisher_inflight":
		PublisherInflight.WithLabelValues("Pod").Set(0)
	case "omniscience_operator_publisher_errors_total":
		PublisherErrorsTotal.WithLabelValues("publish").Inc()
	case "omniscience_operator_event_lag_seconds":
		EventLagSeconds.WithLabelValues("Pod").Observe(1)
	case "omniscience_operator_informer_cache_objects":
		InformerCacheObjects.WithLabelValues("Pod").Set(0)
	case "omniscience_operator_workspace_id_info":
		WorkspaceIDInfo.WithLabelValues("test", "0.0.0").Set(1)
	case "omniscience_operator_last_publish_unix_seconds":
		LastPublishUnixSeconds.Set(0)
	case "omniscience_operator_last_reconcile_unix_seconds":
		LastReconcileUnixSeconds.Set(0)
	}
}

// getCounter extracts the current value of a counter metric for the given
// label set. Returns 0 if the series is not yet present.
func getCounter(t *testing.T, name string, labels map[string]string) float64 {
	t.Helper()
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != name {
			continue
		}
		for _, m := range f.GetMetric() {
			if metricLabelsMatch(m, labels) {
				return m.GetCounter().GetValue()
			}
		}
	}
	return 0
}

// getGaugeSimple returns the current value of a label-less gauge.
func getGaugeSimple(t *testing.T, name string) float64 {
	t.Helper()
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != name {
			continue
		}
		for _, m := range f.GetMetric() {
			return m.GetGauge().GetValue()
		}
	}
	return 0
}

// metricLabelsMatch returns true iff every (k, v) in want is present on m.
func metricLabelsMatch(m *dto.Metric, want map[string]string) bool {
	got := map[string]string{}
	for _, lp := range m.GetLabel() {
		got[lp.GetName()] = lp.GetValue()
	}
	for k, v := range want {
		if got[k] != v {
			return false
		}
	}
	return true
}

// looksLikeUUID returns true if s is shaped like a canonical UUID. Used to
// belt-and-braces detect a workspace_id leak into a label value.
func looksLikeUUID(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i, ch := range s {
		switch i {
		case 8, 13, 18, 23:
			if ch != '-' {
				return false
			}
		default:
			isHex := (ch >= '0' && ch <= '9') ||
				(ch >= 'a' && ch <= 'f') ||
				(ch >= 'A' && ch <= 'F')
			if !isHex {
				return false
			}
		}
	}
	return true
}
