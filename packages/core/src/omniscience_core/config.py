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
        description="Embedding backend: 'ollama', 'openai', 'voyage', or 'cohere'.",
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
