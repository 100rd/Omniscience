// Package controller wires controller-runtime reconcilers for the operator.
//
// In v0.2 there is exactly one controller: the Pod watcher. It reconciles
// every Pod create/update/delete and emits an Event onto the NATS publisher.
// The reconcile is deliberately stateless — there is no in-cluster mirror to
// keep in sync; we are observation-only (see ADR-0007 §API model).
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

// PodReconciler watches Pods and publishes change events.
//
// Fields are populated by SetupWithManager / NewPodReconciler — never
// mutated after that.
type PodReconciler struct {
	Client      client.Client
	Publisher   publisher.Publisher
	WorkspaceID uuid.UUID
	ClusterName string
	// Now is the clock function used for event timestamps. Injectable so
	// tests can pin time deterministically.
	Now func() time.Time
}

// NewPodReconciler returns a reconciler with sane defaults.
func NewPodReconciler(c client.Client, pub publisher.Publisher, workspaceID uuid.UUID, clusterName string) (*PodReconciler, error) {
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
	return &PodReconciler{
		Client:      c,
		Publisher:   pub,
		WorkspaceID: workspaceID,
		ClusterName: clusterName,
		Now:         time.Now,
	}, nil
}

// Reconcile is invoked by controller-runtime on every Pod add/update/delete.
//
// Semantics:
//   - Pod GET succeeds → emit "created" or "updated" (the controller does not
//     distinguish between the two on a single reconcile because the manager
//     deduplicates equivalent events; the consumer treats both as upsert).
//   - Pod GET returns NotFound → the Pod has been deleted; emit "deleted".
//   - Any other error → return it; controller-runtime requeues with backoff.
func (r *PodReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues(
		"pod", req.String(),
		"workspace_id", r.WorkspaceID.String(),
	)

	var pod corev1.Pod
	err := r.Client.Get(ctx, req.NamespacedName, &pod)
	switch {
	case err == nil:
		// We do not have a "previously seen" record at this layer; the
		// server-side ingestion path is idempotent on (source_id,
		// external_id), so emitting "updated" for both create and update
		// is correct. A more precise mapping requires a finalizer or an
		// in-memory cache and is deferred to a follow-up issue.
		ev := entity.PodToEvent(&pod, entity.ActionUpdated, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish pod event: %w", perr)
		}
		logger.V(1).Info("published pod upsert", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	case apierrors.IsNotFound(err):
		// Pod has been deleted. We synthesise a minimal Pod with just the
		// namespaced name so the entity-id mapping is still well-formed.
		// We do not have status fields (phase, node) here — the deletion
		// event carries only identifying information, which is what the
		// downstream consumer needs to tombstone.
		stub := &corev1.Pod{}
		stub.Namespace = req.Namespace
		stub.Name = req.Name
		ev := entity.PodToEvent(stub, entity.ActionDeleted, r.WorkspaceID, r.ClusterName, r.Now())
		if perr := r.Publisher.Publish(ctx, ev); perr != nil {
			logger.Error(perr, "publish deletion failed; will requeue")
			return ctrl.Result{}, fmt.Errorf("publish pod deletion: %w", perr)
		}
		logger.V(1).Info("published pod deletion", "external_id", ev.ExternalID)
		return ctrl.Result{}, nil

	default:
		logger.Error(err, "get pod failed")
		return ctrl.Result{}, fmt.Errorf("get pod %s: %w", req.NamespacedName, err)
	}
}

// SetupWithManager registers the reconciler with the controller-runtime
// manager. The manager's cache provides the watch primitive; we do not
// configure rate-limiters or per-reconcile workers here — controller-runtime
// defaults are appropriate for the v0.2 single-replica operator.
func (r *PodReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Pod{}).
		Named("pod-watcher").
		Complete(r)
}
