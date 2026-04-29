// Command manager is the entry point for the omniscience-operator.
//
// It wires controller-runtime's manager, the NATS publisher, and the Pod
// watcher (see ADR-0007). All configuration comes from environment
// variables — see internal/config.
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/google/uuid"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

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

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		return fmt.Errorf("add healthz: %w", err)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		return fmt.Errorf("add readyz: %w", err)
	}

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
