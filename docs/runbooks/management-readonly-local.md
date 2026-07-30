# Runbook: management-readonly-local-v1 (Omniscience owner fragment)

task-sp-95-management-readonly-local-runtime · ADR-0023 · consumes the exact
SP-86 `OmniscienceManagementReadOnlyRelease`.

- **Governing decisions:** Omniscience ADR-0023; genai-enablement
  `management-readonly-local-v1` profile (read-only cross-repo reference).
- **Class:** disposable, laptop-runnable **functional smoke only**.
- **Activation authority:** none. This is owner-local development evidence — not
  Portal evidence, platform qualification, production activation, or HA proof.

## What this fragment is

An independently runnable, owner-contained realization of the Omniscience slice
of the first synchronized-platform profile. Every service/network/volume is
`omniscience-*`. The selected local closure (ADR-0023) is the lite functional
runtime:

```text
omniscience-api  -> omniscience-postgres, omniscience-nats,
                    omniscience-neo4j, omniscience-qdrant
omniscience-admin -> omniscience-api
```

Embeddings run **in-process** from a pinned local model baked into the image
(no provider/model API call). Discovery/reconcile/scheduler/retention workers
are disabled. Only the admin UI is host-published, on a configurable loopback
port. No store or broker is host-published.

| Artifact | Path |
|---|---|
| Image-mode fragment | `deploy/compose/management-readonly-local/compose.yaml` |
| Source-build override | `deploy/compose/management-readonly-local/compose.source.yaml` |
| Env template | `deploy/compose/management-readonly-local/env.example` |
| Local service contract | `contracts/releases/management-readonly-local-v1/service-contract.json` |
| Qualifier / receipt | `scripts/qualify_management_readonly_local.py` |
| Readiness closure | `apps/server/src/omniscience_server/routes/health.py` (`/ready`) |

## Launch modes

### Source mode (runnable today)

```bash
cd deploy/compose/management-readonly-local
docker compose -f compose.yaml -f compose.source.yaml --env-file env.example up -d --build
```

Builds the owner images from this repo (`INSTALL_LOCAL_EMBEDDINGS=true` bakes the
pinned model). A dirty worktree is allowed for developer feedback but forces
`reproducible=false` / `qualification_status=development-only` in the receipt.

### Image mode (reproducible; pending an SP-86 OCI digest)

Set `OMNISCIENCE_API_IMAGE` / `OMNISCIENCE_ADMIN_IMAGE` to **@sha256 digests**
(mutable tags and `latest` are rejected) and run with `compose.yaml` alone. The
consumed SP-86 release lock currently records only
`pending.image_registry_digest` (no live OCI digest yet), so image mode is
**pending** until SP-86 publishes a real registry digest.

## Readiness

`/ready` (the container healthcheck target) covers the full selected closure:
`postgres`, `nats`, `neo4j`, `qdrant`, `migration`, and the release's own
`mcp_contract` / `management_context_contract` / `pw0_privacy_contract`
policy-init closure. Any unavailable dependency returns HTTP 503 with a typed,
non-identifying reason under `reasons` (e.g. `postgres_unavailable`,
`migration_incomplete`) — never cached-current healthy. `/ready` carries the
profile revision only; never a management verdict, recommendation, or
authorization.

```bash
docker exec <api-container> curl -fsS http://localhost:8000/ready
```

## Functional-smoke gates

| Gate | Command / observation |
|---|---|
| Render | `docker compose ... config` renders; exact owner inventory, no mutable tags, single loopback binding |
| Start | migrations complete once; every steady service reaches dependency-aware health within the bound |
| Readiness | stop a store → `/ready` 503 with the typed reason; restart → 200 |
| Consumer contract | `pytest tests/release/management_readonly_local/test_service_contract.py` (positive read passes; foreign/stale/skewed/generic fail closed) |
| PW0 | `pytest tests/release/management_readonly/test_pw0_seeded_leak.py` (seeded PII reaches no store/graph/vector/embed/response sink) |
| No provider egress | local embeddings load under `--network none`; api has no external peer |
| Persistence | seed → `down` (keeps volumes) → `up` → state survives; volume identity stable |
| Severance | a lost dependency is typed unavailable, never cached-healthy |

## Qualification and the receipt

```bash
# capture the rendered model, then qualify + write the receipt
docker compose -f deploy/compose/management-readonly-local/compose.yaml \
  -f deploy/compose/management-readonly-local/compose.source.yaml \
  --env-file deploy/compose/management-readonly-local/env.example config > /tmp/rendered.yaml
uv run python scripts/qualify_management_readonly_local.py \
  --mode source --rendered-config /tmp/rendered.yaml --write
```

Emits `OmniscienceLocalRuntimeReceipt` under
`contracts/releases/management-readonly-local-v1/evidence/local-runtime-receipt.<git_commit>.json`.

### A RED receipt before commit is expected

The receipt is RED while the scoped worktree is dirty (`scoped_worktree_dirty`),
while the SP-86 OCI digest is unpublished (`sp86_image_registry_digest_pending`),
and until a rendered config is supplied — exactly as the SP-86 release lock is
RED for `scoped_worktree_dirty` before its own commit. GREEN requires a clean
scoped commit, a supplied rendered config, and (for image mode) a published
SP-86 OCI digest. The receipt always declares
`availability_class=development-single-host`, `ha_qualified=false`,
`activation_authority=none`.

## Reference host envelope

The minimum image-mode target is 4 vCPU / 8 GB / 25 GB. Measured on this host
(arm64, Docker Desktop): steady aggregate ~1.3 GiB, per-service memory limits
declared in the fragment. **amd64 measurements are pending** a second-arch run
(a single host cannot measure both architectures).

## Shutdown and reset

```bash
docker compose ... down        # preserves named omniscience-* volumes
docker compose ... down -v     # EXPLICIT, DESTRUCTIVE reset (separate command)
```

Normal `down` preserves owner state. Reset is never implicit.

## Rollback

Stop the fragment preserving volumes and restore the exact prior SP-86
artifacts. Digests roll back as one unit by their exact content identity, never
by a mutable tag/branch (see the SP-86 `rollback.json`).
