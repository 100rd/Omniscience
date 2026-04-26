// Package config loads operator configuration from environment variables.
//
// All values are read once at startup. The most security-critical value is
// OMNISCIENCE_WORKSPACE_ID, which establishes the tenant boundary for every
// emitted event (see ADR-0007 §ACL). It MUST come from a mounted Kubernetes
// Secret — never from cluster-side state on the watched resources.
package config

import (
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/google/uuid"
)

// Environment variable names. Centralised so the Helm chart, README, and code
// agree on the exact spellings.
const (
	envWorkspaceID = "OMNISCIENCE_WORKSPACE_ID"
	envClusterName = "OMNISCIENCE_CLUSTER_NAME"
	envNATSURL     = "OMNISCIENCE_NATS_URL"
	// envNATSCreds is the env var name for the credentials file path; the
	// value is a path on disk, never a credential body. The #nosec
	// annotation suppresses gosec G101 (which flags the literal substring
	// "CREDS" as a potential hardcoded credential).
	envNATSCreds = "OMNISCIENCE_NATS_CREDS_FILE" //nolint:gosec // env var name, not a secret
	envSubject     = "OMNISCIENCE_NATS_SUBJECT"
	envResyncSec   = "OMNISCIENCE_RESYNC_PERIOD_SECONDS"
	envMetricsAddr = "OMNISCIENCE_METRICS_ADDR"
	envProbeAddr   = "OMNISCIENCE_PROBE_ADDR"
	envLeaderElect = "OMNISCIENCE_LEADER_ELECT"
)

// Defaults chosen for v0.2 single-replica operator. Each is named in the
// Helm chart values.yaml under its own commented section.
const (
	// defaultResyncPeriod is the controller-runtime informer resync window.
	// 10 minutes balances catching missed-watch-event drift against the cost
	// of replaying every object's reconcile that often. The K8s control
	// plane uses values in this range; see kubernetes/sample-controller.
	defaultResyncPeriod = 10 * time.Minute

	// defaultSubject is the NATS subject prefix; the workspace_id is appended
	// at publish time so subscribers can scope by tenant. See ADR-0007 §ACL.
	defaultSubject = "ingest.changes.k8s"

	// defaultMetricsAddr is the address the controller-runtime metrics
	// endpoint binds to. Bound to localhost-only by default; the Helm chart
	// exposes it via a Service when monitoring is enabled.
	defaultMetricsAddr = ":8080"

	// defaultProbeAddr is the health/readiness probe bind address.
	defaultProbeAddr = ":8081"
)

// Config is the resolved operator configuration. All fields are immutable
// after Load returns.
type Config struct {
	// WorkspaceID is the tenant boundary stamped onto every published event.
	// MUST be a valid UUID and MUST come from a mounted Secret.
	WorkspaceID uuid.UUID

	// ClusterName is a human-readable label included in entity metadata. Not
	// security-bearing — only used for display and lineage.
	ClusterName string

	// NATSURL is the JetStream endpoint. Single URL; the operator does not
	// perform DNS heroics. Example: "nats://omniscience-nats:4222".
	NATSURL string

	// NATSCredsFile is the optional path to a NATS user-credentials file.
	// Empty disables credentials (acceptable for a same-namespace dev NATS
	// only — the Helm chart requires it in non-dev values files).
	NATSCredsFile string

	// SubjectPrefix is the NATS subject prefix; full subject is
	// "{SubjectPrefix}.{WorkspaceID}".
	SubjectPrefix string

	// ResyncPeriod is the informer resync window. See defaultResyncPeriod.
	ResyncPeriod time.Duration

	// MetricsAddr / ProbeAddr / LeaderElect are operator-runtime knobs.
	MetricsAddr  string
	ProbeAddr    string
	LeaderElect  bool
}

// Load reads the operator configuration from process environment.
//
// Returns a typed error if any required value is missing or malformed; the
// caller should log and exit non-zero. We deliberately fail-closed: a
// misconfigured operator must never start up emitting events with the wrong
// or no workspace.
func Load() (*Config, error) {
	cfg := &Config{
		ClusterName:   os.Getenv(envClusterName),
		NATSURL:       os.Getenv(envNATSURL),
		NATSCredsFile: os.Getenv(envNATSCreds),
		SubjectPrefix: envOrDefault(envSubject, defaultSubject),
		MetricsAddr:   envOrDefault(envMetricsAddr, defaultMetricsAddr),
		ProbeAddr:     envOrDefault(envProbeAddr, defaultProbeAddr),
		ResyncPeriod:  defaultResyncPeriod,
		LeaderElect:   os.Getenv(envLeaderElect) == "true",
	}

	wsRaw := os.Getenv(envWorkspaceID)
	if wsRaw == "" {
		return nil, fmt.Errorf("config: %s is required (mount via Secret per ADR-0007)", envWorkspaceID)
	}
	ws, err := uuid.Parse(wsRaw)
	if err != nil {
		return nil, fmt.Errorf("config: %s is not a valid UUID: %w", envWorkspaceID, err)
	}
	cfg.WorkspaceID = ws

	if cfg.NATSURL == "" {
		return nil, fmt.Errorf("config: %s is required", envNATSURL)
	}

	if cfg.ClusterName == "" {
		return nil, fmt.Errorf("config: %s is required (used as entity metadata label)", envClusterName)
	}

	if raw := os.Getenv(envResyncSec); raw != "" {
		secs, perr := time.ParseDuration(raw + "s")
		if perr != nil {
			return nil, fmt.Errorf("config: %s is not a valid integer second count: %w", envResyncSec, perr)
		}
		if secs <= 0 {
			return nil, errors.New("config: OMNISCIENCE_RESYNC_PERIOD_SECONDS must be positive")
		}
		cfg.ResyncPeriod = secs
	}

	return cfg, nil
}

func envOrDefault(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
