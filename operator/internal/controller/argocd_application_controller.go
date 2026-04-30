// argocd_application_controller.go: controller for argoproj.io/v1alpha1
// Application CRs.
//
// The watch is performed via *unstructured.Unstructured rather than a
// typed Application — see operator/internal/argocd/types.go for the
// rationale (importing argo-cd/v2 collapses go.sum onto k8s.io/* v0.26.x).
// Unstructured is decoded into argocd.Application via a JSON round-trip;
// missing optional fields decode to zero-values per the CRD-version-skew
// acceptance criterion.
//
// Registration of this controller is gated on CRD discovery (see
// argocd.DiscoverArgoCD); when the CRD is absent the controller is never
// registered with the manager and the Reconcile method is never called.
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
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/omniscience/operator/internal/argocd"
	"github.com/100rd/omniscience/operator/internal/entity"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// ArgoCDApplicationReconciler watches ArgoCD Applications and publishes
// change events. Mirrors PodReconciler's contract — Get-OK ⇒ upsert,
// NotFound ⇒ deleted, other errors ⇒ requeue.
type ArgoCDApplicationReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewArgoCDApplicationReconciler returns a reconciler with sane defaults
// and the same precondition checks the workload reconcilers enforce.
func NewArgoCDApplicationReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*ArgoCDApplicationReconciler, error) {
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
	return &ArgoCDApplicationReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile mirrors PodReconciler.Reconcile.
func (r *ArgoCDApplicationReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"application", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	u := newUnstructured(argocd.ApplicationGVK())
	err := r.Client.Get(ctx, req.NamespacedName, u)
	switch {
	case err == nil:
		app, derr := decodeApplication(u)
		if derr != nil {
			logger.Error(derr, "decode application failed; skipping reconcile")
			return ctrl.Result{}, nil
		}
		ev := entity.ApplicationToEvent(app, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish application event: %w", perr)
		}
		logger.V(1).Info("published application upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &argocd.Application{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.ApplicationToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish application deletion: %w", perr)
		}
		logger.V(1).Info("published application deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get application failed")
		return ctrl.Result{}, fmt.Errorf("get application %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the manager.
//
// We use ctrl.NewControllerManagedBy(...).For(unstructured.Unstructured)
// rather than a typed Application — same reason the package decodes via
// JSON round-trip (no scheme entry for argoproj.io/v1alpha1 to add).
func (r *ArgoCDApplicationReconciler) SetupWithManager(mgr ctrl.Manager) error {
	u := newUnstructured(argocd.ApplicationGVK())
	return ctrl.NewControllerManagedBy(mgr).
		For(u).
		Named("argocd-application-watcher").
		Complete(r)
}

// newUnstructured returns an unstructured.Unstructured stamped with the
// given GVK. The kind is required by controller-runtime's typed cache so
// it can look up the GVR via the RESTMapper at watch-setup time.
func newUnstructured(gvk schema.GroupVersionKind) *unstructured.Unstructured {
	u := &unstructured.Unstructured{}
	u.SetGroupVersionKind(gvk)
	return u
}

// decodeApplication converts an unstructured.Unstructured into the typed
// argocd.Application mirror via JSON round-trip. Missing optional fields
// decode to zero-values; a malformed CR (un-marshallable) returns an error
// the caller logs without requeuing.
func decodeApplication(u *unstructured.Unstructured) (*argocd.Application, error) {
	if u == nil {
		return nil, errors.New("decode application: nil unstructured")
	}
	raw, err := json.Marshal(u.Object)
	if err != nil {
		return nil, fmt.Errorf("marshal unstructured: %w", err)
	}
	out := &argocd.Application{}
	if err := json.Unmarshal(raw, out); err != nil {
		return nil, fmt.Errorf("unmarshal application: %w", err)
	}
	// JSON round-trip drops the *meta.NamespacedName synthesised by
	// controller-runtime; defensive copy of the bits the mapper reads.
	out.Namespace = u.GetNamespace()
	out.Name = u.GetName()
	out.UID = types.UID(u.GetUID())
	out.OwnerReferences = u.GetOwnerReferences()
	return out, nil
}
