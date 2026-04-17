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

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER omniscience

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=10s \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "omniscience_server"]
