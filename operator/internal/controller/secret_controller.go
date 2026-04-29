// secret_controller.go: controller for corev1.Secret.
//
// SECURITY POSTURE — see internal/entity/secret.go for the hard rule. The
// controller is structurally identical to every other watcher; the value-
// dropping happens in SecretToEvent. Doing the drop in the mapper means it
// is unit-testable without a live cluster, and any future leak shows up in
// secret_test.go's structural assertion.
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

// SecretReconciler watches Secrets and publishes change events. Values are
// dropped at the mapper boundary — see internal/entity/secret.go.
type SecretReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewSecretReconciler returns a reconciler with sane defaults.
func NewSecretReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterName string) (*SecretReconciler, error) {
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
	return &SecretReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every Secret add/update/delete.
func (r *SecretReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"secret", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var s corev1.Secret
	err := r.Client.Get(ctx, req.NamespacedName, &s)
	switch {
	case err == nil:
		ev := entity.SecretToEvent(&s, entity.ActionUpdated, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish secret event: %w", perr)
		}
		// Intentionally do NOT log the external_id at debug level for Secrets
		// — the resource name itself can be sensitive (e.g. "stripe-prod-key").
		// Pod and other watchers log the name; Secret does not. This is a
		// defence-in-depth measure beyond the mapper-level value drop.
		logger.V(1).Info("published secret upsert")
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &corev1.Secret{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.SecretToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish secret deletion: %w", perr)
		}
		logger.V(1).Info("published secret deletion")
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get secret failed")
		return ctrl.Result{}, fmt.Errorf("get secret %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *SecretReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Secret{}).
		Named("secret-watcher").
		Complete(r)
}
