// ingress_controller.go: controller for networkingv1.Ingress.
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/omniscience/operator/internal/entity"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// IngressReconciler watches Ingresses and publishes change events.
type IngressReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewIngressReconciler returns a reconciler with sane defaults.
func NewIngressReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterName string) (*IngressReconciler, error) {
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
	return &IngressReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every Ingress add/update/delete.
func (r *IngressReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"ingress", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var ing networkingv1.Ingress
	err := r.Client.Get(ctx, req.NamespacedName, &ing)
	switch {
	case err == nil:
		ev := entity.IngressToEvent(&ing, entity.ActionUpdated, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish ingress event: %w", perr)
		}
		logger.V(1).Info("published ingress upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &networkingv1.Ingress{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.IngressToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish ingress deletion: %w", perr)
		}
		logger.V(1).Info("published ingress deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get ingress failed")
		return ctrl.Result{}, fmt.Errorf("get ingress %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *IngressReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&networkingv1.Ingress{}).
		Named("ingress-watcher").
		Complete(r)
}
