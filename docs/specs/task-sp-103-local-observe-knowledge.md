---
id: task-sp-103-local-observe-knowledge
title: Provide isolated synthetic knowledge and scoped reads for local Observe
status: ready
approvedBy: platform-owner-via-implementation-request
approvedAt: 2026-11-06
repo: 100rd/Omniscience
governingAdrs: [genai-enablement/ADR-0025]
evidenceDestination: genai-enablement/docs/synchronized-platform/releases/management-local-observe-v1/
scope:
  include: [apps/server/src/omniscience_server/local_observe*, apps/server/src/omniscience_server/routes/tokens.py, apps/server/src/omniscience_server/mcp/mount.py, packages/core/src/omniscience_core/config.py, deploy/compose/management-local-observe-v1/**, tests/**]
  exclude: [graphify-out/**, infra/terraform/**]
acceptanceCriteria:
  - {id: AC-SP103-1, requirement: Bootstrap separate safe workspace graphs and corpus, probe: two-workspace-live-reads, expected: repeat seed preserves data and foreign reads disclose no entity, groundTruth: source revision and scoped actual owner replies}
  - {id: AC-SP103-2, requirement: Authenticate MCP and constrain broker authority, probe: bounded-token-profile-and-transport, expected: exact read scope and expiry with no unauthenticated admin bootstrap, groundTruth: positive and negative live calls}
rollback: {kind: stop-new-profile, probe: preserve-distinct-owner-volumes}
---

This task selects `management-local-observe-v1` only. Source image readiness,
tenant isolation and local activation are tracked separately from SP-86 OCI
publication and previous profile receipts. No production or HA qualification.
