"""Regression comparison: legacy retrieval vs GraphRAG composition.

For every query in ``tests/fixtures/graphrag_regression_set.json``:

1. Run the **legacy path** against a mocked pgvector-style retrieval
   service that returns a stable ordered top-k for the query on a
   shared in-memory corpus.
2. Run the **GraphRAG path** against mocked Neo4j+Qdrant stores that
   retrieve the same corpus (superset) and apply the composer's
   anchor+merge algorithm.
3. Assert the **top-k overlap** — ``|legacy_top_k ∩ graphrag_top_k| /
   |legacy_top_k|`` — meets the threshold declared in the fixture
   (issue #107 acceptance: >= 0.8).

We deliberately do not assert exact equality because the composer is
expected to reorder results (graph-affinity boost pulls
topically-connected chunks up the ranking).  The overlap metric
captures "better-or-equal" within a tolerance — if a chunk the legacy
path ranked in its top-k is not in the composer's top-k, the composer
must have replaced it with another topically-relevant chunk.

Workspace scoping is enforced on every downstream call — the test
uses a fixed workspace and verifies the composer passes it through.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_retrieval.graph_rag import (
    ANCHOR_FILTER_KEY,
    GraphRAGComposer,
)
from omniscience_retrieval.models import (
    ChunkLineage,
    Citation,
    QueryStats,
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInfo,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "graphrag_regression_set.json"


# ---------------------------------------------------------------------------
# Shared corpus — 40 chunks across 8 services, deterministic IDs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    """Build a deterministic corpus shared by both retrieval paths.

    Each service gets a source_id.  Each service has 5 chunks of
    hand-written pseudo-content that mentions service-specific
    keywords.  A simple keyword-overlap scorer is used to rank
    chunks for a query — this simulates what a real vector + BM25
    stack would return on this tiny corpus.
    """
    services = [
        "AuthService",
        "RBACService",
        "BillingService",
        "NotificationService",
        "IngestionService",
        "FreshnessWorker",
        "SchedulerWorker",
        "MCPService",
    ]
    chunk_templates = [
        "{svc} handles {kw1} and {kw2}",
        "{svc} verifies {kw1}",
        "{svc} emits {kw2} events",
        "{svc} reports {kw1} metrics",
        "{svc} persists {kw2}",
    ]
    service_keywords = {
        "AuthService": ("authentication login password tokens credentials", "session cookie jwt"),
        "RBACService": ("role permission access control authorization", "user group policy"),
        "BillingService": ("billing invoice subscription renewal refund", "charge payment"),
        "NotificationService": (
            "notification email webhook retry delivery",
            "channel template",
        ),
        "IngestionService": (
            "ingestion chunking document pipeline embedding",
            "parser connector",
        ),
        "FreshnessWorker": ("freshness SLO monitoring stale age", "sync interval"),
        "SchedulerWorker": ("scheduler interval trigger cron schedule", "resync stale"),
        "MCPService": ("MCP tool endpoint search graph", "REST rate limit workspace"),
    }
    chunks: list[dict[str, Any]] = []
    sources: dict[str, uuid.UUID] = {}
    for svc in services:
        src_id = uuid.uuid4()
        sources[svc] = src_id
        kw1, kw2 = service_keywords[svc]
        for i, tmpl in enumerate(chunk_templates):
            chunks.append(
                {
                    "chunk_id": uuid.uuid4(),
                    "source_id": src_id,
                    "service": svc,
                    "text": tmpl.format(svc=svc, kw1=kw1, kw2=kw2),
                    "ord": i,
                }
            )
    return {"services": services, "sources": sources, "chunks": chunks}


def _score(query: str, chunk_text: str) -> float:
    """Shared scoring used by both legacy and GraphRAG fakes.

    Token-overlap on whitespace; lowercased.  This is not a real
    retrieval scorer — it is deterministic and shared across both
    paths so the overlap metric reflects composition reordering, not
    scoring divergence.
    """
    q = {t.lower() for t in query.split() if len(t) > 2}
    c = {t.lower() for t in chunk_text.replace(",", " ").split() if len(t) > 2}
    if not q or not c:
        return 0.0
    return len(q & c) / len(q)


def _mk_hit(chunk: dict[str, Any], score: float) -> SearchHit:
    return SearchHit(
        chunk_id=chunk["chunk_id"],
        document_id=uuid.uuid4(),
        score=score,
        text=chunk["text"],
        source=SourceInfo(
            id=chunk["source_id"],
            name=chunk["service"],
            type="fake",
        ),
        citation=Citation(
            uri=f"https://corpus/{chunk['service']}/{chunk['ord']}",
            title=chunk["service"],
            indexed_at=datetime.now(UTC),
            doc_version=1,
        ),
        lineage=ChunkLineage(
            ingestion_run_id=None,
            embedding_model="stub",
            embedding_provider="stub",
            parser_version="stub",
            chunker_strategy="stub",
        ),
        metadata={},
    )


# ---------------------------------------------------------------------------
# Fakes that read from the shared corpus
# ---------------------------------------------------------------------------


class _LegacyCorpusService:
    """Simulates the legacy RetrievalService against the shared corpus."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    async def search(self, request: SearchRequest) -> SearchResult:
        scored = [(_score(request.query, c["text"]), c) for c in self._chunks]
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [_mk_hit(c, s) for s, c in scored[: request.top_k] if s > 0]
        return SearchResult(
            hits=top,
            query_stats=QueryStats(
                total_matches_before_filters=len(top),
                vector_matches=len(top),
                text_matches=0,
                duration_ms=1.0,
            ),
        )


