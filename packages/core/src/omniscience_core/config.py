"""Application-wide settings loaded from environment variables."""

from __future__ import annotations

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

    # --- Storage backends (Epic #96 — Neo4j + Qdrant migration) ---
    storage_vector_backend: str = Field(
        default="pgvector",
        description=(
            "Vector-store backend: 'pgvector' (default) or 'qdrant'. "
            "Switches the VectorStore implementation wired into the DI "
            "container. See ADR-0006. Pgvector remains the default until "
            "the cutover (#105) completes."
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
    app_version: str = Field(default="0.1.0", description="Service version reported in telemetry.")
    environment: str = Field(
        default="development",
        description="Deployment environment: development, staging, production.",
    )

    # --- Storage backend selection (Phase 2 / epic #96) ---
    storage_graph_backend: str = Field(
        default="pgvector",
        description=(
            "Graph-store backend: 'pgvector' (default, Phase 1) or 'neo4j' "
            "(Phase 2a, issue #104). Flip to 'neo4j' after running the "
            "dual-write migration per ADR-0005."
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
