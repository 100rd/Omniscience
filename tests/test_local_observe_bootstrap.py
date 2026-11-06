"""Exercise fixture bootstrap replay against Qdrant's in-process store."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.db.models import Document
from omniscience_core.storage.graph import GraphWriteResult
from omniscience_index import IndexWriter
from omniscience_index.hashing import compute_content_hash
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from omniscience_retrieval.models import SearchRequest
from omniscience_server.local_observe import _documents, _seed_corpus
from omniscience_server.local_observe_policy import PROFILE_ID, WORKSPACES, fixture_id
from qdrant_client import AsyncQdrantClient


@pytest.mark.asyncio
async def test_seed_replays_all_documents_at_the_same_source_checkpoint() -> None:
    embedding = MagicMock(dim=2, model_name="fixture", provider_name="fixture")
    embedding.embed = AsyncMock(return_value=[[1.0, 0.0]])
    graph = MagicMock()
    graph.get_entity = AsyncMock(return_value=None)
    graph.upsert_graph = AsyncMock(return_value=GraphWriteResult(applied=True))

    # Postgres already has every document at version 1, as after a partial write.
    documents = {
        (fixture_id(f"source/{slug}"), external_id): Document(
            id=fixture_id(f"document/{slug}/{external_id}"),
            doc_version=1,
            content_hash=compute_content_hash(content),
            doc_metadata={"synthetic_profile": PROFILE_ID},
        )
        for slug in WORKSPACES
        for external_id, _, content in _documents(slug)
    }

    def lookup(statement):
        params = statement.compile().params
        return documents[params["source_id_1"], params["external_id_1"]]

    def execute(statement):
        result = MagicMock()
        result.scalar_one_or_none.return_value = lookup(statement)
        return result

    factory = MagicMock()
    db = MagicMock()
    factory.return_value.__aenter__.return_value = db
    db.scalar = AsyncMock(side_effect=lookup)
    db.execute = AsyncMock(side_effect=execute)

    client = AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(config=QdrantConfig(), embedding_provider=embedding, client=client)
    writer = IndexWriter(session_factory=factory, graph_store=graph, vector_store=store)
    request = SearchRequest(
        query="Local platform observation runbook", retrieval_strategy="structural"
    )
    try:
        await store._ensure_collection()
        await _seed_corpus(factory, writer, graph, embedding)

        expected = {
            "alpha": {"topology.md", "runbook.md", "platform.md"},
            "beta": {"topology.md", "runbook.md"},
        }
        before = {}
        for slug, workspace_id in WORKSPACES.items():
            hits = (await store.search(request=request, workspace_id=workspace_id)).hits
            assert len(hits) == len(expected[slug])
            assert {hit.citation.uri for hit in hits} == {
                f"fixture://{PROFILE_ID}/{slug}/{external_id}" for external_id in expected[slug]
            }
            before[slug] = {hit.chunk_id: hit.citation for hit in hits}

        await _seed_corpus(factory, writer, graph, embedding)
        for slug, workspace_id in WORKSPACES.items():
            hits = (await store.search(request=request, workspace_id=workspace_id)).hits
            assert len(hits) == len(expected[slug])
            assert {hit.chunk_id: hit.citation for hit in hits} == before[slug]
    finally:
        await store.close()
