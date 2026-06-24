from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from omniscience_retrieval.graph_rag import GraphRAGComposer, GRAPHRAG_BUDGET_CHARS
from omniscience_retrieval.models import (
    SearchRequest,
    SearchResult,
    SearchHit,
    SourceInfo,
    Citation,
    ChunkLineage,
    QueryStats,
)

class _FakeVectorStore:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits

    async def search(self, request: SearchRequest, workspace_id: uuid.UUID, as_of: datetime | None = None) -> SearchResult:
        return SearchResult(hits=self.hits, query_stats=QueryStats(hybrid_vectors_scanned=0, bm25_tokens_scanned=0))


@pytest.mark.asyncio
async def test_graphrag_pagination_and_budget() -> None:
    wid = uuid.uuid4()
    # Create 10 hits, each with 10000 chars. Total 100,000 chars.
    # GRAPHRAG_BUDGET_CHARS is 24000. So we should only get 3 hits per page.
    hits = []
    for i in range(10):
        hits.append(
            SearchHit(
                chunk_id=uuid.uuid4(),
                score=1.0 - (i * 0.01),
                text="A" * 10000,
                source=SourceInfo(id=uuid.uuid4(), type="slack"),
                citation=Citation(indexed_at=datetime.now(UTC), uri="slack://foo"),
                lineage=ChunkLineage(document_id=uuid.uuid4()),
                score_type="raw",
                confidence=0.5,
                impact=0.5,
            )
        )
    
    vector_store = _FakeVectorStore(hits)
    composer = GraphRAGComposer(
        graph_store=None,  # Not used for this test (no anchor)
        vector_store=vector_store,
        registry=None,
    )
    
    # Request without cursor
    req1 = SearchRequest(query="test", top_k=10)
    res1 = composer._run_merge_stage(request=req1, vector_result=SearchResult(hits=hits, query_stats=QueryStats(hybrid_vectors_scanned=0, bm25_tokens_scanned=0)), anchor=getattr(composer, "_AnchorStageResult", None))
    # Wait, _run_merge_stage expects a proper anchor object. Let's build a fake one.
    from omniscience_retrieval.graph_rag import _AnchorStageResult
    fake_anchor = _AnchorStageResult(
        anchor_requested=False,
        anchor_name="",
        anchor_hit=False,
        candidate_source_ids=(),
        candidate_depths=(),
        centralities={},
        candidate_parked=(),
        duration_s=0.0
    )
    res1 = composer._run_merge_stage(request=req1, vector_result=SearchResult(hits=hits, query_stats=QueryStats(hybrid_vectors_scanned=0, bm25_tokens_scanned=0)), anchor=fake_anchor)
    
    # 24000 budget / 10000 per hit = 3 hits (since the loop cuts off *after* adding the hit that exceeds the budget)
    # Actually wait. `char_budget + hit_chars > GRAPHRAG_BUDGET_CHARS and len(top_hits) > 0`
    # Hit 1: budget 0 + 10000 > 24000 (False). Added. budget = 10000.
    # Hit 2: budget 10000 + 10000 > 24000 (False). Added. budget = 20000.
    # Hit 3: budget 20000 + 10000 > 24000 (True). And len > 0. It breaks.
    # So it should return 2 hits!
    assert len(res1.hits) == 2
    assert res1.next_cursor == "2"

    # Request with cursor "2"
    req2 = SearchRequest(query="test", top_k=10, cursor="2")
    res2 = composer._run_merge_stage(request=req2, vector_result=SearchResult(hits=hits, query_stats=QueryStats(hybrid_vectors_scanned=0, bm25_tokens_scanned=0)), anchor=fake_anchor)
    
    assert len(res2.hits) == 2
    assert res2.next_cursor == "4"
