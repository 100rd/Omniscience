// Per-kind metadata and dispatch for the reconciliation worker.
//
// The reconciler iterates over a deterministic list of kinds. For each
// kind it needs three operations packaged in a KindHandler:
//
//  1. ListInCluster — read the cluster, return a map keyed on external_id
//     whose value is a *closure* that emits the corresponding "created"
//     event (closing over the live K8s object). Keys feed the diff; values
//     drive the missing-in-graph emit path. The keys-as-strings list is
//     handed to Diff() which only cares about set membership; storing the
//     full object behind the key lets us emit without a second Get round
//     trip and keeps the reconciler's transient memory footprint at one
//     copy of the cluster's objects per kind per cycle.
//
//  2. EmitDeleted — produce a "deleted" event for an external_id present
//     in the graph but missing from the cluster. The external_id format
//     ("k8s_resource/{cluster_id}/{Kind}/{namespace}/{name}" or
//     "k8s_resource/{cluster_id}/{Kind}/{name}" for cluster-scoped) is
//     reversed into (Kind, namespace, name). A stub object with only
//     namespace+name is fed to the mapper, mirroring how the existing
//     watch controllers handle NotFound deletions (see
//     deployment_controller.go's stub path).
//
// We deliberately reuse the live mappers (operator/internal/entity/*) so
// the reconciler and watch path produce byte-identical external_ids —
// that is the symmetric-difference invariant Diff depends on.
//
// The kind list intentionally subsets the watcher coverage. Optional CRDs
// (ArgoCD, Argo Rollouts, DRA) are listed only when the API server has
// them — discovery-gated registration mirrors the watcher startup path
// (see cmd/manager/main.go ── #161 / #160 / #190 ──). v0.2 ships without
// optional-CRD reconciliation; coverage parity will be a follow-up.
package reconciler

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	storagev1 "k8s.io/api/storage/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/100rd/omniscience/operator/internal/entity"
)

// EventBuilder is a closure that emits an Event on demand. The reconciler
// keeps one of these per in-cluster object so it can emit a synthetic
// "created" event for items missing from the graph without re-Get'ing.
type EventBuilder func(workspaceID, clusterID uuid.UUID, clusterName string, now time.Time) *entity.Event

// KindHandler bundles the operations the reconciler performs per kind.
type KindHandler struct {
	// Name is the K8s API Kind string (e.g. "Pod", "Deployment"). Used as
	// the metric `kind` label and as the request to the read API.
	Name string

	// ListInCluster reads the cluster and returns a map external_id →
	// EventBuilder for every in-cluster object of this kind. The keys
	// feed the diff; the closures drive missing-in-graph emit.
	ListInCluster func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error)

	// EmitDeleted constructs a deletion event for an external_id present in
	// the graph but missing from the cluster. The external_id is parsed
	// back to (namespace, name) and a stub object fed to the mapper.
	EmitDeleted func(externalID string, workspaceID, clusterID uuid.UUID, clusterName string, now time.Time) (*entity.Event, error)
}

// kindRegistry returns every KindHandler the reconciler covers in v0.2.
// Order is fixed and deterministic so reconcile cycles are diff-stable
// across operator restarts (helps when grepping logs).
//
// The list mirrors the watcher set in cmd/manager/main.go for the kinds
// that the operator reliably watches without optional-CRD discovery.
func kindRegistry() []KindHandler {
	return []KindHandler{
		// Workload kinds — the highest-volume drift candidates.
		newPodHandler(),
		newDeploymentHandler(),
		newReplicaSetHandler(),
		newStatefulSetHandler(),
		newDaemonSetHandler(),
		newJobHandler(),
		// Networking + config kinds (#158).
		newServiceHandler(),
		newEndpointsHandler(),
		newIngressHandler(),
		newNetworkPolicyHandler(),
		newConfigMapHandler(),
		newSecretHandler(),
		// Cluster-scoped kinds (#159).
		newNodeHandler(),
		newNamespaceHandler(),
		newPersistentVolumeHandler(),
		newStorageClassHandler(),
	}
}

// ---------------------------------------------------------------------------
// Per-kind handler constructors. Each is a small named function rather than
// an inline literal so a stack trace points at the right kind.
// ---------------------------------------------------------------------------

