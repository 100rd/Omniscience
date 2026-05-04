// argo_rollout_controller.go — controller-runtime reconciler for the Argo
// Rollouts CRD (argoproj.io/v1alpha1.Rollout).
//
// Discovery is fail-open (issue #161 §A): the operator probes the API server
// for the `rollouts.argoproj.io` group at startup. When absent we log
// `argo_rollouts.crd.absent` at INFO and skip controller registration; the
// rest of the operator continues to run unaffected.
//
// Reconcile semantics mirror PodReconciler exactly — Get-OK ⇒ upsert,
// NotFound ⇒ deleted (stub object), other errors ⇒ requeue.
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	rolloutsv1alpha1 "github.com/argoproj/argo-rollouts/pkg/apis/rollouts/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/client-go/discovery"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/100rd/omniscience/operator/internal/entity"
	opmetrics "github.com/100rd/omniscience/operator/internal/metrics"
	"github.com/100rd/omniscience/operator/internal/publisher"
)

// ArgoRolloutsGroupVersion is the API group/version this controller probes
// for at startup. Pinned as a string constant (rather than imported from the
// argo-rollouts module's SchemeGroupVersion) so the discovery probe never
// fails closed on a module-version mismatch — the wire-level group/version
// is the stable contract.
const ArgoRolloutsGroupVersion = "argoproj.io/v1alpha1"

// ArgoRolloutsCRDAbsentLogKey is the structured log key emitted at INFO
// when the Rollouts CRD is not installed. Tools that grep `kubectl logs`
// for "argo_rollouts.crd.absent" rely on this exact spelling.
const ArgoRolloutsCRDAbsentLogKey = "argo_rollouts.crd.absent"

// ArgoRolloutReconciler watches argoproj.io/v1alpha1.Rollout objects and
// publishes change events. Mirrors DeploymentReconciler exactly.
type ArgoRolloutReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterID   uuid.UUID
	ClusterName string
	Now         func() time.Time
}

// NewArgoRolloutReconciler returns a reconciler with sane defaults and the
// same precondition checks the other reconcilers enforce.
func NewArgoRolloutReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string) (*ArgoRolloutReconciler, error) {
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
	return &ArgoRolloutReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile mirrors PodReconciler.Reconcile.
func (r *ArgoRolloutReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"rollout", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var ro rolloutsv1alpha1.Rollout
	err := r.Client.Get(ctx, req.NamespacedName, &ro)
	switch {
	case err == nil:
		ev := entity.RolloutToEvent(&ro, entity.ActionUpdated, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		opmetrics.RecordEmit("Rollout", &ro) // #198 freshness probe
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish rollout event: %w", perr)
		}
		logger.V(1).Info("published rollout upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		stub := &rolloutsv1alpha1.Rollout{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.RolloutToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish rollout deletion: %w", perr)
		}
		logger.V(1).Info("published rollout deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get rollout failed")
		return ctrl.Result{}, fmt.Errorf("get rollout %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the controller-runtime
// manager. The manager's scheme MUST include rolloutsv1alpha1 — see
// main.go where AddToScheme is called only after the discovery probe
// confirms the CRD is installed.
func (r *ArgoRolloutReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&rolloutsv1alpha1.Rollout{}).
		Named("argo-rollout-watcher").
		Complete(r)
}

// IsArgoRolloutsCRDInstalled returns true when the API server reports the
// `rollouts` resource under `argoproj.io/v1alpha1`. Returns false (and a nil
// error) on the documented "absent" path: the API server returns NotFound /
// NoMatchError. Returns false plus a non-nil error on truly transient
// failures (the manager startup will treat those as fatal so a discovery
// outage at boot does not silently disable the watcher).
//
// Why this signature: the discovery probe is the only place fail-open vs
// fail-closed is decided. By returning (bool, error) we keep the policy
// decision at the call site (main.go) where it is most reviewable.
func IsArgoRolloutsCRDInstalled(d discovery.DiscoveryInterface) (bool, error) {
	if d == nil {
		return false, errors.New("controller: discovery client must not be nil")
	}
	rl, err := d.ServerResourcesForGroupVersion(ArgoRolloutsGroupVersion)
	switch {
	case err == nil:
		// Present at the group/version level. Confirm the specific resource
		// is listed — a registered group with no resources is treated as
		// absent (the watcher would have nothing to watch).
		for _, r := range rl.APIResources {
			if r.Name == "rollouts" {
				return true, nil
			}
		}
		return false, nil
	case apierrors.IsNotFound(err):
		// The group/version is not registered. This is the documented
		// "Rollouts not installed" path — fail-open, no error.
		return false, nil
	default:
		// Transient discovery failure. The caller decides whether to retry
		// or treat this as fatal — we do not silently swallow.
		return false, fmt.Errorf("discovery probe %s: %w", ArgoRolloutsGroupVersion, err)
	}
}
