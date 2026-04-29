// configmap_controller.go: controller for corev1.ConfigMap.
//
// Metadata-only by default; per-resource opt-in via the
// "omniscience.io/index-data" annotation. The mapper enforces this — the
// controller is the same shape as every other.
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
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// ConfigMapReconciler watches ConfigMaps and publishes change events.
type ConfigMapReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewConfigMapReconciler returns a reconciler with sane defaults.
func NewConfigMapReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterName string) (*ConfigMapReconciler, error) {
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
	return &ConfigMapReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every ConfigMap add/update/delete.
func (r *ConfigMapReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"configmap", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var cm corev1.ConfigMap
	err := r.Client.Get(ctx, req.NamespacedName, &cm)
	switch {
	case err == nil:
		ev := entity.ConfigMapToEvent(&cm, entity.ActionUpdated, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish configmap event: %w", perr)
		}
		logger.V(1).Info("published configmap upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.ConfigMap{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ConfigMapToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish configmap deletion: %w", perr)
		}
		logger.V(1).Info("published configmap deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get configmap failed")
		return ctrl.Result{}, fmt.Errorf("get configmap %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *ConfigMapReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.ConfigMap{}).
		Named("configmap-watcher").
		Complete(r)
}