func newPodHandler() KindHandler {
	return KindHandler{
		Name: "Pod",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.PodList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "Pod", obj.Namespace, obj.Name)
				// In Go 1.22+, loop variables are per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.PodToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("Pod", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.Pod{}
				stub.Namespace = ns
				stub.Name = name
				return entity.PodToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newDeploymentHandler() KindHandler {
	return KindHandler{
		Name: "Deployment",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list appsv1.DeploymentList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "Deployment", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.DeploymentToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("Deployment", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &appsv1.Deployment{}
				stub.Namespace = ns
				stub.Name = name
				return entity.DeploymentToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newReplicaSetHandler() KindHandler {
	return KindHandler{
		Name: "ReplicaSet",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list appsv1.ReplicaSetList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "ReplicaSet", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.ReplicaSetToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("ReplicaSet", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &appsv1.ReplicaSet{}
				stub.Namespace = ns
				stub.Name = name
				return entity.ReplicaSetToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newStatefulSetHandler() KindHandler {
	return KindHandler{
		Name: "StatefulSet",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list appsv1.StatefulSetList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "StatefulSet", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.StatefulSetToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("StatefulSet", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &appsv1.StatefulSet{}
				stub.Namespace = ns
				stub.Name = name
				return entity.StatefulSetToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newDaemonSetHandler() KindHandler {
	return KindHandler{
		Name: "DaemonSet",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list appsv1.DaemonSetList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "DaemonSet", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.DaemonSetToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("DaemonSet", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &appsv1.DaemonSet{}
				stub.Namespace = ns
				stub.Name = name
				return entity.DaemonSetToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newJobHandler() KindHandler {
	return KindHandler{
		Name: "Job",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list batchv1.JobList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "Job", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.JobToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("Job", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &batchv1.Job{}
				stub.Namespace = ns
				stub.Name = name
				return entity.JobToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newServiceHandler() KindHandler {
	return KindHandler{
		Name: "Service",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.ServiceList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "Service", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.ServiceToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("Service", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.Service{}
				stub.Namespace = ns
				stub.Name = name
				return entity.ServiceToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newEndpointsHandler() KindHandler {
	return KindHandler{
		Name: "Endpoints",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.EndpointsList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "Endpoints", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.EndpointsToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("Endpoints", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.Endpoints{}
				stub.Namespace = ns
				stub.Name = name
				return entity.EndpointsToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newIngressHandler() KindHandler {
	return KindHandler{
		Name: "Ingress",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list networkingv1.IngressList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "Ingress", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.IngressToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("Ingress", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &networkingv1.Ingress{}
				stub.Namespace = ns
				stub.Name = name
				return entity.IngressToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newNetworkPolicyHandler() KindHandler {
	return KindHandler{
		Name: "NetworkPolicy",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list networkingv1.NetworkPolicyList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "NetworkPolicy", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.NetworkPolicyToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("NetworkPolicy", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &networkingv1.NetworkPolicy{}
				stub.Namespace = ns
				stub.Name = name
				return entity.NetworkPolicyToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newConfigMapHandler() KindHandler {
	return KindHandler{
		Name: "ConfigMap",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.ConfigMapList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "ConfigMap", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.ConfigMapToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("ConfigMap", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.ConfigMap{}
				stub.Namespace = ns
				stub.Name = name
				return entity.ConfigMapToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newSecretHandler() KindHandler {
	return KindHandler{
		Name: "Secret",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.SecretList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := namespacedExternalID(clusterID, "Secret", obj.Namespace, obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.SecretToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: namespacedDeleter("Secret", func(ns, name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.Secret{}
				stub.Namespace = ns
				stub.Name = name
				return entity.SecretToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newNodeHandler() KindHandler {
	return KindHandler{
		Name: "Node",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.NodeList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := clusterScopedExternalID(clusterID, "Node", obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.NodeToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: clusterScopedDeleter("Node", func(name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name}}
				return entity.NodeToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newNamespaceHandler() KindHandler {
	return KindHandler{
		Name: "Namespace",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.NamespaceList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := clusterScopedExternalID(clusterID, "Namespace", obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.NamespaceToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: clusterScopedDeleter("Namespace", func(name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}
				return entity.NamespaceToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newPersistentVolumeHandler() KindHandler {
	return KindHandler{
		Name: "PersistentVolume",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list corev1.PersistentVolumeList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := clusterScopedExternalID(clusterID, "PersistentVolume", obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.PersistentVolumeToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: clusterScopedDeleter("PersistentVolume", func(name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &corev1.PersistentVolume{ObjectMeta: metav1.ObjectMeta{Name: name}}
				return entity.PersistentVolumeToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

func newStorageClassHandler() KindHandler {
	return KindHandler{
		Name: "StorageClass",
		ListInCluster: func(ctx context.Context, c client.Client, clusterID uuid.UUID) (map[string]EventBuilder, error) {
			var list storagev1.StorageClassList
			if err := c.List(ctx, &list); err != nil {
				return nil, err
			}
			out := make(map[string]EventBuilder, len(list.Items))
			for i := range list.Items {
				obj := &list.Items[i]
				ext := clusterScopedExternalID(clusterID, "StorageClass", obj.Name)
				// Go 1.22 loop var is per-iteration; capture is implicit.
				out[ext] = func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
					return entity.StorageClassToEvent(obj, entity.ActionCreated, ws, cid, cname, now)
				}
			}
			return out, nil
		},
		EmitDeleted: clusterScopedDeleter("StorageClass", func(name string) entityFromStub {
			return func(ws, cid uuid.UUID, cname string, now time.Time) *entity.Event {
				stub := &storagev1.StorageClass{ObjectMeta: metav1.ObjectMeta{Name: name}}
				return entity.StorageClassToEvent(stub, entity.ActionDeleted, ws, cid, cname, now)
			}
		}),
	}
}

// ---------------------------------------------------------------------------
// External-id format helpers (production and parse).
//
// These mirror operator/internal/entity/common.go::externalIDFor and
// externalIDForClusterScoped exactly, so the reconciler's strings line up
// byte-for-byte with what the watch path emits.
//
// We do NOT call into the entity package's externalIDFor (it is exported but
// keeping the format duplicated here is a deliberate pact with one rule:
// any change to one MUST land with a matching change to the other,
// enforced by integration tests that compare watch-emitted external_ids
// against reconciler-emitted ones for the same cluster object).
// ---------------------------------------------------------------------------

// namespacedExternalID returns "k8s_resource/{cluster_id}/{Kind}/{namespace}/{name}".
func namespacedExternalID(clusterID uuid.UUID, kind, namespace, name string) string {
	if namespace == "" {
		namespace = "default"
	}
	return entity.EntityKindPrefix + "/" + clusterID.String() + "/" + kind + "/" + namespace + "/" + name
}

// clusterScopedExternalID returns "k8s_resource/{cluster_id}/{Kind}/{name}".
func clusterScopedExternalID(clusterID uuid.UUID, kind, name string) string {
	return entity.EntityKindPrefix + "/" + clusterID.String() + "/" + kind + "/" + name
}

// parseNamespacedExternalID reverses namespacedExternalID: returns
// (namespace, name) or an error when the prefix or shape is wrong.
func parseNamespacedExternalID(externalID, expectedKind string) (namespace, name string, err error) {
	parts := strings.Split(externalID, "/")
	// Expected: ["k8s_resource", "{cid}", "{Kind}", "{ns}", "{name}"]
	if len(parts) != 5 {
		return "", "", fmt.Errorf("reconciler: malformed namespaced external_id %q", externalID)
	}
	if parts[0] != entity.EntityKindPrefix {
		return "", "", fmt.Errorf("reconciler: external_id has unexpected prefix %q", parts[0])
	}
	if parts[2] != expectedKind {
		return "", "", fmt.Errorf("reconciler: external_id kind %q does not match expected %q", parts[2], expectedKind)
	}
	if parts[3] == "" || parts[4] == "" {
		return "", "", fmt.Errorf("reconciler: external_id missing namespace or name: %q", externalID)
	}
	return parts[3], parts[4], nil
}

// parseClusterScopedExternalID reverses clusterScopedExternalID.
func parseClusterScopedExternalID(externalID, expectedKind string) (name string, err error) {
	parts := strings.Split(externalID, "/")
	// Expected: ["k8s_resource", "{cid}", "{Kind}", "{name}"]
	if len(parts) != 4 {
		return "", fmt.Errorf("reconciler: malformed cluster-scoped external_id %q", externalID)
	}
	if parts[0] != entity.EntityKindPrefix {
		return "", fmt.Errorf("reconciler: external_id has unexpected prefix %q", parts[0])
	}
	if parts[2] != expectedKind {
		return "", fmt.Errorf("reconciler: external_id kind %q does not match expected %q", parts[2], expectedKind)
	}
	if parts[3] == "" {
		return "", fmt.Errorf("reconciler: external_id missing name: %q", externalID)
	}
	return parts[3], nil
}

// entityFromStub binds (ns, name) at dispatch and accepts (workspace, cluster, ts) at emit.
type entityFromStub func(workspaceID, clusterID uuid.UUID, clusterName string, now time.Time) *entity.Event

// namespacedDeleter wraps a stub-builder closure into the EmitDeleted shape.
func namespacedDeleter(kind string, stubBuilder func(ns, name string) entityFromStub) func(string, uuid.UUID, uuid.UUID, string, time.Time) (*entity.Event, error) {
	return func(externalID string, ws, cid uuid.UUID, cname string, now time.Time) (*entity.Event, error) {
		ns, name, err := parseNamespacedExternalID(externalID, kind)
		if err != nil {
			return nil, err
		}
		fn := stubBuilder(ns, name)
		ev := fn(ws, cid, cname, now)
		if ev == nil {
			return nil, errors.New("reconciler: stub mapper returned nil event")
		}
		return ev, nil
	}
}

// clusterScopedDeleter is the cluster-scoped counterpart of namespacedDeleter.
func clusterScopedDeleter(kind string, stubBuilder func(name string) entityFromStub) func(string, uuid.UUID, uuid.UUID, string, time.Time) (*entity.Event, error) {
	return func(externalID string, ws, cid uuid.UUID, cname string, now time.Time) (*entity.Event, error) {
		name, err := parseClusterScopedExternalID(externalID, kind)
		if err != nil {
			return nil, err
		}
		fn := stubBuilder(name)
		ev := fn(ws, cid, cname, now)
		if ev == nil {
			return nil, errors.New("reconciler: stub mapper returned nil event")
		}
		return ev, nil
	}
}
