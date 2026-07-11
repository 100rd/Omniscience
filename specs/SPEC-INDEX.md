# Omniscience Capability SPEC Index

Capability SPECs translate accepted human ADR decisions into reusable agent-executable contracts.
They do not authorize one task; bounded work lives in `docs/specs/` and cites these contracts.

All initial SDD specs are `draft` while ADR-0019 is proposed.

| ID | Capability | Primary boundary |
|---|---|---|
| [SPEC-IN](SPEC-IN-task-spec-intake.md) | Task SPEC intake | human-ready immutable work contract |
| [SPEC-KP](SPEC-KP-dark-factory-knowledge-plane.md) | Dark Factory knowledge plane | read-only, severable, never the oracle |
| [SPEC-EV](SPEC-EV-retrieval-evidence-contract.md) | Retrieval evidence | citations, lineage, freshness, confidence, degradation |
| [SPEC-SOT](SPEC-SOT-ledger-and-projections.md) | Ledger and projections | Postgres authority, outbox, Neo4j/Qdrant convergence |
| [SPEC-ACL](SPEC-ACL-workspace-isolation.md) | Workspace isolation | server-derived tenant scope and non-disclosure |
| [SPEC-OPS](SPEC-OPS-operational-evidence.md) | Operational evidence | CI, conformance, benchmark, DR, documentation consistency |

## Dependency order

```text
ADR-0019
   |
   +--> SPEC-IN
   +--> SPEC-SOT --> SPEC-EV --> SPEC-KP
   +--> SPEC-ACL --------^       |
   +--> SPEC-OPS <---------------+
```

## Capability SPEC states

```text
draft -> ready -> implemented -> verified -> superseded
```

Only a human/CODEOWNER may mark `ready`. Only independent evidence may mark `verified`.
Requirements and their probes keep stable ids across compatible revisions.

