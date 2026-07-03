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

	"github.com/100rd/Omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
	"github.com/100rd/Omniscience/operator/internal/publisher"
)

// JobReconciler watches batchv1.Job — see pod_controller.go for the
// documented reconcile contract. CronJob coverage is deferred to a
// follow-up sub-issue.
type JobReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewJobReconciler validates inputs identically to NewPodReconciler.
func NewJobReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*JobReconciler, error) {
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
	return &JobReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile mirrors PodReconciler.Reconcile.
func (r *JobReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"job", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var j batchv1.Job
	err := r.Client.Get(ctx, req.NamespacedName, &j)
	switch {
	case err == nil:
		ev := entity.JobToEvent(&j, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("Job", &j) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish job event: %w", perr)
		}
		logger.V(1).Info("published job upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &batchv1.Job{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.JobToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish job deletion: %w", perr)
		}
		logger.V(1).Info("published job deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get job failed")
		return ctrl.Result{}, fmt.Errorf("get job %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *JobReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&batchv1.Job{}).
		Named("job-watcher").
		Complete(r)
}
