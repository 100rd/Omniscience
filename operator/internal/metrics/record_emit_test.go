// Tests for the RecordEmit helper (#198).
//
// Each test asserts via the controller-runtime registry's Gather() output —
// the same surface Prometheus scrapes — so the assertions catch any
// regression in the metric registration path too. Touch-then-Gather is
// necessary because Prometheus' client_golang elides histograms with zero
// observations from Gather().
package metrics

import (
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
)

// eventLagSampleCount returns the cumulative SampleCount of the
// event_lag_seconds histogram for the given kind, or 0 if the series is not
// yet emitted. Test helper kept private to the metrics package so it can
// reuse the registry global.
func eventLagSampleCount(t *testing.T, kind string) uint64 {
	t.Helper()
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != "omniscience_operator_event_lag_seconds" {
			continue
		}
		for _, m := range f.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "kind" && lp.GetValue() == kind {
					return m.GetHistogram().GetSampleCount()
				}
			}
		}
	}
	return 0
}

// TestRecordEmit_HappyPath asserts a Pod with a non-zero CreationTimestamp
// produces exactly one observation on the event_lag_seconds histogram.
func TestRecordEmit_HappyPath(t *testing.T) {
	MustRegister()
	const kind = "RecordEmitHappyPath"
	before := eventLagSampleCount(t, kind)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			CreationTimestamp: metav1.NewTime(time.Now().Add(-2 * time.Second)),
		},
	}
	RecordEmit(kind, pod)
	got := eventLagSampleCount(t, kind)
	if got != before+1 {
		t.Errorf("expected SampleCount %d, got %d", before+1, got)
	}
}

// TestRecordEmit_NilObject asserts the helper short-circuits on a nil
// object and does NOT record an observation. Guards against a future caller
// that mistakenly wires the helper into an error path.
func TestRecordEmit_NilObject(t *testing.T) {
	MustRegister()
	const kind = "RecordEmitNilObject"
	before := eventLagSampleCount(t, kind)
	RecordEmit(kind, nil)
	got := eventLagSampleCount(t, kind)
	if got != before {
		t.Errorf("expected no observation for nil obj, SampleCount went from %d to %d", before, got)
	}
}

// TestRecordEmit_ZeroCreationTimestamp asserts a deletion-stub object (built
// from req.NamespacedName, no creationTimestamp) is silently skipped — that
// stub has no real freshness signal so observing it would corrupt the
// histogram with bogus large lags (now - zero-time = decades).
func TestRecordEmit_ZeroCreationTimestamp(t *testing.T) {
	MustRegister()
	const kind = "RecordEmitZeroCreationTimestamp"
	before := eventLagSampleCount(t, kind)
	stub := &corev1.Pod{}
	stub.Namespace = "ns"
	stub.Name = "deletion-stub"
	RecordEmit(kind, stub)
	got := eventLagSampleCount(t, kind)
	if got != before {
		t.Errorf("expected no observation for zero CreationTimestamp, SampleCount went from %d to %d", before, got)
	}
}

// TestRecordEmit_FutureCreationTimestamp asserts a future-dated
// CreationTimestamp (clock skew between API server and operator) does not
// panic, IS recorded (it's a real event, just with a misaligned clock), and
// is clamped into the smallest bucket via ObserveEventLag's defensive clamp.
func TestRecordEmit_FutureCreationTimestamp(t *testing.T) {
	MustRegister()
	const kind = "RecordEmitFutureCreationTimestamp"
	before := eventLagSampleCount(t, kind)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			CreationTimestamp: metav1.NewTime(time.Now().Add(5 * time.Second)),
		},
	}
	RecordEmit(kind, pod)
	got := eventLagSampleCount(t, kind)
	if got != before+1 {
		t.Errorf("expected SampleCount %d after future-ts observation, got %d", before+1, got)
	}
	// Verify the observation landed in the smallest bucket (≤0.1s) — the
	// clamp-to-zero in ObserveEventLag puts it at 0s exactly.
	families, _ := ctrlmetrics.Registry.Gather()
	for _, f := range families {
		if f.GetName() != "omniscience_operator_event_lag_seconds" {
			continue
		}
		for _, m := range f.GetMetric() {
			matched := false
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "kind" && lp.GetValue() == kind {
					matched = true
					break
				}
			}
			if !matched {
				continue
			}
			h := m.GetHistogram()
			// SampleSum should be 0 (or essentially 0) — clamp put the lag at 0.
			if h.GetSampleSum() > 0.001 {
				t.Errorf("expected clamped sample sum ~0, got %f", h.GetSampleSum())
			}
			return
		}
	}
}

// TestRecordEmit_KindLabelOnly asserts the metric series produced by
// RecordEmit carries ONLY the `kind` label — no namespace, name, or any
// tenancy-revealing key. This is the ADR-0007 §ACL invariant for the new
// call-site.
func TestRecordEmit_KindLabelOnly(t *testing.T) {
	MustRegister()
	const kind = "RecordEmitKindLabelOnly"
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:         "tenant-a-internal",
			Name:              "secret-pod",
			CreationTimestamp: metav1.NewTime(time.Now().Add(-time.Second)),
		},
	}
	RecordEmit(kind, pod)
	families, _ := ctrlmetrics.Registry.Gather()
	for _, f := range families {
		if f.GetName() != "omniscience_operator_event_lag_seconds" {
			continue
		}
		for _, m := range f.GetMetric() {
			matchedKind := ""
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "kind" {
					matchedKind = lp.GetValue()
				}
			}
			if matchedKind != kind {
				continue
			}
			// Now verify no other label name is present.
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "kind" {
					continue
				}
				t.Errorf("ACL VIOLATION: event_lag_seconds carries unexpected label %q=%q", lp.GetName(), lp.GetValue())
			}
			// Also make sure the pod's namespace/name did not leak into any
			// label value of any series — paranoid double-check.
			for _, lp := range m.GetLabel() {
				if strings.Contains(lp.GetValue(), "tenant-a-internal") || strings.Contains(lp.GetValue(), "secret-pod") {
					t.Errorf("ACL VIOLATION: pod identifier leaked into metric label: %s=%s", lp.GetName(), lp.GetValue())
				}
			}
			return
		}
	}
}
