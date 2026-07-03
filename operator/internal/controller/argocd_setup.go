// argocd_setup.go: discovery-gated registration helper for the ArgoCD CRD
// watchers.
//
// SetupArgoCDWatchers is called from cmd/manager/main.go AFTER the manager
// is wired but BEFORE mgr.Start. It performs the discovery probe (issue #160
// §A — fail-open) and registers controllers only for CRDs that are present.
// When NEITHER CRD is present we log "argocd.crd.absent" and return nil
// without touching the manager.
package controller

import (
	"fmt"

	"github.com/go-logr/logr"
	"github.com/google/uuid"
	ctrl "sigs.k8s.io/controller-runtime"

	"github.com/100rd/Omniscience/operator/internal/argocd"
	"github.com/100rd/Omniscience/operator/internal/publisher"
)

// SetupArgoCDWatchers conditionally registers the Application and
// ApplicationSet controllers based on CRD presence. Returns nil on success
// (including the all-absent path); returns an error only when the discovery
// probe itself fails with a non-NotFound error or when controller registration
// of a present CRD fails.
//
// presence is the result of the discovery probe; passing it in (rather than
// running the probe inside this function) keeps the function pure-wiring
// for tests.
func SetupArgoCDWatchers(
	mgr ctrl.Manager,
	logger logr.Logger,
	presence argocd.PresenceCheck,
	pub publisher.Publisher,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
) error {
	if !presence.AnyPresent() {
		// Fail-open: ArgoCD is not installed. Log once and return.
		argocd.LogAbsence(logger, argocd.KindApplication)
		argocd.LogAbsence(logger, argocd.KindApplicationSet)
		return nil
	}

	if presence.ApplicationsPresent {
		appRec, err := NewArgoCDApplicationReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName)
		if err != nil {
			return fmt.Errorf("argocd application reconciler init: %w", err)
		}
		if err := appRec.SetupWithManager(mgr); err != nil {
			return fmt.Errorf("argocd application reconciler setup: %w", err)
		}
		logger.Info("argocd.crd.present", "kind", argocd.KindApplication)
	} else {
		argocd.LogAbsence(logger, argocd.KindApplication)
	}

	if presence.ApplicationSetsPresent {
		setRec, err := NewArgoCDApplicationSetReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName)
		if err != nil {
			return fmt.Errorf("argocd applicationset reconciler init: %w", err)
		}
		if err := setRec.SetupWithManager(mgr); err != nil {
			return fmt.Errorf("argocd applicationset reconciler setup: %w", err)
		}
		logger.Info("argocd.crd.present", "kind", argocd.KindApplicationSet)
	} else {
		argocd.LogAbsence(logger, argocd.KindApplicationSet)
	}

	return nil
}
