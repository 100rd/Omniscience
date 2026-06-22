# Action Point 2 (P0) Report

## Analysis
The objective was to introduce a read-time consistency contract (`min-applied-version` and `staleness`) for evidence returned by MCP queries, particularly addressing risk R1: heterogeneous versions coming from three different stores (Graph, Vector, etc.) undermining confidence.

To achieve this:
1. `staleness` (difference between effective time and time of projection insertion) and `applied_version` (SoT version of projection) needed to be tracked for evidence entities:
   - `SearchHit` (Vector search results)
   - `AlertSummary`, `ResourceSummary`, `PrSummary`, `SlackThreadSummary` (Incident resolution Graph traverse results)
2. `min_applied_version` needed to be computed by finding the minimum `applied_version` among all pieces of evidence returned in the response payloads (`SearchResult`, `ResolveIncidentResponse`).
3. Added these fields to the corresponding Pydantic models so the MCP API directly exposes them to the client.

## Implementation Details
1. **Pydantic Models (`packages/retrieval/src/omniscience_retrieval/models.py`)**:
   - Added `staleness: float | None` and `applied_version: int | None` to `SearchHit`.
   - Added `min_applied_version: int | None` to `SearchResult`.
2. **Incident Pydantic Models (`packages/retrieval/src/omniscience_retrieval/incidents/resolution.py`)**:
   - Added `staleness` and `applied_version` to `AlertSummary`, `ResourceSummary`, `PrSummary`, `SlackThreadSummary`.
   - Added `min_applied_version` to `ResolveIncidentResponse`.
   - Extracted helper `_compute_staleness` to find delta between `effective_as_of` and the node's `recorded_at`.
   - Gathered versions from all resolved node summaries and set `min_applied_version` in `build_resolve_response`.
3. **Graph Storage Layer (`packages/core/src/omniscience_core/storage/graph.py` & `postgres_only_store.py` & `neo4j/mappers.py`)**:
   - Added `version: int | None` to `EntityNodeView`.
   - Configured mappers and parsers in Postgres (`version=ent.version`, `version=seed_ent.version`, etc.) and Neo4j (`version=record.get("version")`) to carry over the `version` attribute from the database.
4. **Vector Storage Layer (`packages/index/src/omniscience_index/stores/qdrant_store.py` & `postgres_only_store.py`)**:
   - Mapped `payload.get(PAYLOAD_DOC_VERSION)` to `applied_version` and calculated staleness based on `indexed_at` in `SearchHit` builders.
   - Built `min_applied_version` on the overall `SearchResult`.
5. **Federated and Graph RAG composition (`federation.py` & `graph_rag.py`)**:
   - Carried the calculation for `min_applied_version` based on top level hits array merged from different sources.

## Test Run
The full test suite was executed via `pytest` to confirm backward compatibility and ensure no regression in current functionality. 

The task is successfully implemented.
