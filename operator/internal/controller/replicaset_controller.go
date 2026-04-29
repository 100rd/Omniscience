package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	appsv1 "k8s.io/api/apps/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/omniscience/operator/internal/entity"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// ReplicaSetReconciler watches appsv1.ReplicaSet — see pod_controller.go
// for the documented reconcile contract.
type ReplicaSetReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewReplicaSetReconciler validates inputs identically to NewPodReconciler.
func NewReplicaSetReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterName string) (*ReplicaSetReconciler, error) {
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
	return &ReplicaSetReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile mirrors PodReconciler.Reconcile.
func (r *ReplicaSetReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"replicaset", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var rs appsv1.ReplicaSet
	err := r.Client.Get(ctx, req.NamespacedName, &rs)
	switch {
	case err == nil:
		ev := entity.ReplicaSetToEvent(&rs, entity.ActionUpdated, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish replicaset event: %w", perr)
		}
		logger.V(1).Info("published replicaset upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &appsv1.ReplicaSet{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ReplicaSetToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish replicaset deletion: %w", perr)
		}
		logger.V(1).Info("published replicaset deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get replicaset failed")
		return ctrl.Result{}, fmt.Errorf("get replicaset %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *ReplicaSetReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&appsv1.ReplicaSet{}).
		Named("replicaset-watcher").
		Complete(r)
}
