---
id: gh-issue-350-consumer-severance
title: Publish and run MCP consumer severance conformance
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-17
source: { kind: github, ref: "350", url: "https://github.com/100rd/Omniscience/issues/350" }
governingAdrs: [genai-enablement/ADR-0017, Omniscience/ADR-0019]
capabilitySpecs: [SPEC-MCP, SPEC-KP, SPEC-EV]
sddMode: full
repo: 100rd/Omniscience
evidenceDestination: component-execution-index://docs/specs/execution-index.json#gh-issue-350-consumer-severance
scope:
  include:
    - scripts/check_consumer_severance.py
    - tests/conformance/consumer_severance/**
    - tests/test_consumer_severance*.py
    - docs/runbooks/consumer-severance.md
    - docs/integrations/**
  exclude:
    - apps/**
    - packages/**
    - docs/decisions/**
    - docs/specs/**
    - specs/**
    - graphify-out/**
acceptanceCriteria:
  - id: AC-SEV-1
    requirement: The conformance kit covers every MCP v1 evidence-fitness and contract-pin failure
    probe: consumer-evidence-fitness-matrix
    expected: stale, unknown, degraded, missing-lineage, fallback-required, unavailable, and every contract digest mismatch deterministically select direct-source fallback
    groundTruth: versioned producer fixtures, MCP v1 schemas, and expected consumer decisions
  - id: AC-SEV-2
    requirement: Omnius and the SRE harness are checked from exact immutable consumer revisions
    probe: pinned-consumer-conformance
    expected: each consumer emits a content-addressed receipt for the complete fitness matrix; absent, dirty, mutable, or partial evidence is RED
    groundTruth: consumer Git revisions, conformance commands, fixture digest, and signed or CI-backed receipts
  - id: AC-SEV-3
    requirement: Removing Omniscience does not stop already materialized work
    probe: live-consumer-severance-drill
    expected: work continues from pinned/direct sources or parks only when authoritative sources also fail
    groundTruth: live Omnius and SRE harness drill traces joined to the materialized task or incident id
  - id: AC-SEV-4
    requirement: No consumer uses Omniscience output as the sole correctness or authorization oracle
    probe: consumer-oracle-boundary
    expected: merge, apply, incident action, and terminal decisions require independent authority
    groundTruth: consumer gate traces and negative conformance tests
  - id: AC-SEV-5
    requirement: The producer-side verifier is read-only against consumer repositories
    probe: consumer-scope-isolation
    expected: verification accepts only explicit revision and receipt inputs and cannot edit, commit, push, or broaden scope in Omnius or SRE
    groundTruth: sandbox permissions, read-only checkout tests, and repository status fingerprints
rollback: { kind: revert-pr, probe: remove-producer-conformance-kit-and-retain-consumer-pins }
---

## Outcome

Publish one producer-owned fixture matrix and verifier, then use it to collect comparable severance
receipts from Omnius and the SRE harness. The Omniscience task is ready because its own work is bounded;
consumer changes, if a receipt is RED, require separate human-ready task specs in those repositories.

## Execution order and external boundary

Start after MCP v1 schemas, manifest, and `contract_info` materialize from one clean commit. Prove the
fixture matrix and verifier locally, then run read-only checks against explicitly supplied consumer
revisions. Run a live drill only in a named safe environment with an already materialized task or
incident and verified direct-source access.

A missing consumer revision, direct-source profile, owner approval, or safe drill target produces a
decision return and RED receipt. It does not authorize this agent to edit another repository.

## Out of scope

- writing, committing, or pushing changes in Omnius or the SRE repository
- making Omniscience confidence, projection state, or synthesis a correctness oracle
- treating a fixture-only test as live severance evidence
- changing the accepted ADRs, capability SPECs, this ready revision, or its probes

The human-ready revision is immutable during execution.
