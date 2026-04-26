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

// scheme is the runtime scheme used by the manager's client and cache. Only
// core/v1 is needed in v0.2 (Pod); follow-up issues that watch additional
// API groups will register them here.
var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
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
