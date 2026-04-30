// Package argocd provides a minimal, JSON-tagged Go mirror of the ArgoCD
// v1alpha1 CRD fields the operator actually reads.
//
// Why not import github.com/argoproj/argo-cd/v2/pkg/apis/application/v1alpha1?
//
//   - That module pins k8s.io/api / apimachinery / client-go to v0.26.x and
//     drags in the entire ArgoCD server (including kustomize, kubectl and
//     kube-aggregator). The operator runs on k8s.io/* v0.31.1 (see Wave 2,
//     issues #157 / #158 / #159). Mixing the two collapses go.sum.
//   - The operator only reads a small, stable subset of fields:
//       spec.project, spec.source.repoURL, spec.source.path,
//       spec.source.targetRevision, spec.destination.server,
//       spec.destination.namespace, status.sync.status, status.health.status,
//       status.operationState.phase, status.resources[],
//       spec.generators[] (ApplicationSet),
//       spec.template.metadata.name (ApplicationSet),
//       status.applicationStatus[] (ApplicationSet, optional/newer field).
//   - Going through unstructured + a thin typed mirror is the standard
//     pattern for operators that read CRDs they don't own. The mirror only
//     decodes fields we explicitly read; missing optional fields (older
//     ArgoCD installs) decode to zero-values, never panics. Issue #160
//     explicitly requires this fall-back behaviour.
//
// The on-the-wire shape is whatever ArgoCD writes; we only describe the bits
// we read. Decode is always best-effort: a malformed nested field returns
// zero-values for its enclosing struct so the mapper continues.
package argocd

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// Group / Version / Resource constants for ArgoCD CRDs. Pinned to the
// upstream v1alpha1 line — ArgoCD has not bumped past v1alpha1 in any
// shipped release through v2.14, and the v0.2 operator targets the
// stable ArgoCD line v2.10–v2.14. Newer minor versions add OPTIONAL
// fields; missing optional fields decode to zero-values here.
const (
	GroupName              = "argoproj.io"
	Version                = "v1alpha1"
	ResourceApplications   = "applications"
	ResourceApplicationSet = "applicationsets"
	KindApplication        = "Application"
	KindApplicationSet     = "ApplicationSet"
)

// GVR / GVK helpers.
func ApplicationGVR() schema.GroupVersionResource {
	return schema.GroupVersionResource{Group: GroupName, Version: Version, Resource: ResourceApplications}
}

func ApplicationSetGVR() schema.GroupVersionResource {
	return schema.GroupVersionResource{Group: GroupName, Version: Version, Resource: ResourceApplicationSet}
}

func ApplicationGVK() schema.GroupVersionKind {
	return schema.GroupVersionKind{Group: GroupName, Version: Version, Kind: KindApplication}
}

func ApplicationSetGVK() schema.GroupVersionKind {
	return schema.GroupVersionKind{Group: GroupName, Version: Version, Kind: KindApplicationSet}
}

// ───────────────────── Application ─────────────────────────────────────────

// Application is a typed mirror of argoproj.io/v1alpha1/Application restricted
// to the read-only fields the operator surfaces. All fields are pointer- or
// zero-value-friendly so a partial CR (older ArgoCD installation, missing
// status block, etc.) decodes without panic.
type Application struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ApplicationSpec   `json:"spec,omitempty"`
	Status ApplicationStatus `json:"status,omitempty"`
}

// ApplicationSpec is the read subset of spec.
type ApplicationSpec struct {
	Project     string                 `json:"project,omitempty"`
	Source      *ApplicationSource     `json:"source,omitempty"`
	Destination ApplicationDestination `json:"destination,omitempty"`
}

// ApplicationSource is the read subset of spec.source.
type ApplicationSource struct {
	RepoURL        string `json:"repoURL,omitempty"`
	Path           string `json:"path,omitempty"`
	TargetRevision string `json:"targetRevision,omitempty"`
}

// ApplicationDestination is the read subset of spec.destination.
type ApplicationDestination struct {
	Server    string `json:"server,omitempty"`
	Namespace string `json:"namespace,omitempty"`
	Name      string `json:"name,omitempty"`
}

// ApplicationStatus is the read subset of status.
type ApplicationStatus struct {
	Sync           SyncStatus           `json:"sync,omitempty"`
	Health         HealthStatus         `json:"health,omitempty"`
	OperationState *OperationState      `json:"operationState,omitempty"`
	Resources      []ResourceStatus     `json:"resources,omitempty"`
}

// SyncStatus mirrors status.sync.
type SyncStatus struct {
	Status string `json:"status,omitempty"` // Synced | OutOfSync | Unknown
}

// HealthStatus mirrors status.health.
type HealthStatus struct {
	Status string `json:"status,omitempty"` // Healthy | Degraded | Progressing | Suspended | Missing
}

// OperationState mirrors status.operationState.
type OperationState struct {
	Phase string `json:"phase,omitempty"`
}

