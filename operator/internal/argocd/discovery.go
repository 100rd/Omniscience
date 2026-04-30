// CRD discovery — fail-open opt-in for ArgoCD.
//
// The operator MUST start cleanly when ArgoCD is not installed (issue #160
// §A). At startup we ask the API server whether the ArgoCD CRDs are present;
// if absent we log INFO `argocd.crd.absent` and skip controller registration.
// We never fail-close on this check.
package argocd

import (
	"errors"
	"fmt"

	"github.com/go-logr/logr"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/rest"
)

// AbsentLogKey is the log message emitted when an ArgoCD CRD is not present
// on the API server. Pinned as a constant so kubectl-grep-able operations
// (and tests) can match it byte-for-byte.
const AbsentLogKey = "argocd.crd.absent"

// PresenceCheck reports whether the two ArgoCD CRDs the operator covers
// are present on the API server.
type PresenceCheck struct {
	ApplicationsPresent    bool
	ApplicationSetsPresent bool
}

// AnyPresent returns true if either CRD is on the server.
func (p PresenceCheck) AnyPresent() bool {
	return p.ApplicationsPresent || p.ApplicationSetsPresent
}

// DiscoverArgoCD probes the API server for the ArgoCD v1alpha1 group/version
// and reports per-resource presence. Designed for the hot startup path:
//   - A single GET against /apis/argoproj.io/v1alpha1.
//   - A "not found" response for the group is the expected absent path —
//     we surface presence=false, NOT an error.
//   - A network error or unexpected API error is returned to the caller so
//     a misconfigured cluster is loud, not silently absent.
//
// The opinionated decision (per issue): NotFound on the API group ⇒ absent,
// not an operational error.
func DiscoverArgoCD(client discovery.DiscoveryInterface) (PresenceCheck, error) {
	if client == nil {
		return PresenceCheck{}, errors.New("argocd discovery: nil discovery client")
	}
	gv := fmt.Sprintf("%s/%s", GroupName, Version)
	list, err := client.ServerResourcesForGroupVersion(gv)
	if err != nil {
		// API server returns 404 for an unknown group/version. This is the
		// graceful "ArgoCD not installed" path.
		if apierrors.IsNotFound(err) || discovery.IsGroupDiscoveryFailedError(err) {
			return PresenceCheck{}, nil
		}
		return PresenceCheck{}, fmt.Errorf("discover %s: %w", gv, err)
	}
	if list == nil {
		return PresenceCheck{}, nil
	}
	out := PresenceCheck{}
	for _, r := range list.APIResources {
		switch r.Name {
		case ResourceApplications:
			out.ApplicationsPresent = true
		case ResourceApplicationSet:
			out.ApplicationSetsPresent = true
		}
	}
	return out, nil
}

// DiscoverFromRESTConfig is the convenience helper used from main.go: it
// builds a discovery client from the manager's rest config and probes.
func DiscoverFromRESTConfig(cfg *rest.Config) (PresenceCheck, error) {
	if cfg == nil {
		return PresenceCheck{}, errors.New("argocd discovery: nil rest config")
	}
	dc, err := discovery.NewDiscoveryClientForConfig(cfg)
	if err != nil {
		return PresenceCheck{}, fmt.Errorf("argocd discovery: build client: %w", err)
	}
	return DiscoverArgoCD(dc)
}

// LogAbsence emits the canonical INFO log line when a given CRD is absent.
// Centralised so tests can grep for the exact key string and the kubectl
// log surface stays consistent across the two CRDs.
func LogAbsence(logger logr.Logger, kind string) {
	logger.Info(AbsentLogKey, "kind", kind, "group", GroupName, "version", Version)
}
