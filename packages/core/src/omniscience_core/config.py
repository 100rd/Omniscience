"""Application-wide settings loaded from environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for all Omniscience services.

    Values are read from environment variables (case-insensitive).
    A .env file in the working directory is also picked up automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    database_url: str = Field(
        default="postgresql+asyncpg://omniscience:omniscience@localhost:5432/omniscience",
        description="Async SQLAlchemy connection URL for PostgreSQL.",
    )

    # --- NATS ---
    nats_url: str = Field(
        default="nats://localhost:4222",
        description="NATS server URL for JetStream messaging.",
    )

    # --- Embeddings ---
    embedding_provider: str = Field(
        default="ollama",
        description=(
            "Embedding backend: 'ollama', 'openai', 'voyage', 'cohere', or 'local'. "
            "Use 'local' for fully air-gapped deployments (requires sentence-transformers)."
        ),
    )
    ollama_url: str = Field(
        default="http://localhost:11434",
        description="Base URL for the Ollama API (used when embedding_provider='ollama').",
    )
    voyage_api_key: str | None = Field(
        default=None,
        description=(
            "Voyage AI API key (used when embedding_provider='voyage'). "
            "Falls back to the VOYAGE_API_KEY environment variable when None."
        ),
    )
    cohere_api_key: str | None = Field(
        default=None,
        description=(
            "Cohere API key (used when embedding_provider='cohere'). "
            "Falls back to the COHERE_API_KEY environment variable when None."
        ),
    )

    # --- Local / Air-gapped Embeddings ---
    local_model_name: str = Field(
        default="all-MiniLM-L6-v2",
        description=(
            "sentence-transformers model ID or local directory path used when "
            "embedding_provider='local'.  The model must be pre-downloaded for "
            "truly air-gapped deployments."
        ),
    )
    local_model_device: str = Field(
        default="cpu",
        description=(
            "Torch device for the local embedding model: 'cpu', 'cuda', or 'mps'. "
            "Defaults to 'cpu' for maximum portability in air-gapped environments."
        ),
    )

    # --- Query Rewriting ---
    query_rewriting_enabled: bool = Field(
        default=False,
        description=(
            "When True, search queries are rewritten and expanded before retrieval "
            "to improve recall.  Uses heuristic expansion in v0.4 MVP; a local LLM "
            "can be plugged in via QueryRewriter(model_path=...) in later versions."
        ),
    )

    # --- Re-ranker ---
    reranker_enabled: bool = Field(
        default=False,
        description=(
            "When True, a cross-encoder re-ranker scores candidate chunks after "
            "initial retrieval and re-orders them before the final top-k slice."
        ),
    )
    reranker_model: str = Field(
        default="nomic-embed-text",
        description="Ollama model used by OllamaReranker for embedding-based scoring.",
    )

    # --- Storage backends (Epic #96 — Neo4j + Qdrant) ---
    storage_vector_backend: str = Field(
        default="qdrant",
        description=(
            "Vector-store backend. Only 'qdrant' is supported as of v0.2 "
            "(pgvector was removed at the #105 cutover). The field is kept "
            "for forward compatibility should additional backends land; any "
            "other value is rejected at startup. See ADR-0006."
        ),
    )
    qdrant_host: str = Field(
        default="localhost",
        description="Qdrant server host (used when storage_vector_backend='qdrant').",
    )
    qdrant_grpc_port: int = Field(
        default=6334,
        ge=1,
        le=65535,
        description="Qdrant gRPC port (ADR-0006 §Transport — primary transport).",
    )
    qdrant_http_port: int = Field(
        default=6333,
        ge=1,
        le=65535,
        description="Qdrant HTTP port (ADR-0006 §Transport — fallback transport).",
    )
    qdrant_api_key: str | None = Field(
        default=None,
        description=(
            "Qdrant API key. Required in every non-dev environment per "
            "ADR-0006 §Deployment posture. Read from env QDRANT_API_KEY."
        ),
    )
    qdrant_https: bool = Field(
        default=False,
        description="Enable TLS on Qdrant client connections (required in non-dev).",
    )
    qdrant_prefer_grpc: bool = Field(
        default=True,
        description=(
            "Prefer gRPC over HTTP when talking to Qdrant. gRPC is the "
            "primary transport per ADR-0006; HTTP is the fallback."
        ),
    )
    qdrant_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Per-RPC timeout for Qdrant operations.",
    )

    # --- Federation ---
    federation_enabled: bool = Field(
        default=False,
        description=(
            "When True, search queries are fanned out to all enabled remote "
            "Omniscience instances listed in ``federation_instances``, and "
            "results are merged before being returned to the caller."
        ),
    )
    federation_instances: str = Field(
        default="",
        description=(
            "JSON array of remote Omniscience instance descriptors.  Each "
            "element must be an object with keys ``name`` (str), ``url`` (str), "
            "``token`` (str), and optionally ``enabled`` (bool, default true) "
            "and ``priority`` (int, default 0).  Example: "
            '[{"name": "eu-cluster", "url": "https://eu.example.com", '
            '"token": "tok_abc123"}]'
        ),
    )
    federation_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        description="Per-remote HTTP timeout (seconds) used during federated search fan-out.",
    )

    # --- Observability ---
    log_level: str = Field(
        default="INFO",
        description="Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    )
    otlp_endpoint: str | None = Field(
        default=None,
        description=(
            "OTLP exporter endpoint (e.g. http://otel-collector:4317). "
            "When None, telemetry is a no-op."
        ),
    )

    # --- Application identity ---
    app_name: str = Field(default="omniscience", description="Service name reported in telemetry.")
    app_version: str = Field(default="0.2.0", description="Service version reported in telemetry.")
    environment: str = Field(
        default="development",
        description="Deployment environment: development, staging, production.",
    )

    # --- Storage backend selection (Epic #96 — Neo4j + Qdrant) ---
    storage_graph_backend: str = Field(
        default="neo4j",
        description=(
            "Graph-store backend. Only 'neo4j' is supported as of v0.2 "
            "(pgvector was removed at the #105 cutover). The field is kept "
            "for forward compatibility should additional backends land; any "
            "other value is rejected at startup. See ADR-0005."
        ),
    )

    # --- Neo4j (graph store, issue #104) ---
    neo4j_uri: str = Field(
        default="bolt://localhost:7687",
        description="Bolt URI for the Neo4j graph store.",
    )
    neo4j_username: str = Field(
        default="neo4j",
        description="Neo4j auth username.",
    )
    neo4j_password: str = Field(
        default="neo4j_dev",
        description=(
            "Neo4j auth password. MUST be overridden in non-dev "
            "environments via Kubernetes Secret or equivalent."
        ),
    )
    neo4j_database: str = Field(
        default="neo4j",
        description="Neo4j database name. Community Edition uses the default 'neo4j'.",
    )
    neo4j_max_pool_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum size of the Neo4j connection pool.",
    )
    neo4j_acquisition_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Seconds to wait for a pooled connection before failing.",
    )
    neo4j_max_retry_time_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Upper bound on managed-transaction retry time.",
    )
    neo4j_default_max_depth: int = Field(
        default=3,
        ge=1,
        le=6,
        description=(
            "Default BFS depth cap for find_related traversals when the "
            "caller does not supply one. Hard ceiling is 6."
        ),
    )

    # --- Bitemporal schema rollout (ADR-0008 §8, issues #130-#139, #317) ---
    graph_bitemporal: Literal["enabled", "disabled"] = Field(
        default="enabled",
        description=(
            "Bitemporal write-path rollout flag for the Neo4j graph. "
            "Default 'enabled' (flipped in issue #317): the bitemporal "
            "write path is the canonical behaviour — removals end-date "
            "instead of hard-deleting and `as_of` reads traverse the "
            "`:EntityState` version chain (ADR-0008 §8, fully implemented "
            "across #130-#139). Set 'disabled' to restore PR #104's "
            "legacy hard-delete writer verbatim; existing flag-off "
            "deployments should run the operator-driven backfill "
            "(`Neo4jGraphStore.backfill_bitemporal`) on their dataset "
            "before flipping on so legacy rows carry the bitemporal triple."
        ),
    )

    # --- Retention worker (ADR-0009 §3 / §10, issue #135) ---
    #
    # Tier boundaries are global-only in v1 per ADR-0009 §10.  Per-tenant
    # overrides are explicitly out of scope for this PR; the migration path
    # (push the override into the workspaces table) is documented for the
    # first customer ask.  Helm values render these as env vars; see
    # ``helm/omniscience/values.yaml`` ``retention:`` block.
    retention_hot_days: int = Field(
        default=90,
        ge=1,
        description=(
            "Hot tier retention boundary in days (ADR-0009 §1 / §10). "
            "EntityState rows + edges + chunks with `recorded_at < now - hot_days` "
            "are evicted from hot to warm. Default 90 days per Vision §5.3."
        ),
    )
    retention_warm_days: int = Field(
        default=365,
        ge=1,
        description=(
            "Warm tier retention boundary in days (ADR-0009 §1 / §10). "
            "EntitySnapshot:Daily rows with `snapshot_date < now - warm_days` "
            "are evicted from warm to archive (Parquet on object storage). "
            "Default 365 days (cumulative — not warm window length) per Vision §5.3."
        ),
    )
    retention_archive_years: int = Field(
        default=7,
        ge=0,
        description=(
            "Archive retention horizon in years (ADR-0009 §1). Beyond this, "
            "snapshots are deleted by lifecycle rule on the bucket — the "
            "worker does NOT enforce archive expiry; that is the bucket's "
            "concern. Default 7 years per ADR-0009 §1."
        ),
    )
    retention_tick_seconds: int = Field(
        default=21600,  # 6 * 3600 — see RETENTION_TICK_SECONDS_DEFAULT
        ge=60,
        description=(
            "Retention worker tick interval in wall-clock seconds (ADR-0009 §3). "
            "Default 21600 (6 hours). Lower values increase Neo4j write pressure "
            "without improving SLO compliance materially under the 24h lag SLO."
        ),
    )
    retention_batch_size: int = Field(
        default=500,
        ge=1,
        le=10000,
        description=(
            "Maximum rows touched per Neo4j transaction during eviction "
            "(ADR-0009 §3 read-then-mark-then-move). Keeps individual "
            "transactions well under Neo4j's default 30s tx timeout."
        ),
    )
    retention_dry_run: bool = Field(
        default=False,
        description=(
            "Dry-run mode for the retention worker (ADR-0009 §3). When True, "
            "the worker reports eligible counts and a sampled record set "
            "via the admin REST endpoint and structured logs but mutates "
            "neither Neo4j nor Qdrant. Required for ops review before the "
            "first live activation per deployment."
        ),
    )
    retention_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the retention worker. Defaults to True so a "
            "stock deployment runs with the ADR-0009 tiering posture; set "
            "False only for environments where retention is intentionally "
            "deferred (e.g. CI fixtures, single-shot integration tests)."
        ),
    )
    retention_archive_bucket: str | None = Field(
        default=None,
        description=(
            "S3-compatible bucket name for archive parquet writes "
            "(ADR-0009 §7 storage layout). REQUIRED in non-dev: when None, "
            "warm-to-archive transitions are skipped (warm rows remain "
            "queryable indefinitely until configured). Per-workspace "
            "prefix is enforced by the worker."
        ),
    )
    retention_archive_kms_key_arn: str | None = Field(
        default=None,
        description=(
            "ARN of the KMS key used to encrypt archive parquet objects "
            "(ADR-0009 §1 — KMS-managed keys mandatory in non-dev). "
            "When None, the worker uses SSE-S3 (AES256), which is "
            "acceptable in dev only."
        ),
    )
    retention_archive_s3_endpoint_url: str | None = Field(
        default=None,
        description=(
            "Custom endpoint URL for S3-compatible object storage "
            "(MinIO, Ceph, etc). When None, AWS S3 is assumed. "
            "Read by boto3's S3 client."
        ),
    )
    retention_archive_s3_region: str = Field(
        default="us-east-1",
        description="AWS region used for the archive bucket S3 client.",
    )
    retention_sample_size: int = Field(
        default=20,
        ge=0,
        le=1000,
        description=(
            "Number of eligible rows to include in the dry-run report sample. "
            "Bounded to keep the report payload small."
        ),
    )

    # --- Ingestion dedup (issue #164) ---
    #
    # Server-side dedup of operator vs k8s-agentic emitters.  Both env
    # vars are read directly by the ingestion worker; the Settings
    # mirror is provided for documentation symmetry and CLI / admin UI
    # introspection.  See apps/server/src/omniscience_server/ingestion/
    # dedup.py for the full state-machine semantics.
    ingestion_dedup_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the ingestion dedup gate. When False, every "
            "k8s event passes through unchanged (the v0.2 fallback during "
            "the parallel-deprecation window of epic #98). Honoured under "
            "the env var OMNISCIENCE_DEDUP_ENABLED."
        ),
    )
    ingestion_dedup_ttl_hours: float = Field(
        default=24.0,
        description=(
            "Authority TTL in hours. Agentic events for an "
            "operator-authoritative (workspace_id, external_id) pair are "
            "dropped if the operator emitted within this many hours; "
            "outside the window, agentic reclaims authority. 0 disables "
            "TTL re-assignment (operator authority is sticky forever); "
            "-1 disables the dedup gate entirely (debug only). Honoured "
            "under the env var OMNISCIENCE_INGEST_DEDUP_TTL_HOURS."
        ),
    )

    # --- Scheduler ---
    scheduler_enabled: bool = Field(
        default=True,
        description=(
            "When True, the scheduler worker runs in the background and automatically "
            "triggers re-syncs for sources whose data has grown stale relative to their "
            "freshness_sla_seconds budget (or per-type default TTLs when no SLA is set)."
        ),
    )
    scheduler_interval_seconds: int = Field(
        default=300,
        ge=1,
        description=(
            "How often (in seconds) the scheduler worker checks all sources for staleness "
            "and publishes re-sync triggers. Defaults to 300 (5 minutes)."
        ),
    )
