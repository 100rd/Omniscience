// resourcequota_controller.go: controller for corev1.ResourceQuota (issue #204).
//
// Sub-task of epic #199 (v0.4 default-flip parity push). Mirrors the
// LimitRange controller (#202) shape exactly — only the kind, the mapper,
// and the metric label change.
//
// RecordEmit placement matches the pattern wired in #198/#201/#202: the
// freshness probe is recorded BEFORE Publish, on the upsert (err == nil)
// branch only. Deletions do not emit a freshness observation because there
// is no resource state to be "fresh" against.
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

// ResourceQuotaReconciler watches ResourceQuotas and publishes change events.
type ResourceQuotaReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewResourceQuotaReconciler returns a reconciler with sane defaults. Same
// precondition checks as every other controller — fail closed on any nil/
// zero input rather than silently emitting events with the zero workspace.
func NewResourceQuotaReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*ResourceQuotaReconciler, error) {
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
	return &ResourceQuotaReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every ResourceQuota add /
// update / delete.
func (r *ResourceQuotaReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"resourcequota", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var rq corev1.ResourceQuota
	err := r.Client.Get(ctx, req.NamespacedName, &rq)
	switch {
	case err == nil:
		ev := entity.ResourceQuotaToEvent(&rq, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("ResourceQuota", &rq) // #198/#201 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish resourcequota event: %w", perr)
		}
		logger.V(1).Info("published resourcequota upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.ResourceQuota{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ResourceQuotaToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish resourcequota deletion: %w", perr)
		}
		logger.V(1).Info("published resourcequota deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get resourcequota failed")
		return ctrl.Result{}, fmt.Errorf("get resourcequota %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *ResourceQuotaReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.ResourceQuota{}).
		Named("resourcequota-watcher").
		Complete(r)
}
