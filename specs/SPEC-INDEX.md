# Omniscience Capability SPEC Index

Cross-repository dependencies and independently claimable work packages are discoverable from the root
[`PLATFORM.md`](../PLATFORM.md). This file remains the authoritative complete Omniscience capability
inventory.

Capability SPECs translate accepted human ADR decisions into reusable agent-executable contracts.
They do not authorize one task; bounded work lives in `docs/specs/` and cites these contracts.

All initial SDD specs are `ready` under accepted ADR-0019 and explicit owner approval recorded in
each contract. `SPEC-PII` and `SPEC-MCTX` are separately ready under accepted ADR-0020/0021 and the
2026-07-21 owner development decision.
Implementation and verification remain separate states backed by their probes.

| ID | Capability | Primary boundary |
|---|---|---|
| [SPEC-IN](SPEC-IN-task-spec-intake.md) | Task SPEC intake | human-ready immutable work contract |
| [SPEC-KP](SPEC-KP-dark-factory-knowledge-plane.md) | Dark Factory knowledge plane | read-only, severable, never the oracle |
| [SPEC-EV](SPEC-EV-retrieval-evidence-contract.md) | Retrieval evidence | citations, lineage, freshness, confidence, degradation |
| [SPEC-SOT](SPEC-SOT-ledger-and-projections.md) | Ledger and projections | Postgres authority, outbox, Neo4j/Qdrant convergence |
| [SPEC-ACL](SPEC-ACL-workspace-isolation.md) | Workspace isolation | server-derived tenant scope and non-disclosure |
| [SPEC-OPS](SPEC-OPS-operational-evidence.md) | Operational evidence | CI, conformance, benchmark, DR, documentation consistency |
| [SPEC-MCP](SPEC-MCP-stable-contract-v1.md) | Stable MCP v1 | content-addressed wire contract, freshness, token profile, severance |
| [SPEC-PII](SPEC-PII-pii-wall-ingestion-lifecycle.md) | PII Wall | pre-store/pre-embedding admission and cross-store lifecycle coverage |
| [SPEC-MCTX](SPEC-MCTX-management-context-producer.md) | Management context producer | cited context and orthogonal knowledge-quality conditions; never management authority |

## Dependency order

```text
ADR-0019
   |
   +--> SPEC-IN
   +--> SPEC-SOT --> SPEC-EV --> SPEC-KP
   +--> SPEC-ACL --------^       |
   +--> SPEC-OPS <---------------+
   +--> SPEC-EV + SPEC-KP + SPEC-ACL + SPEC-OPS --> SPEC-MCP
   +--> SPEC-ACL + SPEC-SOT + SPEC-EV + SPEC-OPS --> SPEC-PII
   +--> SPEC-ACL + SPEC-EV + SPEC-KP + SPEC-MCP + SPEC-OPS --> SPEC-MCTX
```

## Capability SPEC states

```text
draft -> ready -> implemented -> verified -> superseded
```

Only a human/CODEOWNER may mark `ready`. Only independent evidence may mark `verified`.
Requirements and their probes keep stable ids across compatible revisions.
