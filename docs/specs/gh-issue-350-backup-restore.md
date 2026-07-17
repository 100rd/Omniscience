---
id: gh-issue-350-backup-restore
title: Implement isolated authoritative backup and restore qualification
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-17
source: { kind: github, ref: "350", url: "https://github.com/100rd/Omniscience/issues/350" }
governingAdrs: [genai-enablement/ADR-0017, Omniscience/ADR-0018, Omniscience/ADR-0019]
capabilitySpecs: [SPEC-SOT, SPEC-OPS]
sddMode: full
repo: 100rd/Omniscience
scope:
  include:
    - scripts/qualify_backup_restore.py
    - scripts/rebuild_all_projections.py
    - scripts/dr_verify.py
    - docs/runbooks/backup-restore.md
    - tests/test_backup_restore*.py
    - tests/test_dr_rebuild.py
    - .github/workflows/restore-qualification.yml
  exclude:
    - apps/**
    - packages/**
    - docs/decisions/**
    - docs/specs/**
    - specs/**
    - graphify-out/**
    - terraform/**
acceptanceCriteria:
  - id: AC-DR-1
    requirement: PostgreSQL PITR plus cross-account and cross-region copies protect the complete authoritative ledger
    probe: authoritative-ledger-restore
    expected: policy and restore evidence cover every SPEC-SOT authority table, immutable copy ownership, retention, encryption, and a measured recovery point; missing fields are RED
    groundTruth: provider backup metadata and a destructive restore into an isolated recovery environment
  - id: AC-DR-2
    requirement: Every destructive command requires a fresh one-shot human approval bound to the exact operation
    probe: destructive-restore-approval
    expected: approval binds command digest, source backup, target account, region, environment identity, expiry, and nonce; absent, expired, mismatched, or replayed approval aborts before mutation
    groundTruth: human approval envelope, independently observed cloud identity, command digest, and redemption audit
  - id: AC-DR-3
    requirement: The destructive path refuses any target not independently marked disposable and empty
    probe: destructive-restore-safety
    expected: production-like names, absent disposable identity, non-empty projections, active writers, or ambiguous credentials abort before deletion or restore
    groundTruth: cloud identity, target tags, store counts, writer leases, and negative fixtures
  - id: AC-DR-4
    requirement: Neo4j and Qdrant rebuild from the restored ledger as non-authoritative projections
    probe: projection-rebuild-convergence
    expected: counts, versions, lineage hashes, checkpoints, and semantic vector tolerance converge on two idempotent runs; snapshots may accelerate but cannot replace ledger replay
    groundTruth: restored PostgreSQL rows and live ledger-to-projection reconciliation evidence
  - id: AC-DR-5
    requirement: Full destructive drill records measured RPO/RTO and end-to-end query convergence
    probe: destructive-restore-drill
    expected: the report evaluates required environment targets and the ADR-0018 projection budget; absent or breached targets remain RED
    groundTruth: immutable drill manifest, timestamps, hashes, and query probes
  - id: AC-DR-6
    requirement: The drill is reproducible without production credentials in CI
    probe: restore-qualification-replay
    expected: deterministic fixtures exercise identity refusal, restore verification, projection convergence, breach handling, and manifest validation
    groundTruth: committed tests, workflow definition, and content-addressed fixture results
rollback: { kind: disposable, probe: one-shot-approved-destroy-isolated-recovery-environment }
---

## Outcome

Build the qualification command, safety interlocks, runbook, deterministic CI fixtures, and evidence
manifest for a PostgreSQL-authoritative recovery. A real drill restores into a separately identified
recovery environment, then rebuilds Neo4j and Qdrant and proves query convergence.

## Execution order and required inputs

Start after the production-HA profile defines the recovery environment contract. Implement and prove
all refusal paths with local fixtures before accepting cloud credentials. A live drill requires
content-addressed inputs for backup owner, source and recovery identities, regions, retention,
encryption, RPO/RTO targets, and disposable target.

An absent input is a typed RED result, not a placeholder and not permission for the agent to invent a
value. The accepted ADR-0018 default of 900 seconds remains the projection-rebuild budget unless a
human-approved environment profile is stricter.

Task readiness authorizes implementation of the harness and fixtures only. Each live destructive
restore and each destructive cleanup/rollback requires a separate, fresh, one-shot human approval
envelope bound to the exact command digest and independently observed target identity. Readiness
approval, a previous drill approval, a target name, or possession of credentials is not a substitute.

## Out of scope

- deleting, restoring, or applying infrastructure in a production account
- treating Neo4j or Qdrant snapshots as the authoritative backup
- storing cloud credentials, restored customer data, or backup payloads in Git or CI artifacts
- changing the accepted ADRs, capability SPECs, this ready revision, or its probes

The human-ready revision is immutable during execution.
