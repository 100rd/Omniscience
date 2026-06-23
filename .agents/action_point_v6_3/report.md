# Action Point 3 (P0): Parked Entity Propagation

## Goal
Ensure that the retrieval layer (`GraphRAGComposer`, `probabilistic_scoring.py`, `incidents/resolution.py`) is aware if an entity or edge is currently in the 'parked' state (DLQ). If an entity is parked, its confidence must be severely penalized or explicitly flagged as stale/provisional in the MCP API responses, because it is stuck in the outbox and out-of-sync with Postgres-SoT.

## Implementation Details

1. **Storage Layer**
   - Added `is_parked: bool = False` to `EntityNodeView` in `omniscience_core.storage.graph` to safely pass the parked state across module boundaries without leaking server concepts.

2. **Server & Domain Layer**
   - **`incidents.py`**: Intercepted the `EntityNodeView` instances returned from the `GraphStore` (the `seed` and `related` nodes) and populated their `is_parked` fields if their UUIDs exist in the `outbox_consumer_worker._parked_entities` map stored on `app.state`.
   - **`graph_rag.py`**: Added an optional `is_entity_parked_fn` to the constructor of `GraphRAGComposer`. Plumbed the state through the internal `_AnchorStageResult` so the `_run_merge_stage` can query whether a specific chunk belongs to a parked entity.
   - **`app.py`**: Reordered/Updated `app.py` wiring so that `GraphRAGComposer` could receive an anonymous function that dynamically inspects `app.state.outbox_consumer`.

3. **Probabilistic Scoring Layer**
   - **`probabilistic_scoring.py`**: 
     - Modified `calculate_probabilistic_confidence` to accept `is_parked: bool = False`. If true, severely penalizes the confidence score (`raw_confidence *= 0.1`).
     - Modified `calculate_probabilistic_incident_confidence` to check `alert.is_parked`, `classified.responsible_pr.is_parked`, and `classified.target_resource.is_parked`. If any entity is parked, it applies a 10x penalty to the incident confidence score and forces the `is_provisional` flag to `True`.

4. **Testing**
   - Added `test_parked_entity.py` to assert that:
     - `EntityNodeView` can hold the `is_parked` boolean.
     - `calculate_probabilistic_confidence` applies a 0.1 multiplier when `is_parked=True`.
     - `calculate_probabilistic_incident_confidence` forces `is_provisional=True` and applies the penalty if the alert or any related resources are parked.

## Conclusion
The retrieval layer is now fully aware of the DLQ/parked state of entities. When a stuck outbox event causes an entity's graph representation to fall out of sync with the Postgres source of truth, the resolution and search features proactively penalize its confidence and communicate its provisional status to the client.
