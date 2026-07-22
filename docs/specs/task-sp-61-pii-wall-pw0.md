---
id: task-sp-61-pii-wall-pw0
title: Implement the fail-closed PW0 knowledge propagation boundary
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-21
source: { kind: human, ref: "SP-61" }
governingAdrs: [genai-enablement/ADR-0018, Omniscience/ADR-0020]
capabilitySpecs: [SPEC-PII]
sddMode: full
repo: 100rd/Omniscience
evidenceDestination: ci-artifact://synchronized-platform/task-sp-61-pii-wall-pw0/
scope:
  include:
    - apps/server/src/omniscience_server/privacy/**
    - packages/core/src/omniscience_core/privacy/**
    - contracts/pii/**
    - tests/test_pii*.py
    - tests/privacy/**
    - docs/runbooks/pii-wall.md
  exclude: [docs/decisions/**, docs/specs/**, specs/**, graphify-out/**, terraform/**]
acceptanceCriteria:
  - id: AC-SP61-1
    requirement: PW0 blocks or sanitizes before every ordinary store, parse/chunk/embed/projection sink
    probe: P-PII-1+P-PII-2+P-PII-3
    expected: seeded personal, sensitive, prohibited and unknown fixtures never reach protected sinks
    groundTruth: human-approved task revision and SPEC-PII probe contract, immutable to the execution identity
  - id: AC-SP61-2
    requirement: Policy skew, detector failure and quarantine failure are closed and visible
    probe: P-PII-7+P-PII-8
    expected: affected ingestion parks with a non-identifying receipt and no unsafe fallback
    groundTruth: accepted policy-skew and dependency-failure matrix in SPEC-PII, independently replayed
  - id: AC-SP61-3
    requirement: Lifecycle receipts remain per-store and partial
    probe: P-PII-5+P-PII-6
    expected: deletion/restore cannot claim complete while any required store is pending or unavailable
    groundTruth: per-store owner lifecycle receipts and SPEC-PII completion rules, not aggregate producer status
rollback: { kind: revert-pr, probe: disable-unreleased-pw0-code-and-retain-ingestion-refusal }
---

## Authority boundary

Repository-local implementation and disposable fixtures only. No live personal data, provider call,
profile activation, production quarantine store, destructive deletion, deployment or privacy claim.
