// argocd_applicationset_controller.go: controller for argoproj.io/v1alpha1
// ApplicationSet CRs. Mirrors argocd_application_controller.go.
package controller

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/omniscience/operator/internal/argocd"
	"github.com/100rd/omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/omniscience/operator/internal/metrics"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// ArgoCDApplicationSetReconciler watches ArgoCD ApplicationSets.
type ArgoCDApplicationSetReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewArgoCDApplicationSetReconciler returns a reconciler with sane defaults.
func NewArgoCDApplicationSetReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*ArgoCDApplicationSetReconciler, error) {
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
	return &ArgoCDApplicationSetReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile mirrors the workload reconcilers.
func (r *ArgoCDApplicationSetReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"applicationset", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	u := newUnstructured(argocd.ApplicationSetGVK())
	err := r.Client.Get(ctx, req.NamespacedName, u)
	switch {
	case err == nil:
		appset, derr := decodeApplicationSet(u)
		if derr != nil {
			logger.Error(derr, "decode applicationset failed; skipping reconcile")
			return ctrl.Result{}, nil
		}
		ev := entity.ApplicationSetToEvent(appset, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit(argocd.KindApplicationSet, u) // #198 freshness probe — use unstructured for full ObjectMeta
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish applicationset event: %w", perr)
		}
		logger.V(1).Info("published applicationset upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &argocd.ApplicationSet{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ApplicationSetToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish applicationset deletion: %w", perr)
		}
		logger.V(1).Info("published applicationset deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get applicationset failed")
		return ctrl.Result{}, fmt.Errorf("get applicationset %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
func (r *ArgoCDApplicationSetReconciler) SetupWithManager(mgr ctrl.Manager) error {
	u := newUnstructured(argocd.ApplicationSetGVK())
	return ctrl.NewControllerManagedBy(mgr).
		For(u).
		Named("argocd-applicationset-watcher").
		Complete(r)
}

// decodeApplicationSet — see decodeApplication for rationale.
func decodeApplicationSet(u *unstructured.Unstructured) (*argocd.ApplicationSet, error) {
	if u == nil {
		return nil, errors.New("decode applicationset: nil unstructured")
	}
	raw, err := json.Marshal(u.Object)
	if err != nil {
		return nil, fmt.Errorf("marshal unstructured: %w", err)
	}
	out := &argocd.ApplicationSet{}
	if err := json.Unmarshal(raw, out); err != nil {
		return nil, fmt.Errorf("unmarshal applicationset: %w", err)
	}
	out.Namespace = u.GetNamespace()
	out.Name = u.GetName()
	out.UID = types.UID(u.GetUID())
	out.OwnerReferences = u.GetOwnerReferences()
	return out, nil
}
