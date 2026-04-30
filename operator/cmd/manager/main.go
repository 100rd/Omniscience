// Command manager is the entry point for the omniscience-operator.
//
// It wires controller-runtime's manager, the NATS publisher, and the Pod
// watcher (see ADR-0007). All configuration comes from environment
// variables — see internal/config.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	rolloutsv1alpha1 "github.com/argoproj/argo-rollouts/pkg/apis/rollouts/v1alpha1"
	"github.com/google/uuid"
	networkingv1 "k8s.io/api/networking/v1"
	resourcev1beta1 "k8s.io/api/resource/v1beta1"
	storagev1 "k8s.io/api/storage/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/100rd/omniscience/operator/internal/argocd"
	"github.com/100rd/omniscience/operator/internal/config"
	"github.com/100rd/omniscience/operator/internal/controller"
	"github.com/100rd/omniscience/operator/internal/publisher"
	"github.com/100rd/omniscience/operator/internal/reconciler"
)

// scheme is the runtime scheme used by the manager's client and cache.
// clientgoscheme registers the standard core / apps / batch / etc. API
// groups in one call — sufficient for every workload kind watched in v0.2
// (Pod, Deployment, ReplicaSet, StatefulSet, DaemonSet, Job).
var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	// ── #158 networking + config watchers — register networking.k8s.io/v1 ──
	utilruntime.Must(networkingv1.AddToScheme(scheme))
	// ── end #158 ──
	// --- BEGIN issue #159: cluster-scoped watchers scheme registration ---
	// storage.k8s.io/v1 carries StorageClass; the core scheme registered
	// above already covers Node, Namespace, PersistentVolume.
	utilruntime.Must(storagev1.AddToScheme(scheme))
	// --- END issue #159 ---
}

func main() {
	if err := run(); err != nil {
		// Logger may not be wired yet on early failures; print to stderr
		// as well so kubectl logs always shows the cause.
		fmt.Fprintf(os.Stderr, "operator: fatal: %v\n", err)
		os.Exit(1)
	}
}

