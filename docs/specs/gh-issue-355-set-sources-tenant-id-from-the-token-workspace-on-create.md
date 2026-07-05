---
title: "Set sources.tenant_id from the token workspace on create"
status: ready
source: { kind: github, ref: "355", url: "https://github.com/100rd/Omniscience/issues/355" }
repo: "/Users/lord/Develop/multi-team-agentic/project/Omniscience"
acceptanceCriteria:
  - "POST /api/v1/sources sets tenant_id from the authenticated token workspace (never NULL)"
  - "a regression test proves a created source is visible to a workspace-scoped sync"
---

## Problem
`create_source` leaves `sources.tenant_id` NULL, so the source is invisible to workspace-scoped sync.

## Scope
TBD

## Out of scope
None specified.
