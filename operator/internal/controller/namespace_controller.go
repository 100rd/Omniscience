// Package controller — NamespaceReconciler.
//
// Watches corev1.Namespace and emits one Event per reconcile. Same shape
// as the Pod and Node reconcilers; comments kept terse to avoid duplicating
// rationale already documented in pod_controller.go.
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

// NamespaceReconciler watches Namespaces and publishes change events.
type NamespaceReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewNamespaceReconciler returns a reconciler with sane defaults.
func NewNamespaceReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*NamespaceReconciler, error) {
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
	return &NamespaceReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile — see PodReconciler.Reconcile for the rationale.
func (r *NamespaceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"namespace", req.Name,
		"workspace_id", r.WorkspaceID.String(),
	)

	var ns corev1.Namespace
	err := r.Client.Get(ctx, req.NamespacedName, &ns)
	switch {
	case err == nil:
		ev := entity.NamespaceToEvent(&ns, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("Namespace", &ns) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish namespace event: %w", perr)
		}
		logger.V(1).Info("published namespace upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.Namespace{}
		stub.Name = req.Name
		ev := entity.NamespaceToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish namespace deletion: %w", perr)
		}
		logger.V(1).Info("published namespace deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get namespace failed")
		return ctrl.Result{}, fmt.Errorf("get namespace %s: %w", req.Name, err)
	}
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *NamespaceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Namespace{}).
		Named("namespace-watcher").
		Complete(r)
}
