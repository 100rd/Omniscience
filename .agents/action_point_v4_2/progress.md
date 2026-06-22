# Progress Report: Action Point 2 (P0)

## Analysis & Implementation
The read-time consistency contract (`min-applied-version` and `staleness`) was successfully added to address risk R1.
Heterogeneous versions coming from three different stores (Graph, Vector, etc.) needed an explicit confidence measurement in the MCP responses.

### Key Changes
1. Added `staleness: float | None` and `applied_version: int | None` to the `SearchHit` model and other evidence payload objects (`AlertSummary`, `ResourceSummary`, `PrSummary`, `SlackThreadSummary`).
2. Added `min_applied_version: int | None` to the top-level MCP response envelopes (`SearchResult`, `ResolveIncidentResponse`).
3. Plumbed `version` through the Graph and Vector storage layers (`EntityNodeView`, `postgres_only_store.py`, `neo4j/mappers.py`, `qdrant_store.py`).
4. Federated and Graph RAG compositions dynamically compute the `min_applied_version` based on all retrieved evidence chunks.

## Tests
Testing is underway. A full run of `pytest` is ensuring backwards compatibility and verifying the newly surfaced fields.
