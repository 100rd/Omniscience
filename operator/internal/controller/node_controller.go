// Package controller — NodeReconciler.
//
// Watches corev1.Node (cluster-scoped) and emits one Event per reconcile.
// The structure mirrors PodReconciler exactly so a reader who already
// understands Pod can read this in one pass; the only differences are the
// kind being watched and the mapper invoked.
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/Omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
	"github.com/100rd/Omniscience/operator/internal/publisher"
)

// NodeReconciler watches Nodes and publishes change events.
type NodeReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewNodeReconciler returns a reconciler with sane defaults. Symmetric with
// NewPodReconciler — same validation, same defaults.
func NewNodeReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*NodeReconciler, error) {
	if c == nil {
		return nil, errors.New("controller: client must not be nil")
	}
	if pub == nil {
		return nil, errors.New("controller: publisher must not be nil")
	}
	if workspaceID.String() == "00000000-0000-0000-0000-000000000000" {
		return nil, errors.New("controller: workspaceID must not be the zero UUID")
	}
	if clusterName == "" {
		return nil, errors.New("controller: clusterName is required")
	}
	return &NodeReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile follows the Pod reconciler's exact semantics: GET success →
// upsert event; NotFound → deletion event with a stub bearing only the
// identifying metadata; other error → requeue.
func (r *NodeReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"node", req.Name,
		"workspace_id", r.WorkspaceID.String(),
	)

	var node corev1.Node
	err := r.Client.Get(ctx, req.NamespacedName, &node)
	switch {
	case err == nil:
		ev := entity.NodeToEvent(&node, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("Node", &node) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish node event: %w", perr)
		}
		logger.V(1).Info("published node upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.Node{}
		stub.Name = req.Name
		ev := entity.NodeToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish node deletion: %w", perr)
		}
		logger.V(1).Info("published node deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get node failed")
		return ctrl.Result{}, fmt.Errorf("get node %s: %w", req.Name, err)
	}
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *NodeReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Node{}).
		Named("node-watcher").
		Complete(r)
}