// run is the real entry point, separated from main so that deferred cleanup
// (publisher.Close, etc.) actually runs on error paths. main only handles
// process-level exit code.
func run() error {
	// zap-logger flags wire structured logging to stdout. controller-runtime
	// reads from this configured logger via ctrl.Log.
	opts := zap.Options{Development: false}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	logger := ctrl.Log.WithName("setup")

	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("config load: %w", err)
	}
	logger.Info("loaded config",
		"workspace_id", cfg.WorkspaceID.String(),
		"cluster", cfg.ClusterName,
		"nats_url", cfg.NATSURL,
		"subject_prefix", cfg.SubjectPrefix,
		"resync_period", cfg.ResyncPeriod.String(),
	)

	pub, err := publisher.New(cfg.NATSURL, cfg.NATSCredsFile, cfg.SubjectPrefix)
	if err != nil {
		return fmt.Errorf("nats publisher init: %w", err)
	}
	defer func() {
		if cerr := pub.Close(); cerr != nil {
			logger.Error(cerr, "nats publisher close")
		}
	}()

	resync := cfg.ResyncPeriod
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		Metrics: metricsserver.Options{
			BindAddress: cfg.MetricsAddr,
		},
		HealthProbeBindAddress: cfg.ProbeAddr,
		LeaderElection:         cfg.LeaderElect,
		LeaderElectionID:       "omniscience-operator-leader",
		Cache: cache.Options{
			SyncPeriod: &resync,
		},
	})
	if err != nil {
		return fmt.Errorf("manager init: %w", err)
	}

	rec, err := controller.NewPodReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("pod reconciler init: %w", err)
	}
	if err := rec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("pod reconciler setup: %w", err)
	}

	// ─── Workload watchers (#157) ─────────────────────────────────────────
	// Appended below the Pod controller so the existing registration block
	// is untouched. Each new reconciler is registered in dependency-natural
	// order (Deployment → ReplicaSet → StatefulSet → DaemonSet → Job) but
	// the manager itself runs them concurrently — order here only affects
	// startup logging, not runtime semantics.
	depRec, err := controller.NewDeploymentReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("deployment reconciler init: %w", err)
	}
	if err := depRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("deployment reconciler setup: %w", err)
	}

	rsRec, err := controller.NewReplicaSetReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("replicaset reconciler init: %w", err)
	}
	if err := rsRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("replicaset reconciler setup: %w", err)
	}

	ssRec, err := controller.NewStatefulSetReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("statefulset reconciler init: %w", err)
	}
	if err := ssRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("statefulset reconciler setup: %w", err)
	}

	dsRec, err := controller.NewDaemonSetReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("daemonset reconciler init: %w", err)
	}
	if err := dsRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("daemonset reconciler setup: %w", err)
	}

	jobRec, err := controller.NewJobReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("job reconciler init: %w", err)
	}
	if err := jobRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("job reconciler setup: %w", err)
	}
	// ─── End workload watchers (#157) ─────────────────────────────────────

	// ── #158 networking + config watchers ─────────────────────────────────
	// One reconciler per kind. Setup order doesn't matter — controller-
	// runtime resolves dependencies at start time. Each Setup* call returns
	// a wrapped error so a single line in kubectl logs identifies which
	// kind failed to register.
	if err := setupNetworkingAndConfigWatchers(mgr, pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName); err != nil {
		return err
	}
	// ── end #158 ──────────────────────────────────────────────────────────

	// ── #161 Argo Rollouts CRD coverage — discovery-gated registration ────
	// Discovery probe is fail-open per ADR-0007 §A and issue #161: when the
	// CRD is not installed we log INFO and skip registration. A truly
	// transient discovery failure (5xx, connection refused) is propagated so
	// startup does not silently disable the watcher on a flaky API server.
	if err := setupArgoRolloutsWatcher(mgr, pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName, logger); err != nil {
		return err
	}
	// ── end #161 ──────────────────────────────────────────────────────────

	// ── #190 DRA resource handles — discovery-gated registration ──────────
	// Probe the API server for resource.k8s.io/v1beta1; absent → log INFO
	// and skip all 4 DRA controllers. Same fail-open posture as #161.
	if err := setupDRAWatchers(mgr, pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName, logger); err != nil {
		return err
	}
	// ── end #190 ──────────────────────────────────────────────────────────

	// --- BEGIN issue #159: cluster-scoped watcher registration ---
	// Register the four cluster-scoped reconcilers (Node, Namespace,
	// PersistentVolume, StorageClass). Each follows the same constructor /
	// SetupWithManager shape as the Pod reconciler above; failure on any of
	// them is a hard startup error so the operator never runs partial.
	nodeRec, err := controller.NewNodeReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("node reconciler init: %w", err)
	}
	if err := nodeRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("node reconciler setup: %w", err)
	}

	nsRec, err := controller.NewNamespaceReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("namespace reconciler init: %w", err)
	}
	if err := nsRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("namespace reconciler setup: %w", err)
	}

	pvRec, err := controller.NewPersistentVolumeReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("persistentvolume reconciler init: %w", err)
	}
	if err := pvRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("persistentvolume reconciler setup: %w", err)
	}

	scRec, err := controller.NewStorageClassReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("storageclass reconciler init: %w", err)
	}
	if err := scRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("storageclass reconciler setup: %w", err)
	}
	// --- END issue #159 ---

	// ─── BEGIN issue #160: ArgoCD CRD coverage (discovery-gated) ──────────
	// Discovery-based opt-in: probe the API server for ArgoCD CRDs and
	// register controllers only for the ones that are present. When the
	// CRDs are absent we log INFO "argocd.crd.absent" and continue — the
	// operator MUST NOT fail-close on a missing optional CRD (issue #160 §A).
	//
	// A discovery error that is NOT "group not found" is logged but does
	// not crash the operator — degrading gracefully to "no ArgoCD watchers"
	// is preferable to wedging the entire watch set on a transient API
	// server hiccup at startup.
	argoPresence, argoErr := argocd.DiscoverFromRESTConfig(ctrl.GetConfigOrDie())
	if argoErr != nil {
		logger.Error(argoErr, "argocd discovery failed; continuing without ArgoCD watchers")
	} else if err := controller.SetupArgoCDWatchers(mgr, logger, argoPresence, pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName); err != nil {
		return fmt.Errorf("argocd watchers setup: %w", err)
	}
	// ─── END issue #160 ───────────────────────────────────────────────────

	// ── #163 reconciliation worker ────────────────────────────────────────
	// Drift detector that periodically compares cluster state against the
	// graph slice for (workspace_id, cluster_id) and emits corrective
	// events. Implements manager.LeaderElectionRunnable, so the manager
	// only ticks the worker on the leader replica when LeaderElect=true.
	//
	// Config knobs (all with safe defaults; see internal/config):
	//   OMNISCIENCE_RECONCILE_INTERVAL  — default 15m
	//   OMNISCIENCE_RECONCILE_DRY_RUN   — default false
	//   OMNISCIENCE_API_BASE_URL        — required to enable
	//   OMNISCIENCE_API_BEARER_TOKEN    — required to enable (Secret-mounted)
	//
	// Empty API URL disables the worker entirely — the rest of the
	// operator continues to start normally. This preserves the watch path
	// as the single source of truth in deployments that have not yet
	// rolled out the read API endpoint server-side.
	if cfg.APIBaseURL == "" {
		logger.Info("reconciler.disabled — OMNISCIENCE_API_BASE_URL is empty",
			"event", "reconciler.disabled",
		)
	} else {
		apiClient, ierr := reconciler.NewHTTPEntitiesClient(cfg.APIBaseURL, cfg.APIBearerToken)
		if ierr != nil {
			return fmt.Errorf("reconciler: api client init: %w", ierr)
		}
		worker, ierr := reconciler.New(reconciler.Options{
			K8sClient:   mgr.GetClient(),
			APIClient:   apiClient,
			Publisher:   pub,
			WorkspaceID: cfg.WorkspaceID,
			ClusterID:   cfg.ClusterID,
			ClusterName: cfg.ClusterName,
			Interval:    cfg.ReconcileInterval,
			DryRun:      cfg.ReconcileDryRun,
		})
		if ierr != nil {
			return fmt.Errorf("reconciler: worker init: %w", ierr)
		}
		if ierr := mgr.Add(worker); ierr != nil {
			return fmt.Errorf("reconciler: mgr.Add: %w", ierr)
		}
		logger.Info("reconciler.enabled",
			"event", "reconciler.enabled",
			"interval", cfg.ReconcileInterval.String(),
			"dry_run", cfg.ReconcileDryRun,
			"api_base_url", cfg.APIBaseURL,
		)
	}
	// ── end #163 ──────────────────────────────────────────────────────────

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		return fmt.Errorf("add healthz: %w", err)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		return fmt.Errorf("add readyz: %w", err)
	}

	// --- BEGIN issue #159: Cluster anchor entity publish at startup ---
	// The Cluster anchor is the per-cluster top-level entity; it is emitted
	// once before the manager loop starts so every subsequent watch event
	// can reference it via an IN_CLUSTER edge. Restart re-publishes the
	// same external_id and cluster_id (deterministic UUIDv5), so the
	// server-side upsert is idempotent. The kubernetes_version and
	// cluster_endpoint fields are passed empty here — they are populated
	// in a follow-up that reads from the rest config + a Node sample once
	// the manager cache is warm. The empty-string strategy keeps this
	// change small and pure-stamping.
	anchorPub, err := controller.NewClusterAnchorPublisher(pub, cfg.WorkspaceID, cfg.ClusterID, cfg.ClusterName, "", "")
	if err != nil {
		return fmt.Errorf("cluster anchor init: %w", err)
	}
	if err := anchorPub.PublishOnce(context.Background()); err != nil {
		// Cluster anchor publish failure is logged but not fatal — the
		// operator can still emit per-resource events; the consumer side
		// upserts the cluster on first IN_CLUSTER edge resolution if the
		// anchor never lands. Hard-erroring here would mean a brief NATS
		// outage at startup blocks the whole operator.
		logger.Error(err, "cluster anchor publish; continuing without anchor")
	} else {
		logger.Info("cluster anchor published",
			"cluster_name", cfg.ClusterName,
			"cluster_id", anchorPub.BuildEvent().Metadata["cluster_id"],
		)
	}
	// --- END issue #159 ---

	logger.Info("starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		return fmt.Errorf("manager runtime: %w", err)
	}
	return nil
}

