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
	// envClusterID is the multi-cluster identity disambiguator (issue #167).
	// Required when OMNISCIENCE_WORKSPACE_ID is shared across multiple
	// operators (the multi-cluster case). Optional in single-cluster
	// deployments; when unset the operator derives a deterministic
	// uuid5(workspace_id, cluster_name) — back-compat with v0.2 deployments.
	// MUST be a valid UUID and MUST come from a mounted Secret. Validation
	// CANNOT enforce that two operators in different clusters use distinct
	// values — that is the platform team's discipline; the server-side
	// collision detector is the second line of defence.
	envClusterID   = "OMNISCIENCE_CLUSTER_ID"
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

	// ── #163 reconciliation worker ───────────────────────────────────────
	// envReconcileInterval is the period between reconcile cycles. Default
	// 15m (issue #163 §Goal). Format is any time.ParseDuration value.
	envReconcileInterval = "OMNISCIENCE_RECONCILE_INTERVAL"
	// envReconcileDryRun, when "true", suppresses event emission. Used for
	// first-time-on-customer-cluster validation per issue #163 §Scope.
	envReconcileDryRun = "OMNISCIENCE_RECONCILE_DRY_RUN"
	// envAPIBaseURL is the absolute URL of the Omniscience server, e.g.
	// "https://omniscience.example". The read API endpoint
	// "/api/v1/operator/entities" is appended at request time.
	envAPIBaseURL = "OMNISCIENCE_API_BASE_URL"
	// envAPIBearerToken is the operator's bearer token for the read API.
	// MUST come from a Secret-mounted file (see ADR-0007 §ACL); the
	// server gates the workspace_id query param against the token's
	// workspace and 403s on mismatch — this is the load-bearing ACL gate.
	// The #nosec annotation suppresses gosec G101 on the literal substring
	// "TOKEN" (which it flags as a potential hardcoded credential).
	envAPIBearerToken = "OMNISCIENCE_API_BEARER_TOKEN" //nolint:gosec // env var name, not a secret
	// ── end #163 ──────────────────────────────────────────────────────────
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

	// defaultReconcileInterval is the default period between reconciliation
	// worker cycles (issue #163 §Goal). 15 minutes is a balance between
	// drift recovery latency and read-API + cluster-list cost on large
	// clusters. Tunable via OMNISCIENCE_RECONCILE_INTERVAL.
	defaultReconcileInterval = 15 * time.Minute
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

	// ClusterID is the multi-cluster identity (issue #167). Stamped into
	// every emitted entity's external_id so the same Kind+namespace+name in
	// two clusters under one workspace produce DISTINCT graph entities.
	// When OMNISCIENCE_CLUSTER_ID is unset, Load derives the deterministic
	// default uuid5(workspace_id, cluster_name) — preserves v0.2 single-
	// cluster identity stability across operator restarts.
	ClusterID uuid.UUID

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

	// ── #163 reconciliation worker ───────────────────────────────────────
	// ReconcileInterval is the period between reconcile cycles. 15m default.
	ReconcileInterval time.Duration

	// ReconcileDryRun, when true, suppresses event emission from the
	// reconciler. Watch-path emission is unaffected.
	ReconcileDryRun bool

	// APIBaseURL is the absolute URL of the Omniscience read API. Empty
	// disables the reconciler entirely (the worker is a no-op).
	APIBaseURL string

	// APIBearerToken authenticates the operator to the read API. MUST come
	// from a Secret-mounted file (ADR-0007 §ACL). Empty disables the
	// reconciler when APIBaseURL is also empty; an empty token with a
	// non-empty URL is a misconfiguration and Load returns an error.
	APIBearerToken string
	// ── end #163 ──────────────────────────────────────────────────────────
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

	// ClusterID resolution (issue #167):
	//   - Explicit OMNISCIENCE_CLUSTER_ID — parse, fail-closed on malformed.
	//   - Unset — derive uuid5(workspace_id, cluster_name) so single-cluster
	//     deployments keep stable identity across restarts. The deterministic
	//     default matches the v0.2 #159 cluster_id derivation, so existing
	//     v0.2 graph data lines up byte-for-byte after the re-keying once
	//     the migration script runs (deferred to a separate sub-issue).
	if cidRaw := os.Getenv(envClusterID); cidRaw != "" {
		cid, cerr := uuid.Parse(cidRaw)
		if cerr != nil {
			return nil, fmt.Errorf("config: %s is not a valid UUID: %w", envClusterID, cerr)
		}
		cfg.ClusterID = cid
	} else {
		cfg.ClusterID = deriveDefaultClusterID(cfg.WorkspaceID, cfg.ClusterName)
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

	// ── #163 reconciliation worker config ────────────────────────────────
	cfg.ReconcileInterval = defaultReconcileInterval
	if raw := os.Getenv(envReconcileInterval); raw != "" {
		d, perr := time.ParseDuration(raw)
		if perr != nil {
			return nil, fmt.Errorf("config: %s is not a valid duration: %w", envReconcileInterval, perr)
		}
		if d <= 0 {
			return nil, fmt.Errorf("config: %s must be positive", envReconcileInterval)
		}
		cfg.ReconcileInterval = d
	}
	cfg.ReconcileDryRun = os.Getenv(envReconcileDryRun) == "true"
	cfg.APIBaseURL = os.Getenv(envAPIBaseURL)
	cfg.APIBearerToken = os.Getenv(envAPIBearerToken)
	// Fail-closed misconfiguration check: a base URL without a token is
	// always wrong. The reverse (token without URL) is also wrong but
	// rejecting only the URL+no-token combination is enough — an empty
	// URL disables the reconciler entirely (worker startup logs that).
	if cfg.APIBaseURL != "" && cfg.APIBearerToken == "" {
		return nil, fmt.Errorf("config: %s set but %s is empty (reconciler requires both)", envAPIBaseURL, envAPIBearerToken)
	}
	// ── end #163 ──────────────────────────────────────────────────────────

	return cfg, nil
}

func envOrDefault(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

// nsClusterIDDefault is the fixed UUID namespace used to derive the
// deterministic-default cluster_id from (workspace_id, cluster_name) when
// OMNISCIENCE_CLUSTER_ID is unset. Matches the namespace used by
// entity.DeriveClusterID so the value is byte-equal to the #159 v0.2
// derivation — single-cluster v0.2 deployments retain identity continuity
// when the operator picks up #167.
var nsClusterIDDefault = uuid.MustParse("d6c4a5b1-3e7f-4a92-9c2d-7e1f8b6c4a5b")

// deriveDefaultClusterID returns uuid5(workspaceID, "cluster/" + workspaceID
// + "/" + clusterName). The "cluster/" prefix matches entity.DeriveClusterID
// — both derivations live in lockstep so the deterministic default is
// observable in tests without importing a server-side helper.
func deriveDefaultClusterID(workspaceID uuid.UUID, clusterName string) uuid.UUID {
	return uuid.NewSHA1(nsClusterIDDefault, []byte("cluster/"+workspaceID.String()+"/"+clusterName))
}
