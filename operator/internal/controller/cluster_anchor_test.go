package controller_test

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/100rd/omniscience/operator/internal/controller"
	"github.com/100rd/omniscience/operator/internal/entity"
)

// anchorFakePublisher is a minimal in-memory publisher that records every
// published event. Safe for concurrent use; the cluster anchor publisher
// runs serially today but the test asserts the contract that could change.
type anchorFakePublisher struct {
	mu     sync.Mutex
	events []*entity.Event
}

func (f *anchorFakePublisher) Publish(_ context.Context, ev *entity.Event) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, ev)
	return nil
}

func (f *anchorFakePublisher) Close() error { return nil }

func (f *anchorFakePublisher) snapshot() []*entity.Event {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]*entity.Event, len(f.events))
	copy(out, f.events)
	return out
}

var (
	fixedWorkspace = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	fixedClusterID = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	fixedNow       = time.Date(2026, 4, 24, 12, 0, 0, 0, time.UTC)
)

// TestClusterAnchorPublisher_PublishOnceIsIdempotent — the issue acceptance
// test: "Restart of the operator pod re-publishes the same Cluster entity
// (idempotent — server side resolves by external_id)". Two PublishOnce
// calls on two distinct publisher instances (simulating restarts) must
// produce events with the same external_id, cluster_id, and source_id.
func TestClusterAnchorPublisher_PublishOnceIsIdempotent(t *testing.T) {
	pub1 := &anchorFakePublisher{}
	p1, err := controller.NewClusterAnchorPublisher(pub1, fixedWorkspace, fixedClusterID, "prod-eu", "v1.31.1", "https://api:6443")
	if err != nil {
		t.Fatalf("new pub1: %v", err)
	}
	p1.Now = func() time.Time { return fixedNow }

	if err := p1.PublishOnce(context.Background()); err != nil {
		t.Fatalf("publish1: %v", err)
	}

	// Second "restart": fresh publisher instance, same config.
	pub2 := &anchorFakePublisher{}
	p2, err := controller.NewClusterAnchorPublisher(pub2, fixedWorkspace, fixedClusterID, "prod-eu", "v1.31.1", "https://api:6443")
	if err != nil {
		t.Fatalf("new pub2: %v", err)
	}
	p2.Now = func() time.Time { return fixedNow.Add(time.Hour) }

	if err := p2.PublishOnce(context.Background()); err != nil {
		t.Fatalf("publish2: %v", err)
	}

	got1 := pub1.snapshot()
	got2 := pub2.snapshot()
	if len(got1) != 1 || len(got2) != 1 {
		t.Fatalf("expected 1 event each, got %d / %d", len(got1), len(got2))
	}

	a, b := got1[0], got2[0]
	if a.ExternalID != b.ExternalID {
		t.Fatalf("external_id drift: %q vs %q", a.ExternalID, b.ExternalID)
	}
	if a.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Cluster/prod-eu" {
		t.Fatalf("external_id = %q", a.ExternalID)
	}
	if a.SourceID != b.SourceID {
		t.Fatalf("source_id drift: %s vs %s", a.SourceID, b.SourceID)
	}
	if a.Metadata["cluster_id"] != b.Metadata["cluster_id"] {
		t.Fatalf("cluster_id drift: %q vs %q", a.Metadata["cluster_id"], b.Metadata["cluster_id"])
	}
	// Sanity: emitted_at differs (different clocks).
	if a.EmittedAt.Equal(b.EmittedAt) {
		t.Fatalf("expected emitted_at to differ across restarts")
	}
}

// TestClusterAnchorPublisher_RejectsZeroWorkspace — defensive: a zero
// workspace UUID would publish an event the publisher then rejects. Fail
// closed at construction time so the operator surfaces the misconfiguration.
func TestClusterAnchorPublisher_RejectsZeroWorkspace(t *testing.T) {
	_, err := controller.NewClusterAnchorPublisher(&anchorFakePublisher{}, uuid.Nil, fixedClusterID, "prod-eu", "", "")
	if err == nil {
		t.Fatalf("expected error for zero workspace UUID")
	}
}

// TestClusterAnchorPublisher_RejectsEmptyClusterName — ensures we never
// publish an anchor with external_id "k8s_resource/99999999-8888-7777-6666-555555555555/Cluster/" (trailing
// slash) which would collide trivially across workspaces.
func TestClusterAnchorPublisher_RejectsEmptyClusterName(t *testing.T) {
	_, err := controller.NewClusterAnchorPublisher(&anchorFakePublisher{}, fixedWorkspace, fixedClusterID, "", "", "")
	if err == nil {
		t.Fatalf("expected error for empty clusterName")
	}
}

// TestClusterAnchorPublisher_RejectsNilPublisher — defence-in-depth.
func TestClusterAnchorPublisher_RejectsNilPublisher(t *testing.T) {
	_, err := controller.NewClusterAnchorPublisher(nil, fixedWorkspace, fixedClusterID, "prod-eu", "", "")
	if err == nil {
		t.Fatalf("expected error for nil publisher")
	}
}
