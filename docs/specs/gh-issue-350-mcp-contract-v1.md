---
id: gh-issue-350-mcp-v1
title: Publish the stable freshness-aware MCP contract v1
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-15
source:
  kind: github
  ref: "350"
  url: https://github.com/100rd/Omniscience/issues/350
governingAdrs:
  - genai-enablement/ADR-0017
  - Omniscience/ADR-0019
capabilitySpecs:
  - SPEC-MCP
  - SPEC-EV
  - SPEC-KP
  - SPEC-ACL
  - SPEC-OPS
sddMode: full
repo: 100rd/Omniscience
evidenceDestination: component-execution-index://docs/specs/execution-index.json#gh-issue-350-mcp-v1
scope:
  include:
    - apps/server/src/omniscience_server/mcp/**
    - apps/server/src/omniscience_server/routes/tokens.py
    - packages/core/src/omniscience_core/auth/**
    - packages/core/src/omniscience_core/db/models.py
    - packages/core/src/omniscience_core/db/schemas.py
    - packages/core/alembic/versions/*
    - docs/api/mcp.md
    - docs/freshness-and-lineage.md
    - docs/roadmap.md
    - .mcp/**
    - scripts/check_mcp_contract_drift.py
    - tests/test_mcp_contract*.py
    - tests/test_mcp_token_profile_v1.py
    - tests/test_mcp*.py
    - tests/test_token*.py
    - tests/test_auth*.py
    - tests/test_tenant_isolation*.py
    - .github/workflows/ci.yml
  exclude:
    - docs/decisions/**
    - docs/specs/**
    - specs/**
    - graphify-out/**
    - helm/**
    - terraform/**
    - docker-compose*.yml
acceptanceCriteria:
  - id: AC-MCP-1
    requirement: Runtime, manifest, schemas, contract_info, docs, and catalogs publish exact MCP 1.0.0 and fifteen tools
    probe: P-MCP-1+P-MCP-2+P-MCP-3+P-MCP-10
    expected: all content digests and tool names agree offline
    groundTruth: committed artifacts and FastMCP AST registry
  - id: AC-MCP-2
    requirement: Every successful legacy payload gains schema-valid token-derived meta without top-level renames
    probe: P-MCP-4
    expected: v0 payload keys remain compatible and v1 meta validates
    groundTruth: contract schemas and per-tool contract fixtures
  - id: AC-MCP-3
    requirement: Freshness and consistency reflect only used evidence and require fallback when unsuitable
    probe: P-MCP-5+P-MCP-6+P-MCP-7
    expected: fresh/stale/unknown/degraded and projection lag produce deterministic outcomes
    groundTruth: seeded source lineage, ledger versions, and projection observations
  - id: AC-MCP-4
    requirement: Bootstrap tokens use exact workspace-bound search-only 30-day profile with audited 24-hour rotation and revoke
    probe: P-MCP-8+P-MCP-9
    expected: valid profile passes and every legacy/broader/unbounded/revoked profile fails closed
    groundTruth: authenticated token records and audit trail
  - id: AC-MCP-5
    requirement: Pinned consumers continue from direct authoritative sources on contract or freshness failure
    probe: P-MCP-11+P-MCP-12
    expected: live severance drill continues materialized work or parks safely
    groundTruth: omnius and SRE consumer drill evidence
rollback:
  kind: revert-pr
  probe: disable-v1-canary-and-require-direct-source-fallback
---

## Outcome

Ship MCP contract `1.0.0` as the first bounded slice of issue #350. Publish content-addressed schemas
and tool registry, authenticated `contract_info`, additive freshness/consistency/fallback metadata, and
the exact `omniscience-mcp-read-v1` token profile. Pin the canary in omnius only after skew and security
tests pass.

## Out of scope

- production HA, EKS/application replica topology, and priority classes
- backup/restore infrastructure and destructive restore drills
- shared admission/rate-limit backend and read scaling
- OAuth 2.1, passport propagation, or policy-engine discovery
- promoting this task to terminal state without live severance and human verification evidence

The human-ready revision is immutable during execution. Implementation cannot change this task's
requirements, probes, scope, fallback, or governing contracts.
