// Package controller — DeviceClassReconciler.
//
// Watches resource.k8s.io/v1beta1 DeviceClass (namespaced) and emits one
// Event per reconcile. The shape mirrors PodReconciler exactly so a reader
// who already understands Pod can read this in one pass; the only differences
// are the kind being watched, the mapper invoked, and the API version.
//
// Registration is gated on DRA-API discovery in main.go — when the cluster
// does not serve resource.k8s.io/v1beta1 (operator on a 1.30 cluster, or 1.31
// without the feature gate), this reconciler is never instantiated and the
// operator runs without DRA. See cmd/manager/main.go for the discovery path.
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	resourcev1beta1 "k8s.io/api/resource/v1beta1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/Omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/Omniscience/operator/internal/metrics"
	"github.com/100rd/Omniscience/operator/internal/publisher"
)

// DeviceClassReconciler watches DeviceClasss and publishes change events.
type DeviceClassReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewDeviceClassReconciler returns a reconciler with sane defaults.
// Validation matches every other reconciler in this package.
func NewDeviceClassReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterName string) (*DeviceClassReconciler, error) {
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
	return &DeviceClassReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile follows the Pod reconciler's exact semantics. GET success →
// upsert event; NotFound → deletion event with a stub bearing only the
// identifying metadata; other error → requeue.
func (r *DeviceClassReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"deviceclass", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var rc resourcev1beta1.DeviceClass
	err := r.Client.Get(ctx, req.NamespacedName, &rc)
	switch {
	case err == nil:
		// Derived cluster_id (issue #167); the DRA reconciler does not yet
		// carry a ClusterID field — the v0.2 deterministic uuid5 derivation
		// gives the same value the operator config would compute, keeping
		// the watch path's external_ids stable for single-cluster deployments.
		// Multi-cluster DRA coverage requires a follow-up that plumbs
		// cfg.ClusterID through this constructor.
		ev := entity.DeviceClassToEvent(&rc, entity.ActionUpdated, r.WorkspaceID, entity.DeriveClusterID(r.WorkspaceID, r.ClusterName), r.ClusterName, r.Now())
		opmetrics.RecordEmit("DeviceClass", &rc) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish deviceclass event: %w", perr)
		}
		logger.V(1).Info("published deviceclass upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &resourcev1beta1.DeviceClass{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.DeviceClassToEvent(stub, entity.ActionDeleted, r.WorkspaceID, entity.DeriveClusterID(r.WorkspaceID, r.ClusterName), r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish deviceclass deletion: %w", perr)
		}
		logger.V(1).Info("published deviceclass deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get deviceclass failed")
		return ctrl.Result{}, fmt.Errorf("get deviceclass %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *DeviceClassReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&resourcev1beta1.DeviceClass{}).
		Named("deviceclass-watcher").
		Complete(r)
}
