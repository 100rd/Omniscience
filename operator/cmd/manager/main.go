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

	"github.com/google/uuid"
	networkingv1 "k8s.io/api/networking/v1"
	storagev1 "k8s.io/api/storage/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
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

	rec, err := controller.NewPodReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
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
	depRec, err := controller.NewDeploymentReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("deployment reconciler init: %w", err)
	}
	if err := depRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("deployment reconciler setup: %w", err)
	}

	rsRec, err := controller.NewReplicaSetReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("replicaset reconciler init: %w", err)
	}
	if err := rsRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("replicaset reconciler setup: %w", err)
	}

	ssRec, err := controller.NewStatefulSetReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("statefulset reconciler init: %w", err)
	}
	if err := ssRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("statefulset reconciler setup: %w", err)
	}

	dsRec, err := controller.NewDaemonSetReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("daemonset reconciler init: %w", err)
	}
	if err := dsRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("daemonset reconciler setup: %w", err)
	}

	jobRec, err := controller.NewJobReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
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
	if err := setupNetworkingAndConfigWatchers(mgr, pub, cfg.WorkspaceID, cfg.ClusterName); err != nil {
		return err
	}
	// ── end #158 ──────────────────────────────────────────────────────────

	// --- BEGIN issue #159: cluster-scoped watcher registration ---
	// Register the four cluster-scoped reconcilers (Node, Namespace,
	// PersistentVolume, StorageClass). Each follows the same constructor /
	// SetupWithManager shape as the Pod reconciler above; failure on any of
	// them is a hard startup error so the operator never runs partial.
	nodeRec, err := controller.NewNodeReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("node reconciler init: %w", err)
	}
	if err := nodeRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("node reconciler setup: %w", err)
	}

	nsRec, err := controller.NewNamespaceReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("namespace reconciler init: %w", err)
	}
	if err := nsRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("namespace reconciler setup: %w", err)
	}

	pvRec, err := controller.NewPersistentVolumeReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
	if err != nil {
		return fmt.Errorf("persistentvolume reconciler init: %w", err)
	}
	if err := pvRec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("persistentvolume reconciler setup: %w", err)
	}

	scRec, err := controller.NewStorageClassReconciler(mgr.GetClient(), pub, cfg.WorkspaceID, cfg.ClusterName)
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
	} else if err := controller.SetupArgoCDWatchers(mgr, logger, argoPresence, pub, cfg.WorkspaceID, cfg.ClusterName); err != nil {
		return fmt.Errorf("argocd watchers setup: %w", err)
	}
	// ─── END issue #160 ───────────────────────────────────────────────────

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
	anchorPub, err := controller.NewClusterAnchorPublisher(pub, cfg.WorkspaceID, cfg.ClusterName, "", "")
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
	clusterName string,
) error {
	if svc, err := controller.NewServiceReconciler(mgr.GetClient(), pub, workspaceID, clusterName); err != nil {
		return fmt.Errorf("service reconciler init: %w", err)
	} else if err := svc.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("service reconciler setup: %w", err)
	}
	if ep, err := controller.NewEndpointsReconciler(mgr.GetClient(), pub, workspaceID, clusterName); err != nil {
		return fmt.Errorf("endpoints reconciler init: %w", err)
	} else if err := ep.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("endpoints reconciler setup: %w", err)
	}
	if ing, err := controller.NewIngressReconciler(mgr.GetClient(), pub, workspaceID, clusterName); err != nil {
		return fmt.Errorf("ingress reconciler init: %w", err)
	} else if err := ing.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("ingress reconciler setup: %w", err)
	}
	if np, err := controller.NewNetworkPolicyReconciler(mgr.GetClient(), pub, workspaceID, clusterName); err != nil {
		return fmt.Errorf("networkpolicy reconciler init: %w", err)
	} else if err := np.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("networkpolicy reconciler setup: %w", err)
	}
	if cm, err := controller.NewConfigMapReconciler(mgr.GetClient(), pub, workspaceID, clusterName); err != nil {
		return fmt.Errorf("configmap reconciler init: %w", err)
	} else if err := cm.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("configmap reconciler setup: %w", err)
	}
	if sec, err := controller.NewSecretReconciler(mgr.GetClient(), pub, workspaceID, clusterName); err != nil {
		return fmt.Errorf("secret reconciler init: %w", err)
	} else if err := sec.SetupWithManager(mgr); err != nil {
		return fmt.Errorf("secret reconciler setup: %w", err)
	}
	return nil
}

// ── end #158 ────────────────────────────────────────────────────────────────
