// Package controller — ClusterAnchor publisher.
//
// The Cluster anchor entity is the per-cluster top-level entity that every
// other operator-emitted entity references via an IN_CLUSTER edge. It is
// emitted once at operator startup (not driven by a watch — there is no
// "Cluster" Kubernetes resource to watch). Restart of the operator pod
// re-emits the same entity (same external_id, same cluster_id) so the
// server-side upsert is idempotent.
//
// The publisher is split out from main.go so the unit test in
// cluster_anchor_test.go can exercise it without spinning up a controller-
// runtime manager. Two calls in a row produce byte-equal Events modulo the
// emitted_at timestamp — the idempotence test pins the clock and asserts.
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"

	"github.com/100rd/omniscience/operator/internal/entity"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// ClusterAnchorPublisher emits the per-cluster anchor entity at startup.
// It is not a controller-runtime reconciler — it has no watch — so it is
// invoked directly from main once the manager is wired but before
// mgr.Start blocks.
type ClusterAnchorPublisher struct {
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string

	// KubernetesVersion and ClusterEndpoint are optional cluster facts
	// stamped onto the anchor's metadata. Empty values are dropped from
	// the event by ClusterToEvent so missing keys are clearer than
	// empty-string keys.
	KubernetesVersion string
	ClusterEndpoint   string

	// Now is the clock used for emitted_at. Injectable for tests.
	Now func() time.Time
}

// NewClusterAnchorPublisher returns a publisher with the same validation
// the watcher reconcilers do — workspace must be a non-zero UUID, cluster
// name must be set, publisher must be non-nil. Returning an error rather
// than panicking lets main.go exit non-zero with a useful message.
func NewClusterAnchorPublisher(pub publisher.Publisher, workspaceID uuid.UUID, clusterName, kubernetesVersion, clusterEndpoint string) (*ClusterAnchorPublisher, error) {
	if pub == nil {
		return nil, errors.New("cluster anchor: publisher must not be nil")
	}
	if workspaceID.String() == "00000000-0000-0000-0000-000000000000" {
		return nil, errors.New("cluster anchor: workspaceID must not be the zero UUID")
	}
	if clusterName == "" {
		return nil, errors.New("cluster anchor: clusterName is required")
	}
	return &ClusterAnchorPublisher{
		Publisher:         pub,
		WorkspaceID:       workspaceID,
		ClusterName:       clusterName,
		KubernetesVersion: kubernetesVersion,
		ClusterEndpoint:   clusterEndpoint,
		Now:               time.Now,
	}, nil
}

// PublishOnce emits the Cluster anchor entity exactly once. The server side
// is idempotent on (workspace_id, external_id), so a re-run on operator
// restart produces no duplicate row — the deterministic external_id and
// cluster_id are the contract that makes the publish safe to repeat.
func (p *ClusterAnchorPublisher) PublishOnce(ctx context.Context) error {
	ev := entity.ClusterToEvent(
		entity.ActionCreated,
		p.WorkspaceID,
		p.ClusterName,
		p.KubernetesVersion,
		p.ClusterEndpoint,
		p.Now(),
	)
	if err := p.Publisher.Publish(ctx, ev); err != nil {
		return fmt.Errorf("publish cluster anchor: %w", err)
	}
	return nil
}

// BuildEvent returns the Event that PublishOnce would emit, without
// publishing it. Exposed so tests (and future call-sites that want to
// inspect the anchor before sending it) don't need a publisher to assert
// on the event shape. Two calls with the same inputs produce byte-equal
// outputs modulo the injected clock — that's the idempotence contract.
func (p *ClusterAnchorPublisher) BuildEvent() *entity.Event {
	return entity.ClusterToEvent(
		entity.ActionCreated,
		p.WorkspaceID,
		p.ClusterName,
		p.KubernetesVersion,
		p.ClusterEndpoint,
		p.Now(),
	)
}
