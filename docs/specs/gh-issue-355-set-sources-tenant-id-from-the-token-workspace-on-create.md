---
title: "Set sources.tenant_id from the token workspace on create"
id: gh-issue-355
status: draft
source: { kind: github, ref: "355", url: "https://github.com/100rd/Omniscience/issues/355" }
governingAdrs:
  - Omniscience/ADR-0019
capabilitySpecs:
  - SPEC-IN
  - SPEC-ACL
sddMode: standard
repo: 100rd/Omniscience
scope:
  include:
    - apps/server/src/omniscience_server/rest/sources.py
    - tests/**
  exclude:
    - docs/decisions/**
    - specs/**
acceptanceCriteria:
  - id: AC-1
    requirement: POST /api/v1/sources sets tenant_id from the authenticated token workspace
    probe: source-create-token-workspace
    expected: tenant_id is non-NULL and equals the token workspace
    groundTruth: authenticated server principal and persisted Postgres row
  - id: AC-2
    requirement: a created source is visible only to its workspace-scoped sync
    probe: source-create-sync-workspace-isolation
    expected: owner sync succeeds; foreign workspace receives non-disclosing not-found
    groundTruth: persisted source row and authenticated integration responses
rollback:
  kind: revert-pr
  probe: source-create-legacy-contract
---

## Problem
`create_source` leaves `sources.tenant_id` NULL, so the source is invisible to workspace-scoped sync.

## Scope

Set the source tenant from server-derived authentication context and add positive/cross-workspace
regression coverage. This draft cannot become ready until ADR-0019 and SPEC-IN/ACL are human-ready.

## Out of scope
None specified.
