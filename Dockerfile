# ── Builder stage ────────────────────────────────────────────────────────────
# build-essential is required for tree-sitter native extensions.
# All build tooling stays in this layer — the runtime image inherits nothing.
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency manifests first to exploit Docker layer caching.
# Changes to source files will not bust the install cache.
COPY pyproject.toml uv.lock* ./
COPY packages/core/pyproject.toml        packages/core/pyproject.toml
COPY packages/connectors/pyproject.toml  packages/connectors/pyproject.toml
COPY packages/parsers/pyproject.toml     packages/parsers/pyproject.toml
COPY packages/embeddings/pyproject.toml  packages/embeddings/pyproject.toml
COPY packages/index/pyproject.toml       packages/index/pyproject.toml
COPY packages/retrieval/pyproject.toml   packages/retrieval/pyproject.toml
COPY apps/server/pyproject.toml          apps/server/pyproject.toml
COPY apps/cli/pyproject.toml             apps/cli/pyproject.toml

# Install dependencies (frozen lock, no dev extras).
RUN uv sync --frozen --no-dev

# Copy source after manifests so source changes don't bust the install cache.
COPY packages/ packages/
COPY apps/     apps/

# Re-sync to install workspace packages now that source is available.
RUN uv sync --frozen --no-dev

# task-sp-95-management-readonly-local-runtime (ADR-0023 OML-4): the local
# owner fragment runs embeddings in-process from a pinned local model and makes
# no provider/model API call. That path needs the optional `local` extra
# (sentence-transformers + torch CPU). It is OFF by default so the base image
# (Ollama/provider modes) stays lean; the management-readonly-local source
# override builds with `--build-arg INSTALL_LOCAL_EMBEDDINGS=true`.
ARG INSTALL_LOCAL_EMBEDDINGS=false
RUN if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
        uv sync --frozen --no-dev --extra local; \
    fi


# ── Runtime stage ─────────────────────────────────────────────────────────────
# Minimal image: no build tools, runs as a non-root user.
# curl is included for the container healthcheck.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Create a dedicated non-root user.
RUN groupadd --system omniscience \
    && useradd --system --gid omniscience --no-create-home omniscience

WORKDIR /app

COPY --from=builder /app/.venv    /app/.venv
COPY --from=builder /app/apps     /app/apps
COPY --from=builder /app/packages /app/packages

# task-sp-86-management-readonly-release: contracts/ is copied read-only into
# the runtime image (not the builder stage -- it carries no Python package to
# install) so /ready (apps/server/.../routes/health.py) can check the
# release's own MCP/management-context/PW0 contract closure without a
# network call. This is data, not a package: no `uv sync` step touches it.
COPY contracts/ /app/contracts/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# task-sp-95-management-readonly-local-runtime (ADR-0023): identify this image
# as the Omniscience owner artifact for the management-readonly-local profile.
# The one-shot `omniscience-migrate` service reuses this exact image and only
# overrides the command to run `alembic -c /app/packages/core/alembic.ini
# upgrade head` (the alembic entrypoint is installed on PATH in the venv);
# nothing about the runtime layout below changes between the api and migrate
# services, so both share one build.
LABEL org.opencontainers.image.title="omniscience" \
      org.opencontainers.image.description="Omniscience owner service for management-readonly-local-v1" \
      io.omniscience.profile="management-readonly-local-v1"

# task-sp-95-management-readonly-local-runtime (ADR-0023 OML-4): when the image
# is built with local embeddings, pre-bake the pinned model into the image so
# the running container needs NO network for embedding -- the fragment then
# runs with HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE and any accidental egress fails
# loudly rather than silently reaching a model host. The cache is owned by the
# non-root runtime user. This step is a no-op for the default (provider) image.
ARG INSTALL_LOCAL_EMBEDDINGS=false
ENV HF_HOME=/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/huggingface \
    LOCAL_EMBEDDING_MODEL=all-MiniLM-L6-v2
RUN if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
        mkdir -p /app/.cache/huggingface && \
        python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" && \
        chown -R omniscience:omniscience /app/.cache; \
    fi

USER omniscience

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=10s \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "omniscience_server"]
