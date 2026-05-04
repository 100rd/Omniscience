// Tests for the cache size ticker (#198).
//
// We do NOT use envtest here. envtest would spin up a real apiserver and
// etcd to back the controller-runtime cache; the ticker has a single
// dependency (cache.List) and we already own that interface in the package
// under test. A hand-rolled fake cache lets the test run in well under a
// second without docker / kubelet prerequisites.
//
// The fake cache implements only the methods the ticker actually calls
// (List). The other Cache methods panic — if the ticker grows a new
// dependency, the panic will surface in CI immediately and force this file
// to be revisited.
package main

import (
	"context"
	"testing"
	"time"

	"github.com/go-logr/logr/testr"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"

	opmetrics "github.com/100rd/omniscience/operator/internal/metrics"
)

// fakeCache is a minimal cache.Cache implementation that returns a
// pre-populated list for every List call. Only List is implemented — every
// other method panics so a future ticker dependency surfaces loudly.
type fakeCache struct {
	cache.Cache // embed to satisfy the interface; nil dispatch panics on unused methods
	listResult  client.ObjectList
	listCalls   int
	listErr     error
}

func (f *fakeCache) List(_ context.Context, list client.ObjectList, _ ...client.ListOption) error {
	f.listCalls++
	if f.listErr != nil {
		return f.listErr
	}
	if f.listResult == nil {
		return nil
	}
	// Copy the pre-populated items into the destination list. We use
	// meta.ExtractList + Set to avoid taking a hard dependency on the
	// concrete list type (the ticker uses a generic listFactory closure).
	items, err := meta.ExtractList(f.listResult)
	if err != nil {
		return err
	}
	if err := meta.SetList(list, items); err != nil {
		return err
	}
	return nil
}

// gaugeValue returns the current value of informer_cache_objects for the
// given kind, or -1 if the series is not present.
func gaugeValue(t *testing.T, kind string) float64 {
	t.Helper()
	families, err := ctrlmetrics.Registry.Gather()
	if err != nil {
		t.Fatalf("Registry.Gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != "omniscience_operator_informer_cache_objects" {
			continue
		}
		for _, m := range f.GetMetric() {
			matched := false
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "kind" && lp.GetValue() == kind {
					matched = true
				}
			}
			if matched {
				return m.GetGauge().GetValue()
			}
		}
	}
	return -1
}

// TestCacheSizeTicker_TickUpdatesGauge asserts a single tick lists the
// cache and updates the gauge to the list length.
func TestCacheSizeTicker_TickUpdatesGauge(t *testing.T) {
	opmetrics.MustRegister()
	const kind = "TickerCMHappy"
	cms := &corev1.ConfigMapList{
		Items: []corev1.ConfigMap{
			{ObjectMeta: metav1.ObjectMeta{Name: "a"}},
			{ObjectMeta: metav1.ObjectMeta{Name: "b"}},
			{ObjectMeta: metav1.ObjectMeta{Name: "c"}},
		},
	}
	fc := &fakeCache{listResult: cms}
	tk, err := newCacheSizeTicker(fc, []cacheKindEntry{
		{kind: kind, listFactory: func() client.ObjectList { return &corev1.ConfigMapList{} }},
	}, 10*time.Millisecond, 0, testr.New(t))
	if err != nil {
		t.Fatalf("newCacheSizeTicker: %v", err)
	}
	// Drive a single tick directly so the test is deterministic.
	tk.tickOnce(context.Background())
	if got := gaugeValue(t, kind); got != 3 {
		t.Errorf("gauge for %s = %v, want 3", kind, got)
	}
	if fc.listCalls != 1 {
		t.Errorf("expected 1 List call, got %d", fc.listCalls)
	}
}

