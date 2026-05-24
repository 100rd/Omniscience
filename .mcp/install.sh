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
#   4. Fetches docker-compose.yml from the repo at the pinned ref.
#   5. Starts the stack with `docker compose up -d`.
#   6. Waits for /health to return 200 (up to 120s).
#   7. Prints next-step instructions: how to mint a token and run
#      `omniscience init --client <ide>`.
#
# Supported: macOS (Intel + Apple Silicon), Linux (x86_64, arm64).
# Re-runnable: safe to invoke repeatedly; never destroys existing data.

set -euo pipefail

OMNISCIENCE_REF="${OMNISCIENCE_REF:-main}"
OMNISCIENCE_DIR="${OMNISCIENCE_DIR:-$PWD/omniscience}"
OMNISCIENCE_REPO="${OMNISCIENCE_REPO:-https://raw.githubusercontent.com/100rd/Omniscience}"
HEALTH_URL="${OMNISCIENCE_HEALTH_URL:-http://localhost:8000/health}"

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
    pw="$(gen_secret)"
    sk="$(gen_secret)"
    umask 077
    cat > .env <<EOF
# Omniscience environment — generated $(date -u +%Y-%m-%dT%H:%M:%SZ)
POSTGRES_PASSWORD=${pw}
OMNISCIENCE_SECRET_KEY=${sk}
EOF
    ok "wrote $OMNISCIENCE_DIR/.env (chmod 600)"
  else
    ok ".env already present — leaving untouched"
  fi

  step "Fetching docker-compose.yml @ ${OMNISCIENCE_REF}"
  curl -fsSL "${OMNISCIENCE_REPO}/${OMNISCIENCE_REF}/docker-compose.yml" -o docker-compose.yml
  ok "wrote docker-compose.yml"

  step "Starting stack (docker compose up -d)"
  docker compose up -d
  ok "containers started"

  step "Waiting for $HEALTH_URL"
  deadline=$(( $(date +%s) + 120 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      ok "/health is responding"
      break
    fi
    sleep 2
  done
  if ! curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    warn "/health did not respond within 120s — check 'docker compose logs app'"
  fi

  cat <<EOF

$(c_grn "Omniscience is up.")

Next steps:

  1) Mint an API token:

     cd "$OMNISCIENCE_DIR"
     docker compose exec app omniscience tokens create \\
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
