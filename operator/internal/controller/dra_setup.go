// dra_setup.go — discovery probe for the Dynamic Resource Allocation (DRA)
// API group. DRA is GA in Kubernetes 1.31; on 1.30 or older clusters the
// API group is absent and we skip registration of all four DRA controllers
// (#190 follow-up to #162). The probe pattern mirrors
// IsArgoRolloutsCRDInstalled — fail-open on absent, propagate transient
// failures so a flaky API server at boot doesn't silently disable the
// watchers.
package controller

import (
	"errors"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/client-go/discovery"
)

// DRAGroupVersion is the API group/version this controller probes for at
// startup. Pinned as a string constant rather than imported from the
// k8s.io/api/resource module so the discovery probe never fails closed on a
// module-version mismatch — the wire-level group/version is the stable
// contract.
const DRAGroupVersion = "resource.k8s.io/v1beta1"

// DRAAPIAbsentLogKey is the structured log key emitted at INFO when the DRA
// API group is not served by the API server. Tools that grep `kubectl logs`
// for "dra.api.absent" rely on this exact spelling.
const DRAAPIAbsentLogKey = "dra.api.absent"

// IsDRAAPIInstalled returns true when the API server reports the
// resource.k8s.io/v1beta1 group with at least the resourceclaims resource
// listed. Returns false (nil error) on the documented "absent" path: the
// API server returns NotFound / NoMatchError. Returns false plus a non-nil
// error on truly transient failures so the caller can fail-loud at boot.
//
// Why this signature: the discovery probe is the only place the fail-open
// vs fail-closed policy is decided. Returning (bool, error) keeps that
// policy at the call site (cmd/manager/main.go) where it is most reviewable.
func IsDRAAPIInstalled(d discovery.DiscoveryInterface) (bool, error) {
	if d == nil {
		return false, errors.New("controller: discovery client must not be nil")
	}
	rl, err := d.ServerResourcesForGroupVersion(DRAGroupVersion)
	switch {
	case err == nil:
		// Group/version is registered. Confirm the canonical resource is
		// listed — a registered group with no resources is treated as absent
		// (the watchers would have nothing to watch).
		for _, r := range rl.APIResources {
			if r.Name == "resourceclaims" {
				return true, nil
			}
		}
		return false, nil
	case apierrors.IsNotFound(err):
		// Group/version is not registered. This is the documented
		// "DRA not installed" path — fail-open, no error.
		return false, nil
	default:
		// Transient discovery failure. Caller decides retry vs fatal.
		return false, fmt.Errorf("discovery probe %s: %w", DRAGroupVersion, err)
	}
}