class _CorpusVectorStore:
    """Simulates :class:`QdrantVectorStore` against the shared corpus.

    Honours ``workspace_id`` (single-workspace fixture — every chunk
    is in this one workspace).  Applies ``request.sources`` filter
    exactly like the real adapter.
    """

    def __init__(
        self,
        *,
        chunks: list[dict[str, Any]],
        workspace_id: uuid.UUID,
    ) -> None:
        self._chunks = chunks
        self._ws = workspace_id

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
    ) -> SearchResult:
        if workspace_id != self._ws:
            return SearchResult(
                hits=[],
                query_stats=QueryStats(
                    total_matches_before_filters=0,
                    vector_matches=0,
                    text_matches=0,
                    duration_ms=0.0,
                ),
            )
        pool: Iterable[dict[str, Any]] = self._chunks
        if request.sources:
            allowed = set(request.sources)
            pool = [c for c in self._chunks if str(c["source_id"]) in allowed]
        scored = [(_score(request.query, c["text"]), c) for c in pool]
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [_mk_hit(c, s) for s, c in scored[: request.top_k] if s > 0]
        return SearchResult(
            hits=top,
            query_stats=QueryStats(
                total_matches_before_filters=len(top),
                vector_matches=len(top),
                text_matches=0,
                duration_ms=1.0,
            ),
        )


