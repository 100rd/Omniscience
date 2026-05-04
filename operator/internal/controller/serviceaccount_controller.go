// serviceaccount_controller.go: controller for corev1.ServiceAccount (issue #206).
//
// Sub-task of epic #199 (v0.4 default-flip parity push). Mirrors the
// LimitRange (#202) / ResourceQuota (#204) shape exactly.
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

	"github.com/100rd/omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/omniscience/operator/internal/metrics"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// ServiceAccountReconciler watches ServiceAccounts and publishes change events.
type ServiceAccountReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewServiceAccountReconciler returns a reconciler with sane defaults. Same
// precondition checks as every other controller — fail closed on any nil/
// zero input rather than silently emitting events with the zero workspace.
func NewServiceAccountReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*ServiceAccountReconciler, error) {
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
	return &ServiceAccountReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every ServiceAccount add /
// update / delete.
func (r *ServiceAccountReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"serviceaccount", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var sa corev1.ServiceAccount
	err := r.Client.Get(ctx, req.NamespacedName, &sa)
	switch {
	case err == nil:
		ev := entity.ServiceAccountToEvent(&sa, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("ServiceAccount", &sa) // #198/#201 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish serviceaccount event: %w", perr)
		}
		logger.V(1).Info("published serviceaccount upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.ServiceAccount{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ServiceAccountToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish serviceaccount deletion: %w", perr)
		}
		logger.V(1).Info("published serviceaccount deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get serviceaccount failed")
		return ctrl.Result{}, fmt.Errorf("get serviceaccount %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *ServiceAccountReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.ServiceAccount{}).
		Named("serviceaccount-watcher").
		Complete(r)
}