// ResourceStatus is one entry in status.resources[]. ArgoCD populates this
// list with the actual rendered cluster resources the Application manages,
// after manifests have been applied. Non-K8s resources (e.g. CRDs not in
// the cluster) appear here too; we emit edges to whatever ArgoCD reports.
type ResourceStatus struct {
	Group     string `json:"group,omitempty"`
	Version   string `json:"version,omitempty"`
	Kind      string `json:"kind,omitempty"`
	Namespace string `json:"namespace,omitempty"`
	Name      string `json:"name,omitempty"`
}

// ───────────────────── ApplicationSet ──────────────────────────────────────

// ApplicationSet is a typed mirror of argoproj.io/v1alpha1/ApplicationSet
// restricted to the read-only fields the operator surfaces.
type ApplicationSet struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ApplicationSetSpec   `json:"spec,omitempty"`
	Status ApplicationSetStatus `json:"status,omitempty"`
}

// ApplicationSetSpec is the read subset of spec.
type ApplicationSetSpec struct {
	Generators []ApplicationSetGenerator `json:"generators,omitempty"`
	Template   ApplicationSetTemplate    `json:"template,omitempty"`
}

// ApplicationSetGenerator captures only the *type* of each generator, never
// its content. ArgoCD generators include `list`, `git`, `cluster`, `matrix`,
// `merge`, `pullRequest`, `scmProvider`, `clusterDecisionResource`. We mark
// presence of each so the entity carries the generator-type list per the
// allow-list ("never their content").
//
// Decoding from unstructured detects which key is non-nil (the wire format
// uses one-of-many top-level fields). The Names() method returns the active
// generator names in stable order.
type ApplicationSetGenerator struct {
	List                    *unstructured.Unstructured `json:"list,omitempty"`
	Clusters                *unstructured.Unstructured `json:"clusters,omitempty"`
	Git                     *unstructured.Unstructured `json:"git,omitempty"`
	SCMProvider             *unstructured.Unstructured `json:"scmProvider,omitempty"`
	ClusterDecisionResource *unstructured.Unstructured `json:"clusterDecisionResource,omitempty"`
	PullRequest             *unstructured.Unstructured `json:"pullRequest,omitempty"`
	Matrix                  *unstructured.Unstructured `json:"matrix,omitempty"`
	Merge                   *unstructured.Unstructured `json:"merge,omitempty"`
	Plugin                  *unstructured.Unstructured `json:"plugin,omitempty"`
}

// ApplicationSetTemplate carries only the metadata.name field used for the
// child-Application name pattern. Per the allow-list we surface
// template_metadata_name only.
type ApplicationSetTemplate struct {
	Metadata ApplicationSetTemplateMeta `json:"metadata,omitempty"`
}

// ApplicationSetTemplateMeta is the read subset of template.metadata.
type ApplicationSetTemplateMeta struct {
	Name string `json:"name,omitempty"`
}

// ApplicationSetStatus is the read subset of status. ArgoCD added
// status.applicationStatus[] in v2.7+; older installations won't populate
// it. We accept missing as missing edges, never panic.
type ApplicationSetStatus struct {
	ApplicationStatus []ApplicationSetApplicationStatus `json:"applicationStatus,omitempty"`
}

// ApplicationSetApplicationStatus is one entry of
// status.applicationStatus[]. The .Application field carries the child
// Application's name.
type ApplicationSetApplicationStatus struct {
	Application string `json:"application,omitempty"`
}

// ───────────────────── DeepCopy / runtime.Object stubs ─────────────────────
//
// controller-runtime's typed cache requires that watched types implement
// runtime.Object. We only watch ArgoCD CRDs via unstructured.Unstructured,
// which already implements runtime.Object — but if a future call site decodes
// a typed Application/ApplicationSet directly via the client, these stubs
// keep that path compiling.

// DeepCopyObject implements runtime.Object.
func (a *Application) DeepCopyObject() runtime.Object {
	if a == nil {
		return nil
	}
	out := *a
	return &out
}

// DeepCopyObject implements runtime.Object.
func (a *ApplicationSet) DeepCopyObject() runtime.Object {
	if a == nil {
		return nil
	}
	out := *a
	return &out
}

// ActiveGeneratorNames returns the lowercase string names of the generators
// that are populated on this ApplicationSet, in a stable canonical order.
// The result is what the mapper carries in metadata["generators"] (joined
// with ","). Per the allow-list we emit *only* the type names, never any
// generator content.
func (g ApplicationSetGenerator) ActiveGeneratorNames() []string {
	var out []string
	if g.List != nil {
		out = append(out, "list")
	}
	if g.Clusters != nil {
		out = append(out, "clusters")
	}
	if g.Git != nil {
		out = append(out, "git")
	}
	if g.SCMProvider != nil {
		out = append(out, "scmProvider")
	}
	if g.ClusterDecisionResource != nil {
		out = append(out, "clusterDecisionResource")
	}
	if g.PullRequest != nil {
		out = append(out, "pullRequest")
	}
	if g.Matrix != nil {
		out = append(out, "matrix")
	}
	if g.Merge != nil {
		out = append(out, "merge")
	}
	if g.Plugin != nil {
		out = append(out, "plugin")
	}
	return out
}
