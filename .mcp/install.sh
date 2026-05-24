#!/usr/bin/env bash
# Omniscience one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/100rd/Omniscience/main/.mcp/install.sh | bash
#
# What it does:
#   1. Checks prerequisites (docker, docker compose).
#   2. Creates ./omniscience/ working directory if missing.
#   3. Writes a .env with generated secrets (idempotent — never overwrites).
#   4. Fetches docker-compose.prod.yml from the repo at the pinned ref.
#      The prod variant references pre-built GHCR images
#      (ghcr.io/100rd/omniscience-app, ghcr.io/100rd/omniscience-admin)
#      so no source checkout / local build is required.
#   5. Starts the stack with `docker compose up -d`.
#   6. Waits for /health to return 200 (up to 300s — first run must
#      pull ~4 images and Neo4j start_period alone is ~30s).
#   7. Prints next-step instructions: how to mint a token and run
#      `omniscience init --client <ide>`.
#
# Environment overrides:
#   OMNISCIENCE_REF       git ref of the published compose file. default: main
#   OMNISCIENCE_VERSION   GHCR image tag to pin. default: latest
#                         (set to e.g. v0.5.0 for reproducibility)
#   OMNISCIENCE_DIR       working dir. default: $PWD/omniscience
#   OMNISCIENCE_REPO      raw.githubusercontent.com base URL
#   OMNISCIENCE_HEALTH_URL  app /health URL. default: http://localhost:8000/health
#
# Supported: macOS (Intel + Apple Silicon), Linux (x86_64, arm64).
# Re-runnable: safe to invoke repeatedly; never destroys existing data.

set -euo pipefail

OMNISCIENCE_REF="${OMNISCIENCE_REF:-main}"
OMNISCIENCE_VERSION="${OMNISCIENCE_VERSION:-latest}"
OMNISCIENCE_DIR="${OMNISCIENCE_DIR:-$PWD/omniscience}"
OMNISCIENCE_REPO="${OMNISCIENCE_REPO:-https://raw.githubusercontent.com/100rd/Omniscience}"
HEALTH_URL="${OMNISCIENCE_HEALTH_URL:-http://localhost:8000/health}"
COMPOSE_FILE="docker-compose.prod.yml"

c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
c_blu() { printf '\033[34m%s\033[0m\n' "$*"; }

step() { c_blu ">>> $*"; }
ok()   { c_grn "    ok: $*"; }
warn() { c_ylw "    warn: $*"; }
die()  { c_red "    error: $*"; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "missing prerequisite: $1"
}

gen_secret() {
  # 32-byte URL-safe random — works on macOS + Linux.
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 32 | tr -d '=+/\n' | cut -c1-32
  else
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
  fi
}

