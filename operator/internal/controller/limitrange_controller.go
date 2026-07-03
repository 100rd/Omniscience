// limitrange_controller.go: controller for corev1.LimitRange (issue #202).
//
// LimitRange is part of the v0.4 default-flip parity push (epic #199). The
// controller mirrors configmap_controller.go shape exactly — only the kind,
// the mapper, and the metric label change.
//
// RecordEmit placement matches the pattern wired in #201 across the other
// 23 controllers: the freshness probe is recorded BEFORE Publish, on the
// upsert (err == nil) branch only. Deletions do not emit a freshness
// observation because there is no resource state to be "fresh" against.
//
// Build dependency note (intentional, per #202 plan): this file imports the
// freshness helper from internal/metrics. That symbol lands in #201 (PR
// #201 → branch feat/operator-event-lag-wiring). If #201 has not merged at
// the time this branch is built from main, the package will not compile —
// that is the desired ordering signal for the human reviewer (merge #201
// first, then rebase this PR).
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

// LimitRangeReconciler watches LimitRanges and publishes change events.
type LimitRangeReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewLimitRangeReconciler returns a reconciler with sane defaults. Same
// precondition checks as every other controller — fail closed on any nil/
// zero input rather than silently emitting events with the zero workspace.
func NewLimitRangeReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*LimitRangeReconciler, error) {
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
	return &LimitRangeReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every LimitRange add /
// update / delete.
func (r *LimitRangeReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"limitrange", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var lr corev1.LimitRange
	err := r.Client.Get(ctx, req.NamespacedName, &lr)
	switch {
	case err == nil:
		ev := entity.LimitRangeToEvent(&lr, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("LimitRange", &lr) // #198/#201 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish limitrange event: %w", perr)
		}
		logger.V(1).Info("published limitrange upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.LimitRange{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.LimitRangeToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish limitrange deletion: %w", perr)
		}
		logger.V(1).Info("published limitrange deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get limitrange failed")
		return ctrl.Result{}, fmt.Errorf("get limitrange %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *LimitRangeReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.LimitRange{}).
		Named("limitrange-watcher").
		Complete(r)
}
