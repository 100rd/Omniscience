# ADR 0015 — multiqlti as MCP consumer and ingestion source

- **Status**: Accepted
- **Date**: 2026-06-05
- **Supersedes**: none
- **Related**: multiqlti [Memory Architecture decision](https://github.com/100rd/multiqlti/blob/main/docs/decisions/memory-architecture.md); ADR [0004 — Retrieval strategy](0004-retrieval-strategy-staged.md); ADR [0002 — Connector framework vs SDK](0002-connector-framework-vs-sdk.md)

## Context

The sibling project **multiqlti** (`github.com/100rd/multiqlti`) is a multi-model AI
pipeline that runs 7 SDLC teams. Today it operates its own retrieval memory (pgvector RAG
in `server/memory/`) and its own incremental code indexer (`server/workspace/`). That
duplicates exactly what Omniscience is built to own: indexing sources into a causal,
temporal, semantic graph and serving retrieval over MCP.

multiqlti is, in MCP terms, precisely the "AI pipeline" client Omniscience's vision targets
("Claude Code, Cursor, internal agents, **or AI pipelines**"). It also exposes a **reverse
MCP server** and produces a rich operational stream — pipeline runs, per-stage outcomes,
failures, and decisions — that is itself valuable world-knowledge once indexed.

This ADR records Omniscience's side of a jointly-agreed memory architecture: which
responsibilities Omniscience accepts, and which it explicitly does not.

## Decision

### 1. multiqlti is a first-class MCP consumer of `search`

multiqlti's `Retriever` calls the Omniscience `search` tool (stdio or streamable-http) as
its world-knowledge backend, replacing its local pgvector path. No special API is added —
multiqlti uses the standard, scoped (`search`, `sources:read`) MCP surface. The stable
`retrieval_strategy` contract (ADR 0004 — unknown values downgrade to `hybrid`) means
multiqlti can be written today against v0.2 semantics and run against a v0.1 deployment.

### 2. The multiqlti workspace becomes an Omniscience connector

multiqlti **stops owning code/source indexing**. Each multiqlti workspace is ingested by
Omniscience through the existing **git / fs connectors** (ADR 0002), so a workspace is
indexed exactly once — here — and is queryable with the same `search`/graph/`as_of`
surface as every other source. This removes the duplicate vector stack.

### 3. multiqlti run/incident history is a new ingestion source

multiqlti's reverse-MCP / webhook stream (run started/failed, stage outcomes with error
text, decisions) is accepted as an **ingestion source** via the webhooks connector path
(cf. #19). This lets Omniscience answer "why did a similar pipeline fail before?" by linking
run history to code and infra — the same causal-graph value proposition, extended to agent
operations. Concrete source schema/connector is tracked as a follow-up issue, not built here.

### 4. Omniscience does NOT model agent experience

Capturing "what a pipeline agent learned" (lessons, write-back, a learning loop) stays
**out of scope** for Omniscience. It is read-only Insight Mode (see vision §4); modelling an
agent's own evolving experience is a write/stateful concern that belongs in multiqlti's
native lessons layer. Omniscience ingests *facts about runs* (as a source), not the agent's
*subjective lessons*.

## Consequences

- **Positive**: Omniscience gains a real, in-house pipeline consumer that exercises the MCP
  `search` contract and the connector framework end-to-end; a new high-value source class
  (operational run/incident history) that strengthens the causal graph; no new bespoke API.
- **Negative / risks**: a real downstream consumer (multiqlti) now depends on `search`
  availability and latency — reinforces the M2/M3 retrieval milestones as the critical path.
  The run-history source must respect retention tiering (ADR 0009) to avoid unbounded growth.
- **No architectural change** to Omniscience's stores, transports, or auth — this is an
  integration/consumer decision, satisfied by existing surfaces (MCP `search`, connectors,
  webhooks) plus one follow-up for the run-history source schema.

## Alternatives rejected

- **Bespoke multiqlti API on Omniscience** — rejected; the standard MCP `search` + connector
  surfaces are sufficient. Special-casing one consumer violates the MCP-first principle.
- **Let multiqlti keep its own index and federate results** — rejected; that is the
  duplicate-index status quo this decision exists to remove.
- **Absorb multiqlti's lessons/experience memory into Omniscience** — rejected; read-only
  Insight Mode does not model write-back agent experience (vision §4 product boundary).

## Follow-ups

- Issue: run/incident-history ingestion source schema + connector (webhooks path, ADR 0002 / #19).
- Coordinate the M2/M3 `hybrid search` milestone as the unblocker for multiqlti Track A.
