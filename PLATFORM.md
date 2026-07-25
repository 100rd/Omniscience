# Synchronized platform — Omniscience

Omniscience is the shared, read-only knowledge plane and MCP producer for the synchronized platform. The
canonical cross-repository ADR/SPEC plan is:

<https://github.com/100rd/genai-enablement/blob/main/docs/synchronized-platform/README.md>

In a sibling workspace it is normally available at
`../genai-enablement/docs/synchronized-platform/README.md`.

The full plan explains why and when components connect. Omniscience implementation authority remains
local:

```text
accepted genai-enablement ADR
        -> accepted Omniscience adoption ADR
        -> specs/SPEC-INDEX.md capability contract
        -> docs/specs/ one human-ready immutable task
        -> docs/specs/execution-index.json terminal evidence
```

## Start one independent Omniscience task

1. Read this file and the canonical plan to identify the cross-repository work package.
2. Read the [local ADR index](docs/decisions/README.md) and
   [capability-SPEC index](specs/SPEC-INDEX.md).
3. Claim exactly one [task SPEC](docs/specs/README.md); its include/exclude paths and acceptance probes
   are the writable execution boundary.
4. Keep ready task revisions immutable. Write terminal evidence beside them in
   `docs/specs/execution-index.json`.
5. Do not edit `genai-enablement` or `omnius` from an Omniscience task. Publish producer artifacts or
   read-only conformance fixtures; consumers return their own receipts from their repositories.

## Local synchronized-platform contracts

| Cross-repository package | Omniscience contract | Current boundary |
|---|---|---|
| `SP-10` MCP v1 producer | `SPEC-MCP` + `gh-issue-350-mcp-v1` | repo-local contract implementation merged; consumer pin, live canary/severance and human verification remain |
| `SP-12` consumer severance | `gh-issue-350-consumer-severance` | producer fixtures/verifier merged; Omnius/SRE receipts and live drill remain external |
| `SP-61` knowledge-plane PII boundary | `SPEC-PII` + `task-sp-61-pii-wall-pw0` | ready for fail-closed non-live development; no profile or data path is active |
| `SP-81` management-context producer | `SPEC-MCTX` + `task-sp-81-management-context-v1` | ready for bounded non-live producer development; no consumer/live authority |
| `SP-86` management-readonly release | `SPEC-MCP`, `SPEC-PII`, `SPEC-MCTX` + `task-sp-86-management-readonly-release` | ready to publish one language-neutral immutable release candidate; independent of concurrent Barbarossa SP-90, while SP-87/SP-88 consumer receipts and activation remain external |
| issue-350 HA | `gh-issue-350-production-ha` | portable profile merged; live fault-domain proof and stateful topology entitlements remain unverified |
| issue-350 recovery | `gh-issue-350-backup-restore` | safety harness merged; every destructive drill still needs separate exact human approval |
| issue-350 scaling | `gh-issue-350-read-scaling-priority` | controller/qualification logic merged; shared-backend selection, dispatch wiring and multi-replica proof remain |

The complete ready capability set is `SPEC-IN`, `SPEC-SOT`, `SPEC-ACL`, `SPEC-EV`, `SPEC-OPS`,
`SPEC-KP`, `SPEC-MCP`, `SPEC-PII`, and `SPEC-MCTX`. Their exact dependencies are authoritative in
[`specs/SPEC-INDEX.md`](specs/SPEC-INDEX.md). PII and management-context readiness authorize only their
exact non-live tasks.

## Knowledge-plane boundary

Omniscience returns cited, lineage-aware evidence. It is never the sole correctness, authorization,
merge, apply, incident-action, or terminal-workflow oracle. Consumers must honor freshness,
consistency, fallback, tenant, and contract pins and must degrade to direct authoritative sources or
park safely.

Portfolio state and accepted cross-repository decisions cannot promote an Omniscience task to
`implemented` or `verified`; only component-owned execution evidence can do that.

## Initial runtime profile

Accepted local ADR-0022 adopts `management-readonly-v1`. Omniscience publishes read-only evidence,
management context, knowledge-quality and non-identifying PW0 projections to exact Barbarossa and
Platform Portal consumers. Omnius remains part of the target architecture but is not deployed, not a
consumer gate, and not represented by a live mock in this profile.

The profile does not make Omniscience authoritative for Reliability. Barbarossa retains direct-source
operation and typed severance; Portal releases owner panels independently and displays Omnius as
`not_deployed`.

Central ADR-0022 selects Go for the Barbarossa production runtime. That decision does not change the
Omniscience implementation or producer schema: SP-86 publishes language-neutral contracts and may run
in parallel with SP-90. SP-87 is responsible for binding both immutable receipts.

## PII Wall boundary

Omniscience owns the earliest platform data gate: classification and admission must occur before
ordinary raw-document persistence, parsing, chunking, embedding, graph/vector projection, archive, or
external provider egress. It also owns retrieval revalidation and exact deletion/retention coverage
across its stores.

The accepted local contract is [`SPEC-PII`](specs/SPEC-PII-pii-wall-ingestion-lifecycle.md). It can be
implemented independently inside this repository under ADR-0020 and the exact ready
`task-sp-61-pii-wall-pw0`,
then publish non-identifying `SanitizationReceipt`, `PrivacyCoverage`, and `DeletionReceipt` artifacts.
Neither Omnius nor Platform Portal is permitted to classify or clean Omniscience data on its behalf.

## Local UI boundary

Omniscience remains MCP/API-first and may expose a small standalone operational UI for sources,
ingestion/indexing, storage, freshness, PII quarantine/coverage for its own stores, MCP health, and
component-local maintenance. It is not the platform-wide dashboard or shared control shell.

Cross-component maps, correlated component detail, synchronized work packages, and delegated controls
belong to [Platform Portal](https://github.com/100rd/platform-portal/blob/main/VISION.md). Omniscience
publishes typed read/action projections with workspace, revision, freshness, coverage, and source-health
provenance. Portal loss cannot block local operation; Omniscience loss becomes an explicit unavailable
portal projection rather than a fabricated healthy or empty state.
