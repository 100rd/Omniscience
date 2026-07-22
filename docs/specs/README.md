# Task SPEC and execution index

Start synchronized-platform work at the root [`PLATFORM.md`](../../PLATFORM.md), then claim exactly one
Omniscience-owned task here. The cross-repository plan orders producer/consumer work but does not grant
sibling writes or replace this execution index.

GitHub issues and reviews are mutable intake. Only a human-ready immutable task SPEC authorizes bounded
execution under SPEC-IN. Terminal state comes from the separate
[`execution-index.json`](execution-index.json), not an issue checkbox or edits to a ready task file.

## Issue #350 slices

Issue [#350](https://github.com/100rd/Omniscience/issues/350) is a parent initiative with five bounded
slices. The five task revisions are human-ready, but their execution boundaries differ: MCP v1 and
consumer conformance may implement repository-local behavior, while HA, restore, and scaling first
produce fail-closed qualification evidence and cannot mutate production without separate authority.

All five now have adjacent `implemented` records in `execution-index.json`. The task files remain
immutable `ready` contracts by design; none is `verified`, because each retains a live or
owner-controlled decision return that merged repository-local code cannot satisfy.

| Task | Contract status | Purpose |
|---|---|---|
| [gh-issue-350-mcp-v1](gh-issue-350-mcp-contract-v1.md) | `ready` | Stable contract `1.0.0`, freshness/fallback metadata, token profile, skew gates |
| [gh-issue-350-production-ha](gh-issue-350-production-ha.md) | `ready` | Portable HA profile, policy validation, and disposable failure qualification |
| [gh-issue-350-backup-restore](gh-issue-350-backup-restore.md) | `ready` | PostgreSQL PITR safety harness and isolated destructive restore/convergence qualification |
| [gh-issue-350-read-scaling-priority](gh-issue-350-read-scaling-priority.md) | `ready` | Shared admission interface, horizontal-read qualification, and bounded SRE lane |
| [gh-issue-350-consumer-severance](gh-issue-350-consumer-severance.md) | `ready` | Producer fixtures plus read-only Omnius/SRE fallback receipts and safe live drill |

## Issue #350 execution order

```text
mcp-v1
   |
   +--> consumer-severance
   |
   +--> production-ha
            |
            +--> backup-restore
            +--> read-scaling-priority
```

One agent should claim one immutable task revision at a time. A task may return RED or
`decision-required` when external evidence is absent; it cannot widen itself, edit another repository,
or reinterpret readiness as production/cloud/destructive authority.

## Synchronized-platform development tasks

| Task | Contract status | Purpose |
|---|---|---|
| [task-sp-61-pii-wall-pw0](task-sp-61-pii-wall-pw0.md) | `ready` | fail-closed repository-local PW0 propagation boundary and fixtures |
| [task-sp-81-management-context-v1](task-sp-81-management-context-v1.md) | `ready` | cited management context and knowledge-quality producer v1 |

Both tasks are human-ready for their exact non-live repository scopes. They authorize no production
policy/profile, live personal data, credential, provider, consumer pin, deployment or external effect.

## Terminal execution evidence

| Task | State | Evidence |
|---|---|---|
| gh-issue-355 | `implemented` | [execution-index.json](execution-index.json): PR #359, implementation and merge commits, reported GitHub rollup 13 SUCCESS / 2 SKIPPED / 0 other |
| gh-issue-350-mcp-v1 | `implemented`, not verified | PR #361; repo-local AC-MCP-1..4 merged; live consumer pin/canary/severance and human verification remain |
| gh-issue-350-consumer-severance | `implemented`, not verified | PR #362; producer matrix/verifier merged; immutable Omnius/SRE receipts and live severance drill remain |
| gh-issue-350-production-ha | `implemented`, not verified | PR #363; fail-closed portable HA profile merged; live fault-domain evidence and topology entitlements remain |
| gh-issue-350-backup-restore | `implemented`, not verified | PR #364; safety/qualification harness merged; approved isolated destructive drill and measured RPO/RTO remain |
| gh-issue-350-read-scaling-priority | `implemented`, not verified | PR #365; admission/controller/qualification logic merged; concrete shared backend and multi-replica evidence remain |

Every ready task SPEC above is immutable and remains unchanged. Evidence is adjacent rather than written
into its execution contract. The structured index retains exact PR/commit, check-rollup, test and
outstanding-boundary detail without misrepresenting merged implementation as independent verification.

## Historical implemented prose

These pre-SDD documents describe already implemented work. Their bodies remain historical and are not
active backlog or executable ready contracts.

| Document | State |
|---|---|
| aws-config-phase1.md | `historical-implemented` |
| discovery-sync-worker.md | `historical-implemented` |

## State model

```text
draft -> ready -> implemented -> verified -> superseded
```

Only a component CODEOWNER may set `ready`. The platform/Omniscience owner approved the two SP-61/SP-81
development tasks on 2026-07-21. Implementation does not imply independent verification.
Agents cannot modify a ready revision's governing ADRs/SPECs, scope, probes, fixtures, policy, or waiver
state while executing it.

## Template

```yaml
---
id: stable-task-id
title: Bounded outcome
status: draft
readiness:
  approvedBy: null
  approvedAt: null
source: { kind: github|human, ref: source-id, url: source-url }
governingAdrs: [Omniscience/ADR-XXXX]
capabilitySpecs: [SPEC-XX]
sddMode: quick|standard|full
repo: 100rd/Omniscience
scope:
  include: []
  exclude: [docs/decisions/**, specs/**]
acceptanceCriteria:
  - id: AC-1
    requirement: observable condition
    probe: registered-probe-id
    expected: typed expected result
    groundTruth: external source of truth
rollback:
  kind: revert-pr|disposable|compensator
  probe: registered-rollback-probe
---
```

Portfolio status and cross-repository dependencies live in
[`genai-enablement`](https://github.com/100rd/genai-enablement/blob/main/docs/portfolio/omniscience.md).
