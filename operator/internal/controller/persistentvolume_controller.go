// Package controller — PersistentVolumeReconciler.
//
// Watches corev1.PersistentVolume (cluster-scoped). Same reconcile shape as
// Pod/Node/Namespace; the mapper emits OF_CLASS edges to StorageClass when
// spec.storageClassName is set.
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

// PersistentVolumeReconciler watches PVs and publishes change events.
type PersistentVolumeReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewPersistentVolumeReconciler returns a reconciler with sane defaults.
func NewPersistentVolumeReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*PersistentVolumeReconciler, error) {
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
	return &PersistentVolumeReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile — see PodReconciler.Reconcile for the rationale.
func (r *PersistentVolumeReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"pv", req.Name,
		"workspace_id", r.WorkspaceID.String(),
	)

	var pv corev1.PersistentVolume
	err := r.Client.Get(ctx, req.NamespacedName, &pv)
	switch {
	case err == nil:
		ev := entity.PersistentVolumeToEvent(&pv, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("PersistentVolume", &pv) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish pv event: %w", perr)
		}
		logger.V(1).Info("published pv upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.PersistentVolume{}
		stub.Name = req.Name
		ev := entity.PersistentVolumeToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish pv deletion: %w", perr)
		}
		logger.V(1).Info("published pv deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get pv failed")
		return ctrl.Result{}, fmt.Errorf("get pv %s: %w", req.Name, err)
	}
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *PersistentVolumeReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.PersistentVolume{}).
		Named("persistentvolume-watcher").
		Complete(r)
}
