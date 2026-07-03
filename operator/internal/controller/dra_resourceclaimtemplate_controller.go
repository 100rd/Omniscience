// Package controller — ResourceClaimTemplateReconciler.
//
// Watches resource.k8s.io/v1beta1 ResourceClaimTemplate (namespaced) and emits one
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

// ResourceClaimTemplateReconciler watches ResourceClaimTemplates and publishes change events.
type ResourceClaimTemplateReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewResourceClaimTemplateReconciler returns a reconciler with sane defaults.
// Validation matches every other reconciler in this package.
func NewResourceClaimTemplateReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterName string) (*ResourceClaimTemplateReconciler, error) {
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
	return &ResourceClaimTemplateReconciler{
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
func (r *ResourceClaimTemplateReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"resourceclaimtemplate", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var rc resourcev1beta1.ResourceClaimTemplate
	err := r.Client.Get(ctx, req.NamespacedName, &rc)
	switch {
	case err == nil:
		// Derived cluster_id (issue #167); see dra_deviceclass_controller.go
		// for the rationale. Follow-up plumbs cfg.ClusterID through.
		ev := entity.ResourceClaimTemplateToEvent(&rc, entity.ActionUpdated, r.WorkspaceID, entity.DeriveClusterID(r.WorkspaceID, r.ClusterName), r.ClusterName, r.Now())
		opmetrics.RecordEmit("ResourceClaimTemplate", &rc) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish resourceclaimtemplate event: %w", perr)
		}
		logger.V(1).Info("published resourceclaimtemplate upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &resourcev1beta1.ResourceClaimTemplate{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ResourceClaimTemplateToEvent(stub, entity.ActionDeleted, r.WorkspaceID, entity.DeriveClusterID(r.WorkspaceID, r.ClusterName), r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish resourceclaimtemplate deletion: %w", perr)
		}
		logger.V(1).Info("published resourceclaimtemplate deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get resourceclaimtemplate failed")
		return ctrl.Result{}, fmt.Errorf("get resourceclaimtemplate %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with controller-runtime.
func (r *ResourceClaimTemplateReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&resourcev1beta1.ResourceClaimTemplate{}).
		Named("resourceclaimtemplate-watcher").
		Complete(r)
}
