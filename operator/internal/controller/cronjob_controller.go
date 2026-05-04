// cronjob_controller.go: controller for batchv1.CronJob (issue #210).
//
// Sub-task of epic #199 (v0.4 default-flip parity push). The Job watcher
// from #157 already covers the actual Jobs that this CronJob spawns; this
// adds the parent CronJob entity (schedule + status) without duplicating
// the per-Job spec.
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/omniscience/operator/internal/metrics"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// CronJobReconciler watches CronJobs and publishes change events.
type CronJobReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewCronJobReconciler returns a reconciler with sane defaults.
func NewCronJobReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*CronJobReconciler, error) {
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
	return &CronJobReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every CronJob add / update /
// delete.
func (r *CronJobReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"cronjob", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var cj batchv1.CronJob
	err := r.Client.Get(ctx, req.NamespacedName, &cj)
	switch {
	case err == nil:
		ev := entity.CronJobToEvent(&cj, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("CronJob", &cj) // #198/#201 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish cronjob event: %w", perr)
		}
		logger.V(1).Info("published cronjob upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &batchv1.CronJob{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.CronJobToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish cronjob deletion: %w", perr)
		}
		logger.V(1).Info("published cronjob deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get cronjob failed")
		return ctrl.Result{}, fmt.Errorf("get cronjob %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *CronJobReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&batchv1.CronJob{}).
		Named("cronjob-watcher").
		Complete(r)
}
