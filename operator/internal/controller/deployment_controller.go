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
	opmetrics "github.com/100rd/omniscience/operator/internal/metrics"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// DeploymentReconciler watches appsv1.Deployment objects and publishes
// change events. Mirrors PodReconciler exactly (see pod_controller.go for
// the documented semantics — Get-OK ⇒ upsert, NotFound ⇒ deleted, other
// errors ⇒ requeue).
type DeploymentReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewDeploymentReconciler returns a reconciler with sane defaults and the
// same precondition checks PodReconciler enforces (non-nil client, non-nil
// publisher, non-zero workspace, non-empty cluster name).
func NewDeploymentReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*DeploymentReconciler, error) {
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
	return &DeploymentReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile mirrors PodReconciler.Reconcile.
func (r *DeploymentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"deployment", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var d appsv1.Deployment
	err := r.Client.Get(ctx, req.NamespacedName, &d)
	switch {
	case err == nil:
		ev := entity.DeploymentToEvent(&d, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("Deployment", &d) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish deployment event: %w", perr)
		}
		logger.V(1).Info("published deployment upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &appsv1.Deployment{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.DeploymentToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish deployment deletion: %w", perr)
		}
		logger.V(1).Info("published deployment deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get deployment failed")
		return ctrl.Result{}, fmt.Errorf("get deployment %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the controller-runtime
// manager and binds it to the unique controller name "deployment-watcher".
func (r *DeploymentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&appsv1.Deployment{}).
		Named("deployment-watcher").
		Complete(r)
}