class _CorpusGraphStore:
    """Simulates :class:`Neo4jGraphStore` against the shared corpus.

    Every service name maps to an entity whose subgraph contains a
    handful of related services (topical clusters).  Workspace is
    honoured: a lookup in the wrong workspace raises
    ``entity_not_found``.
    """

    TOPIC_CLUSTERS: ClassVar[dict[str, list[str]]] = {
        "AuthService": ["RBACService", "MCPService"],
        "RBACService": ["AuthService"],
        "BillingService": ["NotificationService"],
        "NotificationService": ["BillingService"],
        "IngestionService": ["FreshnessWorker", "SchedulerWorker"],
        "FreshnessWorker": ["SchedulerWorker", "IngestionService"],
        "SchedulerWorker": ["FreshnessWorker", "IngestionService"],
        "MCPService": ["AuthService"],
    }

    def __init__(
        self,
        *,
        sources: dict[str, uuid.UUID],
        workspace_id: uuid.UUID,
    ) -> None:
        self._sources = sources
        self._ws = workspace_id

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        if workspace_id != self._ws or entity_name not in self._sources:
            raise ValueError(f"entity_not_found:{entity_name}")
        related_names = self.TOPIC_CLUSTERS.get(entity_name, [])
        seed = EntityNodeView(
            name=entity_name,
            kind="service",
            source=str(self._sources[entity_name]),
            chunk_text=None,
            depth=0,
        )
        related = [
            EntityNodeView(
                name=n,
                kind="service",
                source=str(self._sources[n]),
                chunk_text=None,
                depth=1,
                edge_type="related",
            )
            for n in related_names
            if n in self._sources
        ]
        edges = [
            GraphEdgeView(from_entity=entity_name, to_entity=n, edge_type="related")
            for n in related_names
            if n in self._sources
        ]
        return GraphResultView(seed=seed, related=related, edges=edges)

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return await self.traverse(
            entity_name=entity_name,
            workspace_id=workspace_id,
            max_depth=max_depth,
            edge_types=edge_types,
        )


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


@pytest.fixture()
def force_graphrag(monkeypatch: pytest.MonkeyPatch) -> None:
    import omniscience_retrieval.graph_rag as gr

    monkeypatch.setattr(gr, "_should_use_graphrag", lambda g, v: True)


@pytest.mark.asyncio
async def test_graphrag_regression_top_k_overlap(
    corpus: dict[str, Any], force_graphrag: None
) -> None:
    """Compare legacy vs GraphRAG top-k on 20+ representative queries.

    Asserts the aggregate overlap across the whole regression set
    meets the fixture's threshold (default 0.8).  Individual queries
    MAY dip below the threshold — composition reorders aggressively
    when an anchor is supplied — as long as the average holds.
    """
    with FIXTURE_PATH.open() as f:
        fixture = json.load(f)
    queries = fixture["queries"]
    top_k = int(fixture["top_k"])
    threshold = float(fixture["top_k_overlap_threshold"])
    assert len(queries) >= 20, "Regression set must have 20+ queries (issue #107)"

    workspace = uuid.uuid4()
    chunks = corpus["chunks"]
    sources = corpus["sources"]

    legacy = _LegacyCorpusService(chunks)
    vs = _CorpusVectorStore(chunks=chunks, workspace_id=workspace)
    gs = _CorpusGraphStore(sources=sources, workspace_id=workspace)

    composer = GraphRAGComposer(
        graph_store=cast("Any", gs),
        vector_store=vs,
        legacy_service=legacy,
    )

    overlaps: list[float] = []
    per_query: list[dict[str, Any]] = []
    for q in queries:
        # Legacy: no anchor, just the query.
        legacy_req = SearchRequest(query=q["query"], top_k=top_k)
        legacy_result = await legacy.search(legacy_req)
        legacy_ids = {h.chunk_id for h in legacy_result.hits}

        # GraphRAG: same query, optional anchor hint.
        filters = {ANCHOR_FILTER_KEY: q["anchor"]} if q["anchor"] else None
        gr_req = SearchRequest(query=q["query"], top_k=top_k, filters=filters)
        gr_result = await composer.search(gr_req, workspace_id=workspace)
        gr_ids = {h.chunk_id for h in gr_result.hits}

        # Nothing to compare when legacy returns empty — composition cannot regress.
        overlap = 1.0 if not legacy_ids else len(legacy_ids & gr_ids) / len(legacy_ids)
        overlaps.append(overlap)
        per_query.append(
            {
                "id": q["id"],
                "legacy_top_k": len(legacy_ids),
                "graphrag_top_k": len(gr_ids),
                "overlap": overlap,
            }
        )

    average_overlap = sum(overlaps) / len(overlaps)
    # Aggregate assertion — this is the issue #107 acceptance criterion.
    assert average_overlap >= threshold, (
        f"Mean top-k overlap {average_overlap:.3f} below threshold {threshold}. "
        f"Per-query breakdown: {per_query}"
    )
