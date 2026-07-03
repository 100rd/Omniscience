// clusterrolebinding_controller.go: controller for rbacv1.ClusterRoleBinding (issue #212).
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/Omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
	"github.com/100rd/Omniscience/operator/internal/publisher"
)

// ClusterRoleBindingReconciler watches ClusterRoleBindings and publishes change events.
type ClusterRoleBindingReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

func NewClusterRoleBindingReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*ClusterRoleBindingReconciler, error) {
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
	return &ClusterRoleBindingReconciler{Client: c, Publisher: pub, WorkspaceID: workspaceID, ClusterID: clusterID, ClusterName: clusterName, Now: time.Now}, nil
}

func (r *ClusterRoleBindingReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("clusterrolebinding", req.String(), "workspace_id", r.WorkspaceID.String())

	var obj rbacv1.ClusterRoleBinding
	err := r.Client.Get(ctx, req.NamespacedName, &obj)
	switch {
	case err == nil:
		ev := entity.ClusterRoleBindingToEvent(&obj, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("ClusterRoleBinding", &obj)
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish clusterrolebinding event: %w", perr)
		}
		return ctrl.Result{}, nil
	case apierrors.IsNotFound(err):
		stub := &rbacv1.ClusterRoleBinding{}
		stub.Name = req.Name
		ev := entity.ClusterRoleBindingToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			return ctrl.Result{}, fmt.Errorf("publish clusterrolebinding deletion: %w", perr)
		}
		return ctrl.Result{}, nil
	default:
		return ctrl.Result{}, fmt.Errorf("get clusterrolebinding %s: %w", req.NamespacedName, err)
	}
}

func (r *ClusterRoleBindingReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).For(&rbacv1.ClusterRoleBinding{}).Named("clusterrolebinding-watcher").Complete(r)
}
