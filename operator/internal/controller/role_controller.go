// role_controller.go: controller for rbacv1.Role (issue #212).
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

// RoleReconciler watches Roles and publishes change events.
type RoleReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

func NewRoleReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*RoleReconciler, error) {
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
	return &RoleReconciler{Client: c, Publisher: pub, WorkspaceID: workspaceID, ClusterID: clusterID, ClusterName: clusterName, Now: time.Now}, nil
}

func (r *RoleReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("role", req.String(), "workspace_id", r.WorkspaceID.String())

	var obj rbacv1.Role
	err := r.Client.Get(ctx, req.NamespacedName, &obj)
	switch {
	case err == nil:
		ev := entity.RoleToEvent(&obj, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("Role", &obj)
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish role event: %w", perr)
		}
		return ctrl.Result{}, nil
	case apierrors.IsNotFound(err):
		stub := &rbacv1.Role{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.RoleToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			return ctrl.Result{}, fmt.Errorf("publish role deletion: %w", perr)
		}
		return ctrl.Result{}, nil
	default:
		return ctrl.Result{}, fmt.Errorf("get role %s: %w", req.NamespacedName, err)
	}
}

func (r *RoleReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).For(&rbacv1.Role{}).Named("role-watcher").Complete(r)
}
