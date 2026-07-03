// cache_size_ticker.go is the periodic informer-cache size reporter for
// issue #198.
//
// Background: omniscience_operator_informer_cache_objects is registered and
// dashboarded ("informer cache size" panel) but no controller calls
// SetInformerCacheObjects, so the gauge stays empty and the leak detector
// it powers is dead. #198 wires the call-site here so a single goroutine
// reports cache size for every active kind every 30s instead of
// instrumenting each watcher's cache event handler (which would mean
// touching 23 controllers and racing with controller-runtime's internal
// cache locking).
//
// Design choices:
//
//   - Uses sigs.k8s.io/controller-runtime/pkg/manager.Runnable so the
//     manager owns lifecycle: it calls Start(ctx) when the manager begins
//     and cancels ctx on shutdown. No bespoke goroutine plumbing in main.
//   - NeedLeaderElection() returns false: every replica's local cache
//     should be observed because the metric is a per-replica memory-cost
//     proxy. If we gated on leader election, only one replica would emit
//     and a leak in a non-leader replica would be invisible.
//   - The kind list is built up in main.go as each watcher registers,
//     including discovery-gated ones (ArgoCD, Rollouts, DRA) so absent
//     CRDs do not produce zero-cache series that would mislead the
//     leak-detector dashboard.
//   - First-tick jitter: ±10% of the interval. Multiple operators in a
//     cluster (multi-tenant control plane) would otherwise scrape on
//     the same 30s boundary and create a thundering-herd against the
//     informer cache lock; jitter spreads the load.
//   - List() is the cheap path against the manager's cache (no API
//     round-trip). meta.LenList counts items without an extra ExtractList
//     allocation.
package main

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"time"

	"github.com/go-logr/logr"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"

	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
)

// cacheKindEntry pairs a Kubernetes Kind label (as the dashboard joins on
// it) with a fresh empty list object for the manager's cache to populate.
//
// listFactory is a closure that returns a fresh client.ObjectList of the
// concrete type; passing the same instance per tick would let one tick's
// data leak into the next.
type cacheKindEntry struct {
	kind        string
	listFactory func() client.ObjectList
}

// cacheSizeTicker periodically reports informer cache size per registered
// kind. Implements sigs.k8s.io/controller-runtime/pkg/manager.Runnable.
type cacheSizeTicker struct {
	cache    cache.Cache
	kinds    []cacheKindEntry
	interval time.Duration
	jitter   float64 // fraction (0..1) of interval applied as +- random first-tick offset
	logger   logr.Logger
	// rng is exposed so tests can pin first-tick offset deterministically.
	rng *rand.Rand
}

// newCacheSizeTicker constructs the ticker with validated inputs.
func newCacheSizeTicker(c cache.Cache, kinds []cacheKindEntry, interval time.Duration, jitter float64, logger logr.Logger) (*cacheSizeTicker, error) {
	if c == nil {
		return nil, errors.New("cache_size_ticker: cache must not be nil")
	}
	if interval <= 0 {
		return nil, fmt.Errorf("cache_size_ticker: interval must be positive, got %s", interval)
	}
	if jitter < 0 || jitter > 1 {
		return nil, fmt.Errorf("cache_size_ticker: jitter must be in [0,1], got %f", jitter)
	}
	for i, k := range kinds {
		if k.kind == "" {
			return nil, fmt.Errorf("cache_size_ticker: kinds[%d].kind is empty", i)
		}
		if k.listFactory == nil {
			return nil, fmt.Errorf("cache_size_ticker: kinds[%d].listFactory is nil for %s", i, k.kind)
		}
	}
	return &cacheSizeTicker{
		cache:    c,
		kinds:    kinds,
		interval: interval,
		jitter:   jitter,
		logger:   logger.WithName("cache-size-ticker"),
		// #nosec G404 — non-cryptographic randomness for tick jitter is fine.
		rng: rand.New(rand.NewSource(time.Now().UnixNano())),
	}, nil
}

// NeedLeaderElection returns false so every replica reports its own local
// cache size. The metric is a per-replica memory-cost proxy; a leak in a
// non-leader replica would otherwise be invisible.
func (t *cacheSizeTicker) NeedLeaderElection() bool { return false }

// Start runs until ctx is cancelled. First tick fires after a jittered
// offset; subsequent ticks use a steady time.Ticker. Errors during a tick
// are logged at V(1) and do not stop the loop — a transient cache.List
// failure is recoverable and silencing the loop on first error would
// permanently disable the leak detector.
func (t *cacheSizeTicker) Start(ctx context.Context) error {
	first := t.firstTickDelay()
	t.logger.Info("starting cache size ticker",
		"event", "cache_size_ticker.start",
		"interval", t.interval.String(),
		"first_delay", first.String(),
		"kinds", len(t.kinds),
	)

	select {
	case <-ctx.Done():
		return nil
	case <-time.After(first):
	}
	t.tickOnce(ctx)

	ticker := time.NewTicker(t.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			t.tickOnce(ctx)
		}
	}
}

// firstTickDelay returns interval * (1 + uniform[-jitter, +jitter]).
// Returns interval verbatim when jitter is 0.
func (t *cacheSizeTicker) firstTickDelay() time.Duration {
	if t.jitter == 0 {
		return t.interval
	}
	offset := (t.rng.Float64()*2 - 1) * t.jitter
	delay := time.Duration(float64(t.interval) * (1 + offset))
	if delay < 0 {
		delay = t.interval
	}
	return delay
}

// tickOnce lists each registered kind from the manager's cache and updates
// the gauge. Errors are logged at V(1) — a transient list failure is
// expected during cache resync and should not spam the operator logs at
// info level.
func (t *cacheSizeTicker) tickOnce(ctx context.Context) {
	for _, k := range t.kinds {
		list := k.listFactory()
		if err := t.cache.List(ctx, list); err != nil {
			t.logger.V(1).Info("cache list failed",
				"event", "cache_size_ticker.list_error",
				"kind", k.kind,
				"error", err.Error(),
			)
			continue
		}
		count := apimeta.LenList(list)
		opmetrics.SetInformerCacheObjects(k.kind, count)
	}
}