// ── #158 networking + config watchers — registration helper ────────────────
//
// setupNetworkingAndConfigWatchers wires the Service / Endpoints / Ingress /
// NetworkPolicy / ConfigMap / Secret reconcilers into the manager. Kept in
// its own function so the run() body stays readable and so adding/removing a
// kind is a localised change. APPEND-only relative to the original main.go
// per the parallel-team protocol.
//
// Each constructor validates its inputs (non-nil client, non-zero workspace,
// non-empty cluster name) — those errors are wrapped with the kind so kubectl
// logs identify the offender immediately on init failure.
//
// Goroutine-safety: each reconciler is independent and shares no mutable
// state with the others. The publisher is concurrency-safe (NATS connection
// is goroutine-safe by design).
func setupNetworkingAndConfigWatchers(
	mgr ctrl.Manager,
	pub publisher.Publisher,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
) error {
	if svc, err := controller.NewServiceReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName); err != nil {
		return fmt.Errorf("service reconciler init: %w", err)
	} else if err := svc.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("service reconciler setup: %w", err)
	}
	if ep, err := controller.NewEndpointsReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName); err != nil {
		return fmt.Errorf("endpoints reconciler init: %w", err)
	} else if err := ep.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("endpoints reconciler setup: %w", err)
	}
	if ing, err := controller.NewIngressReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName); err != nil {
		return fmt.Errorf("ingress reconciler init: %w", err)
	} else if err := ing.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("ingress reconciler setup: %w", err)
	}
	if np, err := controller.NewNetworkPolicyReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName); err != nil {
		return fmt.Errorf("networkpolicy reconciler init: %w", err)
	} else if err := np.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("networkpolicy reconciler setup: %w", err)
	}
	if cm, err := controller.NewConfigMapReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName); err != nil {
		return fmt.Errorf("configmap reconciler init: %w", err)
	} else if err := cm.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("configmap reconciler setup: %w", err)
	}
	if sec, err := controller.NewSecretReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName); err != nil {
		return fmt.Errorf("secret reconciler init: %w", err)
	} else if err := sec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("secret reconciler setup: %w", err)
	}
	return nil
}

