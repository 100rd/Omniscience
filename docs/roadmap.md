# Omniscience component roadmap

**Current release:** `v0.5.0`
**Current engineering slice:** MCP contract `1.0.0`
**Portfolio status:** `issue-350-ready-queue`

This roadmap is the compact component view. Mutable issue scheduling lives in
[GitHub milestones](https://github.com/100rd/Omniscience/milestones); cross-repository status,
dependencies, owners, and readiness blockers live in the
[genai-enablement portfolio](https://github.com/100rd/genai-enablement/blob/main/docs/portfolio/omniscience.md).
Capability/task contracts and execution evidence remain canonical in this repository.

## Current cycle: issue #350

| Slice | State | Exit boundary |
|---|---|---|
| mcp-v1 | `ready/in-progress` | Stable manifest/schemas, 15-tool registry, `contract_info`, freshness/fallback metadata, exact token profile, consumer pin and severance evidence |
| production-ha | `ready/queued` | Fail-closed HA profile plus redundant application, PostgreSQL, JetStream, and projection-topology qualification |
| backup-restore | `ready/queued` | PostgreSQL PITR safety harness and a measured isolated restore/convergence drill |
| read-scaling-priority | `ready/queued` | Shared admission, horizontal-read qualification, overload tests, and bounded SRE priority lane |
| consumer-severance | `ready/queued` | Producer fixtures, pinned Omnius/SRE receipts, and a safe live direct-source fallback drill |

The queue is dependency ordered: `mcp-v1` precedes `consumer-severance` and `production-ha`;
`production-ha` precedes `backup-restore` and `read-scaling-priority`. Ready HA/DR/scaling specs
authorize repository-local implementation and disposable qualification, not production mutation.

## Backlog governance

- [#230](https://github.com/100rd/Omniscience/issues/230): 12 of 13 delivery tasks are complete; keep
  the epic open with only PM task [#242](https://github.com/100rd/Omniscience/issues/242) remaining.
- [#244](https://github.com/100rd/Omniscience/issues/244): remains `pre-plan` pending a separate
  competing-hypothesis discovery.
- [#350](https://github.com/100rd/Omniscience/issues/350): parent for the five bounded task SPECs above.
- [#355](https://github.com/100rd/Omniscience/issues/355): implementation is terminally evidenced in
  [`docs/specs/execution-index.json`](specs/execution-index.json); the ready task SPEC is immutable.

## Release and contract policy

- Product release `v0.5.0` is the current immutable release evidence.
- MCP v0 remains the implemented public surface until the ready `1.0.0` task is materialized and
  verified. After that cutover, v1 supersedes v0; compatible additions follow semver and breaking wire
  changes require a new major contract.
- MCP v1 contract conformance must compare runtime, manifest, schemas, SDK dependency/lock, docs, SPEC
  indexes, roadmap, execution evidence, and `.mcp` catalogs through one offline drift checker.
- MCP v1 requires a fail-closed portable canary verifier for an exact materialized bundle and canonical
  no-redirect `/mcp/` Streamable HTTP endpoint; implementing the mechanism is not a deployed canary or
  consumer activation.
- A canary, exact consumer pin, live severance drill, and human verification evidence precede terminal
  MCP v1 task state.
