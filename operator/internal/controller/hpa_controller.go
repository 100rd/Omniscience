// hpa_controller.go: controller for autoscalingv2.HorizontalPodAutoscaler (issue #214).
//
// Sub-task of epic #199 (v0.4 default-flip parity push) — the LAST
// release-blocker. After this lands, all NOT-COVERED gaps are addressed
// (only ReplicationController low-priority remains, deferred to v0.5).
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/Omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
	"github.com/100rd/Omniscience/operator/internal/publisher"
)

// HPAReconciler watches HorizontalPodAutoscalers and publishes change events.
type HPAReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewHPAReconciler returns a reconciler with sane defaults.
func NewHPAReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*HPAReconciler, error) {
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
	return &HPAReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every HPA add / update / delete.
func (r *HPAReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"horizontalpodautoscaler", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var hpa autoscalingv2.HorizontalPodAutoscaler
	err := r.Client.Get(ctx, req.NamespacedName, &hpa)
	switch {
	case err == nil:
		ev := entity.HorizontalPodAutoscalerToEvent(&hpa, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("HorizontalPodAutoscaler", &hpa)
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish hpa event: %w", perr)
		}
		logger.V(1).Info("published hpa upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &autoscalingv2.HorizontalPodAutoscaler{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.HorizontalPodAutoscalerToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish hpa deletion: %w", perr)
		}
		logger.V(1).Info("published hpa deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get hpa failed")
		return ctrl.Result{}, fmt.Errorf("get hpa %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *HPAReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&autoscalingv2.HorizontalPodAutoscaler{}).
		Named("hpa-watcher").
		Complete(r)
}