// ── end #158 ────────────────────────────────────────────────────────────────

// ── #161 Argo Rollouts CRD coverage — registration helper ──────────────────
//
// setupArgoRolloutsWatcher probes the API server for the Rollouts CRD via
// client-go's discovery interface. Three outcomes:
//
//  1. CRD present  → register the v1alpha1 type with the manager scheme and
//     wire the ArgoRolloutReconciler.
//  2. CRD absent   → INFO-log "argo_rollouts.crd.absent" and return nil. The
//     rest of the operator continues to start normally.
//  3. Discovery error (transient API-server failure) → return the error so
//     manager startup fails loudly. Silently disabling the watcher on a
//     flaky API server would leave the operator running blind without any
//     signal that progressive-delivery coverage was lost.
//
// AddToScheme is called only on the present-path so a stale go-module pin
// of argo-rollouts cannot accidentally extend the manager cache contract on
// clusters that don't have the CRD installed.
func setupArgoRolloutsWatcher(
	mgr ctrl.Manager,
	pub publisher.Publisher,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
	logger interface {
		Info(msg string, keysAndValues ...interface{})
	},
) error {
	disc, err := discovery.NewDiscoveryClientForConfig(mgr.GetConfig())
	if err != nil {
		return fmt.Errorf("argo-rollouts: discovery client init: %w", err)
	}

	present, perr := controller.IsArgoRolloutsCRDInstalled(disc)
	if perr != nil {
		return fmt.Errorf("argo-rollouts: discovery probe: %w", perr)
	}
	if !present {
		// Documented INFO log key so dashboards / log queries can detect the
		// no-Rollouts state without scraping for a free-form message.
		logger.Info(controller.ArgoRolloutsCRDAbsentLogKey+" — Argo Rollouts CRD not installed; skipping watcher registration",
			"event", controller.ArgoRolloutsCRDAbsentLogKey,
			"group_version", controller.ArgoRolloutsGroupVersion,
		)
		return nil
	}

	// CRD is present — register the typed scheme and wire the reconciler.
	if err := rolloutsv1alpha1.AddToScheme(mgr.GetScheme()); err != nil {
		return fmt.Errorf("argo-rollouts: add to scheme: %w", err)
	}

	rec, err := controller.NewArgoRolloutReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName)
	if err != nil {
		return fmt.Errorf("argo-rollouts: reconciler init: %w", err)
	}
	if err := rec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("argo-rollouts: reconciler setup: %w", err)
	}
	logger.Info("argo_rollouts.crd.present — Rollout watcher registered",
		"event", "argo_rollouts.crd.present",
		"group_version", controller.ArgoRolloutsGroupVersion,
	)
	return nil
}

