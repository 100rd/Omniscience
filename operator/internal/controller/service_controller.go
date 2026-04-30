// service_controller.go: controller for corev1.Service.
//
// Mirrors PodReconciler exactly — see pod_controller.go for the design
// contract. A new controller per kind keeps the controller-manager metrics
// per-resource (named "service-watcher", "endpoints-watcher", …) and avoids
// type-erased generic gymnastics.
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

// ServiceReconciler watches Services and publishes change events.
type ServiceReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewServiceReconciler returns a reconciler with sane defaults.
func NewServiceReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*ServiceReconciler, error) {
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
	return &ServiceReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every Service add/update/delete.
func (r *ServiceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"service", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var svc corev1.Service
	err := r.Client.Get(ctx, req.NamespacedName, &svc)
	switch {
	case err == nil:
		ev := entity.ServiceToEvent(&svc, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish service event: %w", perr)
		}
		logger.V(1).Info("published service upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.Service{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ServiceToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish service deletion: %w", perr)
		}
		logger.V(1).Info("published service deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get service failed")
		return ctrl.Result{}, fmt.Errorf("get service %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *ServiceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Service{}).
		Named("service-watcher").
		Complete(r)
}
