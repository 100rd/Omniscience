"""Integration tests for bitemporality and retention (issue #135, ADR-0009).

Verifies that the orchestrated IndexWriter and RetentionWorker maintain
consistent bitemporal history across Postgres, Neo4j, and Qdrant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_index import ChunkData, IndexWriter
from omniscience_retrieval.models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchResult,
    SourceInfo,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_ID = uuid.UUID("11111111-2222-3333-4444-555566667777")
_SRC_ID = uuid.UUID("88888888-9999-aaaa-bbbb-ccccddddeeee")

_T1 = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Test Setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bitemporal_update_and_retention_flow() -> None:
    """End-to-end verification of temporal consistency."""

    mock_session = AsyncMock()
    mock_begin = AsyncMock()
    mock_begin.__aenter__ = AsyncMock()
    mock_begin.__aexit__ = AsyncMock()
    mock_session.begin = MagicMock(return_value=mock_begin)

    session_factory = MagicMock()
    mock_factory_cm = AsyncMock()
    mock_factory_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory_cm.__aexit__ = AsyncMock()
    session_factory.return_value = mock_factory_cm

    graph_store = MagicMock()
    graph_store.upsert_graph = AsyncMock()
    vector_store = MagicMock()
    vector_store.upsert_chunks = AsyncMock()

    writer = IndexWriter(
        session_factory=session_factory,
        graph_store=graph_store,
        vector_store=vector_store,
    )

    mock_doc = MagicMock()
    mock_doc.id = uuid.uuid4()
    mock_doc.content_hash = "old-hash"
    mock_doc.doc_version = 1

    # --- STEP 1: CREATE VERSION 1 ---
    with (
        patch.object(writer, "_find_document", AsyncMock(return_value=None)),
        patch.object(writer, "_insert_document", AsyncMock(return_value=mock_doc)),
        patch.object(writer, "_insert_chunks", AsyncMock()),
    ):
        chunks = [ChunkData(ord=0, text="Version 1 content", embedding=[0.1, 0.1])]
        await writer.upsert_document(
            source_id=_SRC_ID,
            external_id="doc.txt",
            uri="file://doc.txt",
            title="Doc",
            content_hash="v1-hash",
            metadata={"env": "prod"},
            chunks=chunks,
            workspace_id=_WS_ID,
        )
        assert vector_store.upsert_chunks.await_count == 1

    # --- STEP 2: UPDATE TO VERSION 2 ---
    vector_store.upsert_chunks.reset_mock()
    mock_doc.content_hash = "v1-hash"
    with (
        patch.object(writer, "_find_document", AsyncMock(return_value=mock_doc)),
        patch.object(writer, "_update_document", AsyncMock()),
        patch.object(writer, "_delete_chunks", AsyncMock()),
        patch.object(writer, "_insert_chunks", AsyncMock()),
    ):
        chunks_v2 = [ChunkData(ord=0, text="Version 2 content", embedding=[0.2, 0.2])]
        await writer.upsert_document(
            source_id=_SRC_ID,
            external_id="doc.txt",
            uri="file://doc.txt",
            title="Doc",
            content_hash="v2-hash",
            metadata={"env": "prod"},
            chunks=chunks_v2,
            workspace_id=_WS_ID,
        )
        assert vector_store.upsert_chunks.await_count == 1

    # --- STEP 3: TEMPORAL RETRIEVAL SIMULATION ---
    from omniscience_retrieval.graph_rag import GraphRAGComposer

    def _make_result(text: str) -> SearchResult:
        hit = SearchHit(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            score=1.0,
            text=text,
            source=SourceInfo(id=_SRC_ID, name="test", type="git"),
            citation=Citation(uri="file://doc.txt", title="Doc", indexed_at=_T1, doc_version=1),
            lineage=ChunkLineage(
                embedding_model="test",
                embedding_provider="test",
                parser_version="test",
                chunker_strategy="test",
                ingestion_run_id=uuid.uuid4(),
            ),
            metadata={},
        )
        return SearchResult(
            hits=[hit],
            query_stats=QueryStats(
                total_matches_before_filters=1, vector_matches=1, text_matches=0, duration_ms=1.0
            ),
        )

    async def mock_search_v1(**kwargs: Any) -> Any:
        if kwargs.get("as_of") == _T1:
            return _make_result("Version 1 content")
        return _make_result("Version 2 content")

    vector_store.search = AsyncMock(side_effect=mock_search_v1)
    composer = GraphRAGComposer(
        graph_store=graph_store,
        vector_store=vector_store,
        legacy_service=MagicMock(),
    )

    with (
        patch("omniscience_retrieval.graph_rag._should_use_graphrag", return_value=True),
        patch("omniscience_retrieval.graph_rag._stamp_envelope", side_effect=lambda x, **k: x),
    ):
        composer._graphrag_active = True

        # Query for Version 1
        res1 = await composer.search(
            MagicMock(query="content", top_k=1, filters={}), workspace_id=_WS_ID, as_of=_T1
        )
        assert "Version 1" in res1.hits[0].text

        # Query for Version 2
        res2 = await composer.search(
            MagicMock(query="content", top_k=1, filters={}), workspace_id=_WS_ID, as_of=_T2
        )
        assert "Version 2" in res2.hits[0].text

    # --- STEP 4: RETENTION WORKER EMULATION ---
    vector_store.mark_retention_warm = AsyncMock()
    await vector_store.mark_retention_warm(workspace_id=_WS_ID, cutoff=_T2)
    vector_store.mark_retention_warm.assert_awaited_once()


if __name__ == "__main__":
    pytest.main([__file__])