// ── end #161 ───────────────────────────────────────────────────────────────

// ── #190 DRA resource handles — registration helper ───────────────────────
//
// setupDRAWatchers probes the API server for resource.k8s.io/v1beta1 via
// client-go's discovery interface. Three outcomes:
//
//  1. API present → register the v1beta1 type with the manager scheme and
//     wire all four DRA reconcilers (ResourceClaim, ResourceClaimTemplate,
//     ResourceSlice, DeviceClass).
//  2. API absent  → INFO-log "dra.api.absent" and return nil. The rest of
//     the operator continues to start normally on K8s 1.30 or older.
//  3. Discovery error → return the error so manager startup fails loudly.
//     Silently disabling the watchers on a flaky API server would leave
//     GPU-cluster operators running blind.
func setupDRAWatchers(
	mgr ctrl.Manager,
	pub publisher.Publisher,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	clusterName string,
	logger interface {
		Info(msg string, keysAndValues ...interface{})
	},
) error {
	disc, err := discovery.NewDiscoveryClientForConfig(mgr.GetConfig())
	if err != nil {
		return fmt.Errorf("dra: discovery client init: %w", err)
	}

	present, perr := controller.IsDRAAPIInstalled(disc)
	if perr != nil {
		return fmt.Errorf("dra: discovery probe: %w", perr)
	}
	if !present {
		logger.Info(controller.DRAAPIAbsentLogKey+" — DRA API not served; skipping watcher registration",
			"event", controller.DRAAPIAbsentLogKey,
			"group_version", controller.DRAGroupVersion,
		)
		return nil
	}

	// API is present — register the typed scheme and wire all 4 reconcilers.
	if err := resourcev1beta1.AddToScheme(mgr.GetScheme()); err != nil {
		return fmt.Errorf("dra: add to scheme: %w", err)
	}

	rcRec, err := controller.NewResourceClaimReconciler(mgr.GetClient(), pub, workspaceID, clusterID, clusterName)
	if err != nil {
		return fmt.Errorf("dra: resourceclaim reconciler init: %w", err)
	}
	if err := rcRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("dra: resourceclaim reconciler setup: %w", err)
	}

	rctRec, err := controller.NewResourceClaimTemplateReconciler(mgr.GetClient(), pub, workspaceID, clusterName)
	if err != nil {
		return fmt.Errorf("dra: resourceclaimtemplate reconciler init: %w", err)
	}
	if err := rctRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("dra: resourceclaimtemplate reconciler setup: %w", err)
	}

	rsRec, err := controller.NewResourceSliceReconciler(mgr.GetClient(), pub, workspaceID, clusterName)
	if err != nil {
		return fmt.Errorf("dra: resourceslice reconciler init: %w", err)
	}
	if err := rsRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("dra: resourceslice reconciler setup: %w", err)
	}

	dcRec, err := controller.NewDeviceClassReconciler(mgr.GetClient(), pub, workspaceID, clusterName)
	if err != nil {
		return fmt.Errorf("dra: deviceclass reconciler init: %w", err)
	}
	if err := dcRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("dra: deviceclass reconciler setup: %w", err)
	}

	logger.Info("dra.api.present — DRA watchers registered",
		"event", "dra.api.present",
		"group_version", controller.DRAGroupVersion,
	)
	return nil
}

// ── end #190 ──────────────────────────────────────────────────────────────
