# Omniscience component roadmap

**Current release:** `v0.5.0`
**Current engineering slice:** MCP contract `1.0.0`
**Portfolio status:** `issue-350-implemented-unverified`

This roadmap is the compact component view. Mutable issue scheduling lives in
[GitHub milestones](https://github.com/100rd/Omniscience/milestones); cross-repository status,
dependencies, owners, and readiness blockers live in the
[genai-enablement portfolio](https://github.com/100rd/genai-enablement/blob/main/docs/portfolio/omniscience.md).
Capability/task contracts and execution evidence remain canonical in this repository.

## Current cycle: issue #350

| Slice | State | Exit boundary |
|---|---|---|
| mcp-v1 | `implemented/unverified` | Repo-local v1 contract merged; exact consumer pin, live canary/severance drill and human verification remain |
| production-ha | `implemented/unverified` | Portable fail-closed profile merged; live failure-domain/SLO evidence and topology entitlements remain |
| backup-restore | `implemented/unverified` | Safety/qualification harness merged; approved isolated destructive restore and measured RPO/RTO remain |
| read-scaling-priority | `implemented/unverified` | Admission/controller/qualification logic merged; shared backend, dispatch wiring and multi-replica proof remain |
| consumer-severance | `implemented/unverified` | Producer fixtures/verifier merged; immutable Omnius/SRE receipts and live fallback drill remain |

The task contracts remain dependency ordered and immutable `ready`: `mcp-v1` precedes
`consumer-severance` and `production-ha`; `production-ha` precedes `backup-restore` and
`read-scaling-priority`. Their adjacent execution records are `implemented`, not `verified`.
HA/DR/scaling readiness and merged portable code still do not authorize production mutation.

## Backlog governance

- [#230](https://github.com/100rd/Omniscience/issues/230): 12 of 13 delivery tasks are complete; keep
  the epic open with only PM task [#242](https://github.com/100rd/Omniscience/issues/242) remaining.
- [#244](https://github.com/100rd/Omniscience/issues/244): remains `pre-plan` pending a separate
  competing-hypothesis discovery.
- [#350](https://github.com/100rd/Omniscience/issues/350): parent for the five bounded task SPECs above;
  all five have merged implementation evidence and outstanding decision returns in
  [`docs/specs/execution-index.json`](specs/execution-index.json).
- [#355](https://github.com/100rd/Omniscience/issues/355): implementation is terminally evidenced in
  [`docs/specs/execution-index.json`](specs/execution-index.json); the ready task SPEC is immutable.

## Release and contract policy

- Product release `v0.5.0` is the current immutable release evidence.
- MCP v1 is the implemented stable public repository contract and v0 is historical/superseded.
  Consumer activation still requires exact pins, a live canary/severance drill and human verification;
  compatible additions follow semver and breaking wire changes require a new major contract.
- MCP v1 contract conformance must compare runtime, manifest, schemas, SDK dependency/lock, docs, SPEC
  indexes, roadmap, execution evidence, and `.mcp` catalogs through one offline drift checker.
- MCP v1 requires a fail-closed portable canary verifier for an exact materialized bundle and canonical
  no-redirect `/mcp/` Streamable HTTP endpoint; implementing the mechanism is not a deployed canary or
  consumer activation.
- A canary, exact consumer pin, live severance drill, and human verification evidence precede terminal
  MCP v1 task state.
