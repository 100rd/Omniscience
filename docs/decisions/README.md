# Architecture Decision Records

ADRs are the human decision plane for Omniscience. Accepted decisions are immutable in
substance; a reversal or boundary change requires a new ADR. Capability contracts in
`/specs` translate decisions for agents, and `docs/specs/` contains bounded task contracts.

Statuses: `Proposed`, `Accepted`, `Implemented`, `Superseded`, `Deprecated`.

| ID | Decision | Status |
|---|---|---|
| ADR-0001 | Language and stack | Accepted |
| ADR-0002 | Connector framework vs SDK | Accepted |
| ADR-0003 | LangGraph for agentic connector discovery | Accepted |
| ADR-0004 | Staged retrieval strategy | Accepted |
| ADR-0005 | Neo4j graph store | Implemented |
| ADR-0006 | Qdrant vector store | Implemented |
| ADR-0007 | Kubernetes operator architecture | Proposed |
| ADR-0008 | Bitemporal Neo4j schema | Implemented |
| ADR-0009 | Retention tiering | Implemented |
| ADR-0010 | Server-side emitter deduplication | Accepted |
| ADR-0011 | Kubernetes agentic deprecation | Accepted |
| ADR-0012 | Neo4j relationship-property indexes | Accepted |
| ADR-0013 | Neo4j metadata JSON encoding | Accepted |
| ADR-0014 | AWS dual-mechanism ingestion | Accepted |
| ADR-0015 | multiqlti as MCP consumer and ingestion source | Accepted |
| ADR-0016 | Postmortem synthesis governance | Accepted |
| ADR-0017 | Per-source epoch pin | Accepted |
| ADR-0018 | Rebuild direct-write DR exception | Accepted |
| [ADR-0019](0019-dark-factory-sdd-and-knowledge-plane-boundary.md) | Dark Factory SDD and knowledge-plane boundary | Accepted |

`ADR-0018` was originally committed with the duplicate id `ADR-0015`. The rename is a
registry repair, not a change to its accepted decision.
