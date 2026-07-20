# Backup/restore qualification runbook

> Status: qualification harness + safety interlocks shipped (AC-DR-1, AC-DR-2,
> AC-DR-3, AC-DR-5, AC-DR-6, and the AC-DR-3/P-SOT-6 pre-wipe refusal gap in
> `rebuild_all_projections.py`) — issue
> [#350](https://github.com/100rd/Omniscience/issues/350),
> [`gh-issue-350-backup-restore`](../specs/gh-issue-350-backup-restore.md).
> AC-DR-1's live destructive restore, AC-DR-2's live cloud-identity binding,
> AC-DR-3's live target-checks, and AC-DR-5's full measured drill are
> **blocked on external inputs** — see
> [Blocked on external — decision returns](#blocked-on-external--decision-returns)
> below.

This runbook documents `scripts/qualify_backup_restore.py`: a repo-local
qualification harness for PostgreSQL-authoritative backup/restore. It never
performs a destructive operation, never opens a cloud SDK connection, and
never stores or requires cloud credentials — every check is a pure function
over explicit evidence (JSON files, injected fakes) that returns a frozen
`GREEN`/`RED` decision-return and never fabricates a passing result.

## What this kit proves, and what it doesn't

- **Proves (repo-local, GREEN today):**
  - AC-DR-1's evidence shape: a backup-policy document names every
    SPEC-SOT authority table plus immutable-copy ownership, retention,
    encryption, cross-account/region copies, and a measured recovery point
    — missing fields are RED (`check_backup_policy_evidence`).
  - AC-DR-2's approval logic: absent, expired, mismatched, or replayed
    one-shot approval envelopes abort before mutation, each with a
    field-specific reason (`verify_approval_envelope`).
  - AC-DR-3's refusal logic: production-like target names, an absent
    disposable-identity tag, non-empty projections outside a sanctioned
    drill context, active writer leases, and ambiguous credentials each
    independently abort (`check_destructive_safety`).
  - AC-DR-3 / P-SOT-6's rebuild gap: `scripts/rebuild_all_projections.py`
    now measures Neo4j/Qdrant occupancy immediately before wiping them and
    refuses to proceed if either is non-empty, unless
    `OMNISCIENCE_DR_DRILL=1` is set (exit code `4`). Previously `--yes`
    wiped unconditionally with no precondition check at all.
  - AC-DR-5's manifest shape: a drill manifest is content-addressed
    (`manifest_id` is a self-addressing sha256 of every other field) and
    any absent or breached target — RPO, RTO, verification, hash
    equivalence, query probes — produces `status="RED"`, never a
    placeholder (`build_manifest`, `validate_manifest_json`).
  - AC-DR-6's reproducibility: every check above runs from committed,
    deterministic fixtures with no cloud credentials — see
    `tests/test_backup_restore_fixtures.py` for the content-addressed
    fixture matrix and `.github/workflows/restore-qualification.yml` for
    the CI wiring.
- **Does not prove:** that a real destructive restore into an isolated
  recovery environment succeeds, that a live cloud identity check agrees
  with an envelope's claim, that a live target's tags/writer-leases/
  credentials were actually inspected, or that a full measured RPO/RTO
  drill converges. Those require external inputs this agent must not
  self-authorize — see below.

## Running the qualification CLI

Each check runs independently; supply the flags for the checks you want.
The process exits `0` and prints `GREEN` only if every requested check
passes; otherwise it exits `1`, prints `RED (decision-return)` to stderr,
and lists one reason per line — never a traceback.

### AC-DR-1: backup-policy evidence

```bash
uv run python scripts/qualify_backup_restore.py --policy backup-policy.json
```

`backup-policy.json` shape:

```json
{
  "authority_tables": ["workspaces", "sources", "documents", "ingestion_runs", "chunks", "entities", "edges", "entity_emitter", "outbox_events", "audit_log"],
  "immutable_copy": {"owner": "backup-team", "retention_days": 35, "encryption": "aws:kms:alias/omniscience-backup"},
  "cross_account_copy": {"account": "<recovery account id>"},
  "cross_region_copy": {"region": "<recovery region>"},
  "measured_recovery_point_seconds": 300
}
```

The `authority_tables` list is checked against `AUTHORITY_TABLES` in
`scripts/qualify_backup_restore.py`. This is not simply "every
`__tablename__` in `db/models.py`" — it is the subset that
[REQ-SOT-1] ("source/document/chunk content and lineage, versions,
entity/outbox records, workspaces, and governance metadata required to
rebuild projections") and [REQ-SOT-8] ("every field needed for projection
rebuild and tenant/audit history") actually name, spelled out per table in
the constant's own comment: `workspaces`/`sources`/`documents`/`chunks`
(content), `ingestion_runs` (lineage), `entities`/`edges`/`outbox_events`
(entity/outbox records), `entity_emitter` (governance — the
authority-emitter state machine a projection consumer uses to accept/drop
an entity write), and `audit_log`
(`packages/core/src/omniscience_core/audit/models.py` — REQ-SOT-8's
"tenant and audit history" verbatim). `api_tokens` is deliberately
excluded: it is authentication/authorization credential material, not
ledger content, lineage, an entity/outbox record, or rebuild-relevant
governance metadata.

### AC-DR-2: destructive-command approval envelope

```bash
uv run python scripts/qualify_backup_restore.py \
  --envelope approval-envelope.json \
  --command "<exact destructive command string, including all args>" \
  --source-backup-id <backup id> \
  --target-account <cloud account id> \
  --target-region <cloud region> \
  --environment-identity <independently observed target identity> \
  --now <RFC3339 timestamp> \
  --nonce-ledger-path nonce-ledger.json
```

`--environment-identity` must come from a source independent of the
envelope (a live identity check in a real drill; a known value in a test)
— the envelope's own `environment_identity` claim is never trusted as its
own proof. `--nonce-ledger-path` points at a JSON array file of already-
consumed nonces; a second submission of the same nonce is `RED
nonce_replayed`. A rejected submission (bad digest, expired, mismatched)
never consumes the nonce, so a legitimate resubmission with a corrected
envelope still works.

Approval-envelope shape:

```json
{
  "command_digest": "<sha256 hex of the exact destructive command>",
  "source_backup_id": "<opaque backup id the approval authorizes restoring from>",
  "target_account": "<cloud account id>",
  "target_region": "<cloud region>",
  "environment_identity": "<independently observed target identity>",
  "issued_at": "<RFC3339>",
  "expires_at": "<RFC3339, short TTL>",
  "nonce": "<random, single-use token>",
  "approved_by": "<human identity>"
}
```

### AC-DR-3: destructive-safety refusal

```bash
uv run python scripts/qualify_backup_restore.py \
  --target-name <target identifier> \
  --target-tags-json '{"disposable": "true"}' \
  --qdrant-chunk-count <measured count> \
  --neo4j-entity-count <measured count> \
  [--drill-context] [--active-writers] [--ambiguous-credentials]
```

`--drill-context` authorizes wiping projections that are already non-empty
(the ordinary DR-rebuild case). It does not bypass the production-name,
disposable-tag, writer-lease, or credential-ambiguity checks.

### AC-DR-5: drill manifest validation

```bash
uv run python scripts/qualify_backup_restore.py --manifest drill-manifest.json
```

Re-validates an already-produced manifest artifact's shape and declared
status — it does not trust a self-declared `"status": "GREEN"` on its own:
`rto_exceeded: true`, `verification_passed: false`, or
`query_probes_passed: false` (or absent) all fail validation even if
`status` claims GREEN (tamper-evidence, not just shape checking). Build
a manifest programmatically with `build_manifest(...)` from measured drill
inputs (`drill_started_at`, `drill_completed_at`, `measured_rpo_seconds`, a
`dr_verify.RtoResult`, `source_backup_id`, `restored_environment_identity`,
a `dr_verify.DRVerificationResult`, hash-equivalence mismatches, and
`query_probes_passed`) — any absent input produces `status="RED"` with a
named reason, never an invented value.

## Pre-wipe refusal in `rebuild_all_projections.py`

`scripts/rebuild_all_projections.py --yes` now measures total Qdrant point
count and total Neo4j node count immediately before wiping either store.
If either is non-empty, the script refuses to wipe (exit code `4`) unless
`OMNISCIENCE_DR_DRILL=1` is set — the same sanctioned-drill marker
`scripts/seed_dr_drill.py` already requires. This closes a real gap:
previously `--yes` alone wiped whatever it found with no precondition check
(REQ-SOT-6: "unmet precondition aborts before destructive action").

`.github/workflows/dr-drill.yml` is unaffected: its Neo4j/Qdrant service
containers start empty on every run, so the check passes trivially even
without the env var. The env var only matters for a rebuild against
already-populated projections (a repeat run, or a real incident where
Neo4j/Qdrant hold stale data that needs replacing from the Postgres source
of truth).

The production-like-name, disposable-tag, writer-lease, and credential-
ambiguity checks in `check_destructive_safety` are **not** wired into
`rebuild_all_projections.py` today — there is no target-identity config
surface in `omniscience_core.config.Settings` to source a target name/tag
set/writer-lease query from. A future live-drill runner that has access to
a real cloud target's identity and tags should call
`check_destructive_safety` with those fields populated; until then this is
a flagged architectural gap, not a silent omission.

Exit codes for `rebuild_all_projections.py`:

| Code | Meaning |
|------|---------|
| 0 | Rebuild and verification passed, RTO met |
| 1 | Missing `--yes` (and not `--verify-only`), or bad `--rto-seconds` |
| 2 | RTO budget exceeded |
| 3 | Post-rebuild verification mismatch |
| 4 | Pre-wipe destructive-safety refusal (non-empty projections outside a drill context) |

## RPO/RTO budget

ADR-0018 sets the default projection-rebuild budget at **900 seconds (15
minutes)**: Neo4j+Qdrant wipe ≤30s, rebuild 50k chunks ≤600s, verification
≤30s, safety margin 240s. `scripts/qualify_backup_restore.py` imports
`DEFAULT_RTO_SECONDS`-carrying types from `scripts/dr_verify.py` rather
than redefining the budget — any caller claiming production-representative
timing must use 900s unless an explicit, human-approved environment
profile supplies a stricter number.

`.github/workflows/dr-drill.yml` and `.github/workflows/restore-
qualification.yml` both use a **120-second CI-fixture budget**
(`--rto-seconds 120`) because their seeded dataset is a handful of
documents/chunks, not 50k. This is a small-fixture budget for a small
fixture dataset — it is not, and must never be read as, evidence that a
production-scale rebuild meets the real 900s target. Only a live drill
against a production-representative dataset measures that.

## Blocked on external — decision returns

Per `docs/specs/gh-issue-350-backup-restore.md` ("Execution order and
required inputs" / "Out of scope") and this workspace's approval rules,
the following are **decision returns**, not something this harness
attempts or fabricates:

1. **AC-DR-1's live destructive restore** — requires real cloud provider
   backup metadata and a genuinely separate recovery account/environment.
   Unavailable from a control-plane checkout; no cloud mutation authority
   here.
2. **AC-DR-2's live "independently observed cloud identity"** — the
   envelope-verification logic is fully built and tested with fixtures;
   binding `observed_environment_identity` to a real cloud identity
   provider (STS, IAM, etc.) requires credentials this agent must not
   hold, and requires a fresh one-shot human approval this agent cannot
   self-grant.
3. **AC-DR-3's live target-tag/writer-lease/credential-ambiguity checks
   against a real cloud account** — the refusal logic is built and tested
   with fakes; running it against a real disposable-or-approved recovery
   environment is a live-drill action requiring the same separate
   approval as #1. The concrete writer-lease source (a consumer heartbeat
   row? a NATS consumer status query?) also needs an architectural
   decision — no such mechanism exists in `apps/**` today.
4. **AC-DR-5's full destructive drill and its measured real-world
   RPO/RTO** — the manifest schema and validation logic is built; a
   genuine measured RPO/RTO against a live restore requires the isolated
   recovery environment from #1, plus a fresh one-shot approval per drill.
   Readiness approval, a previous drill approval, a target name, or
   possession of credentials is explicitly **not** a substitute
   (task-spec, verbatim).
5. **Any actual deletion, restore, or infra apply in a production
   account** — explicit out-of-scope line in the task spec.

Two additional precision questions are flagged for the Lead/architect,
not resolved here:

- **Vector tolerance for non-deterministic embeddings.** `dr_verify.
  verify_hash_equivalence` does an exact sha256 digest comparison, which
  is correct only because the DR-drill fixture forces a deterministic
  local embedding model (`EMBEDDING_PROVIDER=local`). A live drill against
  a non-deterministic remote embedding provider would need a tolerance-
  band comparison (e.g. cosine similarity ≥ threshold) instead — not
  implemented here, since no such live drill exists yet to size the
  threshold against.
- **Writer-lease source.** `check_destructive_safety`'s
  `writer_lease_source` parameter is a `Protocol` with a fake
  implementation for tests; there is no concrete, real writer-lease query
  wired to any live consumer/heartbeat mechanism in this repo today.

Each live destructive restore and each destructive cleanup/rollback needs
a separate, fresh, one-shot human approval envelope bound to the exact
command digest and independently observed target identity — this agent
does not perform destructive actions and does not self-authorize them.
