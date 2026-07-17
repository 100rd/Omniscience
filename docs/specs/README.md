# Task SPEC and execution index

GitHub issues and reviews are mutable intake. Only a human-ready immutable task SPEC authorizes bounded
execution under SPEC-IN. Terminal state comes from the separate
[`execution-index.json`](execution-index.json), not an issue checkbox or edits to a ready task file.

## Issue #350 slices

Issue [#350](https://github.com/100rd/Omniscience/issues/350) is a parent initiative with five bounded
slices. The five task revisions are human-ready, but their execution boundaries differ: MCP v1 and
consumer conformance may implement repository-local behavior, while HA, restore, and scaling first
produce fail-closed qualification evidence and cannot mutate production without separate authority.

| Task | Status | Purpose |
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

## Terminal execution evidence

| Task | State | Evidence |
|---|---|---|
| gh-issue-355 | `implemented` | [execution-index.json](execution-index.json): PR #359, implementation and merge commits, reported GitHub rollup 13 SUCCESS / 2 SKIPPED / 0 other |

The ready [issue #355 task SPEC](gh-issue-355-set-sources-tenant-id-from-the-token-workspace-on-create.md)
is immutable and remains unchanged. Evidence is adjacent rather than written into its execution
contract. The structured record also retains reported RED (2 failed / 15 passed), GREEN (17 passed),
broader (102 passed), and protected CI (3535 passed / 6 skipped / 88% coverage) evidence without
misrepresenting the rollup as one local pytest command.

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

Only a component CODEOWNER may set `ready`. Implementation does not imply independent verification.
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
