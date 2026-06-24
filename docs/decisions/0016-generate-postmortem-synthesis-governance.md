# ADR-0016: generate_postmortem — Synthesis Tool Governance

**Status**: Accepted  
**Date**: 2026-06-25  
**Author**: Engineering (consilium-v9 P0 remediation, AP7)

---

## Context

The `generate_postmortem` MCP tool (issue #232) is registered on the
Omniscience MCP surface alongside retrieval tools (`search`,
`get_entity`, `get_related_entities`, etc.).  Unlike retrieval tools,
`generate_postmortem` **synthesises new content** from the graph: it
assembles a templated post-mortem document and extracts `FollowUp`
entities into the knowledge graph.

AP7 (consilium-v9 Opus review) requires that every MCP-registered tool
be explicitly categorised as either **retrieval** or **synthesis**, with
synthesis tools carrying a documented governance record explaining:

1. Why synthesis is permitted on the MCP surface.
2. What writes the tool performs and under what conditions.
3. The review boundary separating synthesis output from raw graph writes.

---

## Decision

`generate_postmortem` is categorised as a **synthesis tool** with the
following governance constraints:

### 1. Category: synthesis

The tool reads graph state (via the bitemporal timeline) and produces
a new artefact (the post-mortem document).  It also extracts structured
`FollowUp` entities and persists them through the standard ingestion
path (outbox → outbox_consumer → stores).

### 2. Write boundary

All writes performed by `generate_postmortem` go through the
**standard ingestion outbox** (ADR-0012).  The tool does **not** call
graph-store or vector-store APIs directly.  This preserves the
single-writer invariant (AP1, consilium-v8) and the outbox ordering
guarantees.

Specifically:
- `generate_postmortem` calls `generate_postmortem()` from
  `omniscience_server.postmortem` which delegates to
  `PostmortemGenerator.generate()`.
- Follow-up entity extraction (issue #232 §E) emits through the
  `omniscience_server.postmortem.followups` module which writes
  to the outbox table — not to Neo4j or Qdrant directly.

### 3. Scope restriction

The tool requires a **workspace-scoped bearer token** with the `search`
scope.  Cross-workspace synthesis is not permitted; the workspace_id
is taken from the token, not from tool arguments.

### 4. Why synthesis is permitted on the MCP surface

Post-mortem generation is a high-value SRE workflow that benefits from
direct MCP integration (no round-trip through a separate REST endpoint).
The synthesis output is a document artefact and structured entity
updates — both are idempotent-safe and audited via the ingestion pipeline.
The synthesis is **human-in-the-loop** by design: the generated document
is returned to the MCP caller for review before any action is taken.

### 5. Arch-test marker

The AP7 architecture conformance test
(`tests/conformance/test_ap7_arch_invariants.py`) explicitly allows
`generate_postmortem` on the MCP surface by name in a documented
allowlist.  Any new synthesis tools added to the surface must:

a. Be added to the allowlist with a comment referencing this ADR.
b. Have a corresponding governance section in this ADR (or a new ADR).

---

## Consequences

- `generate_postmortem` remains on the MCP surface with the
  `category="synthesis"` label in the tool registry.
- The AP7 arch-test passes by explicitly categorising the tool as
  synthesis in the allowlist, rather than silently skipping it.
- Future synthesis tools (e.g. `generate_runbook`, `suggest_remediation`)
  must follow the same governance path before being added to the MCP
  surface.
- Retrieval tools (`search`, `get_entity`, `get_related_entities`,
  `list_entities`, `list_sources`, `source_stats`, `resolve_incident`,
  `blast_radius`, `replay_context`, `incident_timeline`, `suggest_runbook`,
  `find_similar_incidents`) are read-only and require no write governance.
