---
id: task-sp-81-management-context-v1
title: Implement management context and knowledge-quality producer v1
status: implemented
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-21
source: { kind: human, ref: "SP-81" }
governingAdrs: [genai-enablement/ADR-0017, genai-enablement/ADR-0020, Omniscience/ADR-0021]
capabilitySpecs: [SPEC-MCTX, SPEC-EV, SPEC-KP, SPEC-MCP, SPEC-ACL, SPEC-OPS]
sddMode: full
repo: 100rd/Omniscience
evidenceDestination: ci-artifact://synchronized-platform/task-sp-81-management-context-v1/
scope:
  include:
    - apps/server/src/omniscience_server/management/**
    - packages/core/src/omniscience_core/management/**
    - contracts/management/**
    - tests/test_management_context*.py
    - tests/management/**
    - docs/api/management-context.md
  exclude: [docs/decisions/**, docs/specs/**, specs/**, graphify-out/**, terraform/**]
acceptanceCriteria:
  - id: AC-SP81-1
    requirement: Producer emits schema-valid cited context and orthogonal quality axes
    probe: P-MCTX-1+P-MCTX-2+P-MCTX-4+P-MCTX-7
    expected: pinned fixtures reproduce exact envelopes, citations and quality conditions
    groundTruth: human-approved task revision and SPEC-MCTX requirement/probe contract, immutable to the execution identity
  - id: AC-SP81-2
    requirement: Producer cannot emit management truth or unsafe content
    probe: P-MCTX-3+P-MCTX-5
    expected: authority fields, active content, credentials and seeded PII fail before response
    groundTruth: accepted SPEC-MCTX forbidden-field and PII corpus replayed by the independent conformance runner
  - id: AC-SP81-3
    requirement: Contract skew and severance are safe
    probe: P-MCTX-6
    expected: exact fallback-required states replace best-effort parsing or stale success
    groundTruth: pinned producer manifest plus independently induced skew and severance observations
rollback: { kind: revert-pr, probe: remove-unreleased-management-endpoint-and-retain-direct-source-path }
---

## Authority boundary

Build schemas, fixtures, read-only producer code and conformance only. No live consumer pin, production
token, provider/model call, management decision, deployment or readiness claim.