// TestCacheSizeTicker_StartCancellation asserts Start exits cleanly on
// ctx.Done() with no panic and no error.
func TestCacheSizeTicker_StartCancellation(t *testing.T) {
	opmetrics.MustRegister()
	const kind = "TickerCMCancel"
	fc := &fakeCache{listResult: &corev1.ConfigMapList{}}
	tk, err := newCacheSizeTicker(fc, []cacheKindEntry{
		{kind: kind, listFactory: func() client.ObjectList { return &corev1.ConfigMapList{} }},
	}, 50*time.Millisecond, 0, testr.New(t))
	if err != nil {
		t.Fatalf("newCacheSizeTicker: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- tk.Start(ctx) }()
	// Cancel before the first tick fires.
	time.Sleep(10 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Start returned %v on cancel, want nil", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Start did not return within 1s of cancel")
	}
}

// TestCacheSizeTicker_ListErrorContinues asserts a single failed List does
// NOT stop the ticker — the leak detector must keep working through
// transient cache errors.
func TestCacheSizeTicker_ListErrorContinues(t *testing.T) {
	opmetrics.MustRegister()
	const kind = "TickerCMErr"
	fc := &fakeCache{listErr: errSentinel{}}
	tk, err := newCacheSizeTicker(fc, []cacheKindEntry{
		{kind: kind, listFactory: func() client.ObjectList { return &corev1.ConfigMapList{} }},
	}, 50*time.Millisecond, 0, testr.New(t))
	if err != nil {
		t.Fatalf("newCacheSizeTicker: %v", err)
	}
	// Two ticks under error — should not panic, should still call List
	// twice (loop survives the error).
	tk.tickOnce(context.Background())
	tk.tickOnce(context.Background())
	if fc.listCalls != 2 {
		t.Errorf("expected 2 List calls under error, got %d", fc.listCalls)
	}
	// And the gauge should NOT have been updated for this kind.
	if got := gaugeValue(t, kind); got != -1 {
		t.Errorf("gauge for %s = %v, want -1 (no series), error path must not write", kind, got)
	}
}

// TestNewCacheSizeTicker_Validation asserts the constructor rejects bad
// inputs explicitly so a misconfiguration in main.go fails at startup
// rather than silently disabling the leak detector.
func TestNewCacheSizeTicker_Validation(t *testing.T) {
	logger := testr.New(t)
	cases := []struct {
		name     string
		c        cache.Cache
		kinds    []cacheKindEntry
		interval time.Duration
		jitter   float64
		wantErr  bool
	}{
		{name: "nil cache", c: nil, interval: time.Second, kinds: nil, wantErr: true},
		{name: "zero interval", c: &fakeCache{}, interval: 0, kinds: nil, wantErr: true},
		{name: "negative jitter", c: &fakeCache{}, interval: time.Second, jitter: -0.1, kinds: nil, wantErr: true},
		{name: "jitter > 1", c: &fakeCache{}, interval: time.Second, jitter: 1.5, kinds: nil, wantErr: true},
		{name: "empty kind", c: &fakeCache{}, interval: time.Second, kinds: []cacheKindEntry{{kind: "", listFactory: func() client.ObjectList { return &corev1.ConfigMapList{} }}}, wantErr: true},
		{name: "nil factory", c: &fakeCache{}, interval: time.Second, kinds: []cacheKindEntry{{kind: "K", listFactory: nil}}, wantErr: true},
		{name: "valid", c: &fakeCache{}, interval: time.Second, kinds: []cacheKindEntry{{kind: "K", listFactory: func() client.ObjectList { return &corev1.ConfigMapList{} }}}, wantErr: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := newCacheSizeTicker(tc.c, tc.kinds, tc.interval, tc.jitter, logger)
			if (err != nil) != tc.wantErr {
				t.Errorf("got err=%v, wantErr=%v", err, tc.wantErr)
			}
		})
	}
}

// TestCacheSizeTicker_NoLeaderElection asserts every replica observes its
// own cache. A leak in a non-leader replica would otherwise be invisible.
func TestCacheSizeTicker_NoLeaderElection(t *testing.T) {
	tk := &cacheSizeTicker{}
	if tk.NeedLeaderElection() {
		t.Fatal("ticker must not require leader election — per-replica memory metric")
	}
}

// errSentinel is a typed sentinel error so the test asserts the exact error
// path without string-matching.
type errSentinel struct{}

func (errSentinel) Error() string { return "test sentinel" }

// Compile-time assertions.
var _ runtime.Object = (*corev1.ConfigMapList)(nil)
var _ types.NamespacedName // silence unused import; kept for future test additions