main() {
  step "Checking prerequisites"
  need docker
  if ! docker compose version >/dev/null 2>&1; then
    die "docker compose v2 plugin not available (got: $(docker --version 2>&1))"
  fi
  if ! command -v curl >/dev/null 2>&1; then
    die "missing prerequisite: curl"
  fi
  ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"

  step "Preparing working directory: $OMNISCIENCE_DIR"
  mkdir -p "$OMNISCIENCE_DIR"
  cd "$OMNISCIENCE_DIR"

  if [ ! -f .env ]; then
    step "Generating .env (with fresh secrets)"
    pg_pw="$(gen_secret)"
    neo4j_pw="$(gen_secret)"
    qdrant_key="$(gen_secret)"
    sk="$(gen_secret)"
    umask 077
    # All ${VAR:?...} hard-validated entries from docker-compose.prod.yml
    # are written here (POSTGRES_PASSWORD, NEO4J_PASSWORD), plus the
    # service-discovery hostnames the app needs to reach each backing
    # service inside the compose network. OMNISCIENCE_VERSION pins the
    # GHCR image tag (ghcr.io/100rd/omniscience-*:${OMNISCIENCE_VERSION}).
    cat > .env <<EOF
# Omniscience environment — generated $(date -u +%Y-%m-%dT%H:%M:%SZ)

# === Image pin ===
# Override to e.g. v0.5.0 for reproducible installs (default: latest).
OMNISCIENCE_VERSION=${OMNISCIENCE_VERSION}

# === Required secrets (hard-validated by docker-compose.prod.yml) ===
POSTGRES_PASSWORD=${pg_pw}
NEO4J_PASSWORD=${neo4j_pw}
OMNISCIENCE_SECRET_KEY=${sk}

# === Postgres (operational metadata) ===
DATABASE_URL=postgresql+asyncpg://omniscience:${pg_pw}@postgres:5432/omniscience

# === NATS ===
NATS_URL=nats://nats:4222

# === Neo4j (graph store) ===
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_DATABASE=neo4j

# === Qdrant (vector store) ===
QDRANT_HOST=qdrant
QDRANT_GRPC_PORT=6334
QDRANT_HTTP_PORT=6333
QDRANT_API_KEY=${qdrant_key}
QDRANT_HTTPS=false
QDRANT_PREFER_GRPC=true

# === Storage backend selection (v0.2+: only neo4j + qdrant supported) ===
STORAGE_GRAPH_BACKEND=neo4j
STORAGE_VECTOR_BACKEND=qdrant

# === Application ===
ENVIRONMENT=production
APP_NAME=omniscience
LOG_LEVEL=info
EOF
    ok "wrote $OMNISCIENCE_DIR/.env (chmod 600)"
  else
    ok ".env already present — leaving untouched"
    # Ensure OMNISCIENCE_VERSION is present in older .env files (back-compat).
    if ! grep -q '^OMNISCIENCE_VERSION=' .env; then
      umask 077
      printf '\nOMNISCIENCE_VERSION=%s\n' "$OMNISCIENCE_VERSION" >> .env
      ok "appended OMNISCIENCE_VERSION=$OMNISCIENCE_VERSION to existing .env"
    fi
    # Backfill NEO4J_PASSWORD on .env files written by older installer versions.
    if ! grep -q '^NEO4J_PASSWORD=' .env; then
      neo4j_pw="$(gen_secret)"
      umask 077
      printf 'NEO4J_PASSWORD=%s\n' "$neo4j_pw" >> .env
      ok "appended NEO4J_PASSWORD to existing .env (was missing — pre-#261 install)"
    fi
  fi

  step "Fetching $COMPOSE_FILE @ ${OMNISCIENCE_REF}"
  curl -fsSL "${OMNISCIENCE_REPO}/${OMNISCIENCE_REF}/${COMPOSE_FILE}" -o "$COMPOSE_FILE"
  ok "wrote $COMPOSE_FILE"

  step "Starting stack (docker compose -f $COMPOSE_FILE up -d) — image pull may take a few minutes on first run"
  docker compose -f "$COMPOSE_FILE" up -d
  ok "containers started"

  step "Waiting for $HEALTH_URL (up to 300s)"
  set +o pipefail
  deadline=$(( $(date +%s) + 300 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      ok "/health is responding"
      break
    fi
    sleep 2
  done
  set -o pipefail
  if ! curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    warn "/health did not respond within 300s — check 'docker compose -f $COMPOSE_FILE logs app'"
  fi

  cat <<EOF

$(c_grn "Omniscience is up.")

Next steps:

  1) Mint an API token:

     cd "$OMNISCIENCE_DIR"
     docker compose -f $COMPOSE_FILE exec app omniscience tokens create \\
       --name my-client --scopes search,sources:read

  2) Wire it into your IDE in one shot:

     uvx --from omniscience-cli omniscience init --client claude-code
     uvx --from omniscience-cli omniscience init --client cursor
     uvx --from omniscience-cli omniscience init --client cline
     uvx --from omniscience-cli omniscience init --client zed

  Docs: https://github.com/100rd/Omniscience#integration-guides

EOF
}

main "$@"
