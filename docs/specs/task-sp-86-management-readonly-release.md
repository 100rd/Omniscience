---
id: task-sp-86-management-readonly-release
title: Publish the Omniscience management-readonly-v1 release candidate
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-25
source: { kind: human, ref: "SP-86" }
governingAdrs: [genai-enablement/ADR-0012, genai-enablement/ADR-0017, genai-enablement/ADR-0018, genai-enablement/ADR-0020, genai-enablement/ADR-0021, genai-enablement/ADR-0022, Omniscience/ADR-0022]
capabilitySpecs: [SPEC-MCP, SPEC-PII, SPEC-MCTX, SPEC-ACL, SPEC-EV, SPEC-KP, SPEC-OPS, SPEC-SOT]
sddMode: full
repo: 100rd/Omniscience
executionProfile: management-readonly-v1-release-candidate
evidenceDestination: ci-artifact://synchronized-platform/task-sp-86-management-readonly-release/
scope:
  include:
    - Dockerfile
    - helm/omniscience/**
    - apps/server/src/omniscience_server/app.py
    - apps/server/src/omniscience_server/routes/health.py
    - apps/server/src/omniscience_server/management/**
    - apps/server/src/omniscience_server/privacy/**
    - packages/core/src/omniscience_core/management/**
    - packages/core/src/omniscience_core/privacy/**
    - contracts/mcp/**
    - contracts/management/**
    - contracts/pii/**
    - contracts/releases/management-readonly-v1/**
    - scripts/qualify_management_readonly_release.py
    - tests/release/management_readonly/**
    - tests/test_management_readonly_release*.py
    - docs/runbooks/management-readonly-release.md
    - .github/workflows/management-readonly-release.yml
  exclude: [docs/decisions/**, docs/specs/**, specs/**, graphify-out/**, infra/terraform/**]
acceptanceCriteria:
  - id: AC-SP86-1
    requirement: One immutable release binds the complete selected executable contract policy and configuration closure
    probe: omniscience-management-readonly-release-lock
    expected: Git image chart MCP context schema PW0 policy dependency and rollback digests re-derive exactly and mutable or dirty inputs are RED
    groundTruth: clean protected Git revision OCI registry digest chart package digest and separately published SP-60 policy artifact
  - id: AC-SP86-2
    requirement: Selected read and readiness surfaces enforce exact tenant workspace purpose contract freshness and dependency health
    probe: omniscience-management-readonly-runtime-conformance
    expected: positive reads pass while foreign stale future skewed incomplete and disabled-dependency states fail closed without field donation
    groundTruth: independently minted tenant/workspace identities pinned SP-10/SP-81 manifests and direct dependency observations
  - id: AC-SP86-3
    requirement: PW0 rejects unsafe data before every durable derived provider telemetry archive and backup boundary
    probe: omniscience-management-readonly-pw0-seeded-leak
    expected: seeded PII unknown class reversible token and active content never reach a protected sink response log trace metric archive or backup
    groundTruth: independent seeded corpus sink captures SP-60 policy revision and per-store SP-61 lifecycle inventory
  - id: AC-SP86-4
    requirement: The producer release includes a complete selected-consumer severance fixture kit and read-only verifier
    probe: omniscience-management-readonly-severance-kit
    expected: loss token expiry freshness expiry and contract skew fixtures require typed fallback and cannot validate false healthy empty or cached-current decisions
    groundTruth: immutable SP-10/SP-81 manifests producer fixtures and expected decisions independently reviewed before consumer execution
  - id: AC-SP86-5
    requirement: The release is observable restart-safe and reversible without Omnius or a model/provider
    probe: omniscience-management-readonly-operations-rollback
    expected: health readiness metrics restart projection convergence and exact-digest rollback pass while Omnius/provider/config scans remain empty
    groundTruth: disposable environment workload/store observations OCI/chart inventory and post-rollback conformance capture
  - id: AC-SP86-6
    requirement: The released consumer contract and severance kit remain implementation-language neutral
    probe: omniscience-management-readonly-language-neutral-consumer-kit
    expected: a clean Go consumer validates schemas fixtures auth freshness and severance decisions without executing Node.js or relying on a TypeScript-only generated binding
    groundTruth: exact released schema and fixture digests independently built minimal Go consumer and captured dependency and process inventories
rollback: { kind: revert-pr, probe: restore-prior-omniscience-release-digests-and-reject-unqualified-candidate }
---

## Intent

Turn the already implemented SP-10, SP-61 and SP-81 repository-local slices into one immutable release
candidate for `management-readonly-v1`. The task may harden packaging, runtime wiring, readiness and
qualification code only in the listed paths. It must not reinterpret earlier fixture completion as a
release receipt.

## Required inputs and dependency return

The execution identity requires exact SP-10, SP-60, SP-61, SP-70 and SP-81 artifacts plus a clean source
revision and disposable qualification target. SP-87/SP-88 consume the completed SP-86 release and
produce their own later receipts; they are not inputs to this producer task.

SP-86 is also independent of Barbarossa SP-90 and may execute concurrently with it. Omniscience does not
inspect, approve or mint the Go baseline; SP-87 later binds the two owner receipts.

Missing inputs produce a content-addressed RED/decision-required report. The task cannot edit
Barbarossa, Platform Portal, Omnius, `genai-enablement`, a production environment, or this ready task
revision.

## Required output

`OmniscienceManagementReadOnlyRelease` contains at least:

```text
profile_id/revision; git_commit; image_digest; chart_digest; configuration_digest;
mcp_manifest_digest; management_context_manifest_digest; schema_set_digest;
pii_policy_revision/digest; pw0_receipt_refs; auth/token_profile; dependency_profile;
health/readiness contract; severance_fixture_digest; language_neutral_consumer_kit_digest;
evidence_refs; rollback_release_digest
```

The output is producer evidence only. It is not a Barbarossa/Portal receipt, deployment activation,
privacy verdict or production-readiness claim.
