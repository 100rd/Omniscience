# Action Point 3: Min-Checkpoint Replay for Partial Failure Isolation

## Problem Statement
Partial failure in the `IndexWriter` (e.g., Postgres write succeeds, but Qdrant or Neo4j fails) leaves the event in the queue or outbox. During the replay of the shared cursor, the pipeline would read the document from Postgres as "unchanged" but previously neglected to pass down the Postgres `doc_version` to Qdrant and Neo4j. This lack of the version prevented Qdrant and Neo4j from utilizing their internal per-store checkpoint logic (the `version` parameter remained `None`). Consequently, partial-failure replays were either re-executing non-idempotent operations or losing track of the true state convergence.

## Changes Implemented

1. **`IndexWriter.upsert_document` (in `writer.py`)**:
   - Removed the `version` parameter from the signature.
   - Now extracts `doc.doc_version` directly from the Postgres `Document` (the Source of Truth) after it's been inserted or updated.
   - Passes this canonical `doc.doc_version` to `self._vector_store.upsert_chunks(..., version=doc.doc_version)`.

2. **`IngestionPipeline` (in `pipeline.py`)**:
   - Updated `_stage_index` to return `(upsert_action, document_id, doc_version)`.
   - The `run` method now captures `doc_version` and propagates it to `_stage_graph`.
   - `_stage_graph` now accepts `doc_version` and forwards it to `self._index_writer.upsert_graph(..., version=doc_version)`.
   - Fixed `IndexWriterProtocol` to correctly advertise the `version` keyword argument for `upsert_graph`.

## Benefits & Semantics
- **Min-Checkpoint Replay**: By injecting the canonical `doc.doc_version` into Qdrant and Neo4j updates, we successfully unblock their built-in per-store checkpoint gating (`if version is not None and checkpoint < version:`).
- **Isolation of Failures**: If Qdrant fails on version 5 but Postgres succeeds, replay of the message will fetch version 5 from Postgres and pass `version=5` to Qdrant. Qdrant will see its internal checkpoint is `< 5`, apply the changes, and record the checkpoint as `5`. If Neo4j had succeeded previously, it skips the replay because its checkpoint is already `5`.

## Status
Completed. Regression tests in `test_store_contract.py` were also fixed alongside this effort to resolve `FullStore` and `PostgresOnlyStore` mismatches.
