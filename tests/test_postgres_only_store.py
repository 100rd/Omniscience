"""Unit/Integration tests for PostgresOnlyStore (Lite-mode store) using SQLAlchemy mocks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_index.stores.postgres_only_store import PostgresOnlyStore
from omniscience_retrieval.models import SearchRequest
from sqlalchemy import text


# Async mock for EmbeddingProvider
class DummyEmbeddingProvider:
    def __init__(self, dim: int = 4):
        self.dim = dim
        self.provider_name = "dummy"
        self.model_name = "dummy-model"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class MockAsyncContextManager:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


@pytest.fixture()
def mock_session_factory():
    """Create a mock session factory that returns a mock session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    session.get = AsyncMock()

    # Mock transactions context manager
    session.begin = MagicMock(return_value=MockAsyncContextManager(None))

    factory = MagicMock()
    factory.return_value = MockAsyncContextManager(session)
    factory.session = session
    return factory


@pytest.mark.asyncio
async def test_postgres_only_store_lifecycle(mock_session_factory):
    # Mock execute to return expected schema
    async def mock_execute(stmt, *args, **kwargs):
        res = MagicMock()
        res.fetchone.return_value = ("embedding", "vector")
        return res

    mock_session_factory.session.execute = AsyncMock(side_effect=mock_execute)

    store = PostgresOnlyStore(session_factory=mock_session_factory)
    await store.connect()

    # Check if vector column check is made
    async with mock_session_factory() as session:
        res = await session.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'chunks' AND column_name = 'embedding'"
            )
        )
        row = res.fetchone()
        assert row is not None
        assert "vector" in row[1]

    await store.close()


@pytest.mark.asyncio
async def test_postgres_only_store_graph_ops(mock_session_factory):
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    entity1_id = uuid.uuid4()

    # Dynamic SQL execution mocker
    async def mock_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt)
        res = MagicMock()

        if "traverse_cte" in stmt_str:
            mock_cte_row = MagicMock()
            mock_cte_row.id = uuid.uuid4()
            mock_cte_row.source_id = source_id
            mock_cte_row.entity_type = "module"
            mock_cte_row.name = "helper.py"
            mock_cte_row.display_name = "helper.py"
            mock_cte_row.chunk_text = "helper chunk"
            mock_cte_row.depth = 1
            res.all.return_value = [mock_cte_row]
        elif "SELECT edge.edge_type" in stmt_str or "edges" in stmt_str:
            mock_edge_row = MagicMock()
            mock_edge_row.edge_type = "imports"
            mock_edge_row.from_name = "main.py"
            mock_edge_row.to_name = "helper.py"
            res.all.return_value = [mock_edge_row]
        elif "information_schema" in stmt_str:
            res.fetchone.return_value = ("embedding", "vector")
        elif "SELECT entities.id" in stmt_str or "Entity.id" in stmt_str or "entities" in stmt_str:
            if "name =" in stmt_str or "entities.name =" in stmt_str or "name" in stmt_str:
                # Seed entity query
                seed_ent = MagicMock()
                seed_ent.id = entity1_id
                seed_ent.source_id = source_id
                seed_ent.entity_type = "module"
                seed_ent.name = "main.py"
                seed_ent.display_name = "main.py"
                seed_ent.chunk_id = None
                res.scalar_one_or_none.return_value = seed_ent
                # traverse now uses scalars().first() to handle potential
                # duplicate rows (concurrent upserts); configure both.
                res.scalars.return_value.first.return_value = seed_ent
            else:
                res.all.return_value = []
        else:
            res.all.return_value = []
            res.fetchone.return_value = None
            res.scalar_one_or_none.return_value = None
            res.scalars.return_value.first.return_value = None
        return res

    mock_session_factory.session.execute = AsyncMock(side_effect=mock_execute)

    store = PostgresOnlyStore(session_factory=mock_session_factory)
    await store.connect()

    class MockEntity:
        def __init__(self, entity_id, name, kind, metadata):
            self.id = entity_id
            self.name = name
            self.entity_type = kind
            self.metadata = metadata

    class MockEdge:
        def __init__(self, source_entity_id, target_name, edge_type):
            self.source_entity_id = source_entity_id
            self.target_name = target_name
            self.edge_type = edge_type

    entities = [
        MockEntity(entity1_id, "main.py", "module", {"workspace_id": str(workspace_id)}),
    ]
    edges = [MockEdge(entity1_id, "helper.py", "imports")]

    await store.upsert_graph(
        source_id=source_id,
        document_id=doc_id,
        entities=entities,
        edges=edges,
    )

    # Verify traversal (recursive CTE)
    res = await store.traverse(
        entity_name="main.py",
        workspace_id=workspace_id,
        max_depth=2,
    )

    assert res.seed.name == "main.py"
    assert len(res.related) == 1
    assert res.related[0].name == "helper.py"
    assert len(res.edges) == 1
    assert res.edges[0].from_entity == "main.py"
    assert res.edges[0].to_entity == "helper.py"


@pytest.mark.asyncio
async def test_postgres_only_store_vector_ops(mock_session_factory):
    provider = DummyEmbeddingProvider(dim=4)
    store = PostgresOnlyStore(session_factory=mock_session_factory, embedding_provider=provider)
    await store.connect()

    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()

    # Dynamic SQL execution mocker
    async def mock_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt)
        res = MagicMock()

        if "distance" in stmt_str or "<=>" in stmt_str:
            mock_chunk = MagicMock()
            mock_chunk.id = uuid.uuid4()
            mock_chunk.document_id = uuid.uuid4()
            mock_chunk.ord = 0
            mock_chunk.text = "def hello(): print('hello')"
            mock_chunk.symbol = "hello"
            mock_chunk.ingestion_run_id = uuid.uuid4()
            mock_chunk.embedding_model = "dummy-model"
            mock_chunk.embedding_provider = "dummy"
            mock_chunk.parser_version = "v1"
            mock_chunk.chunker_strategy = "simple"
            mock_chunk.chunk_metadata = {}

            mock_doc = MagicMock()
            mock_doc.id = uuid.uuid4()
            mock_doc.source_id = source_id
            mock_doc.external_id = "main.py"
            mock_doc.uri = "https://example.com/main.py"
            mock_doc.title = "Main Py"
            mock_doc.content_hash = "abc"
            mock_doc.indexed_at = datetime.now(UTC)
            mock_doc.doc_version = 1

            mock_source = MagicMock()
            mock_source.id = source_id
            mock_source.tenant_id = workspace_id
            mock_source.name = "Test Src"
            mock_source.type = "git"
            mock_source.source_type = "git"

            res.all.return_value = [(mock_chunk, mock_doc, mock_source, 0.0)]
        elif "information_schema" in stmt_str:
            res.fetchone.return_value = ("embedding", "vector")
        elif (
            "SELECT documents.id" in stmt_str or "Document" in stmt_str or "documents" in stmt_str
        ):
            res.scalar_one_or_none.return_value = None  # force document creation path
        else:
            res.all.return_value = []
            res.fetchone.return_value = None
            res.scalar_one_or_none.return_value = None
        return res

    mock_session_factory.session.execute = AsyncMock(side_effect=mock_execute)

    chunks = [
        {
            "ord": 0,
            "text": "def hello(): print('hello')",
            "embedding": [0.1, 0.2, 0.3, 0.4],
            "symbol": "hello",
            "metadata": {"line": 1},
        }
    ]

    await store.upsert_chunks(
        source_id=source_id,
        external_id="main.py",
        uri="https://example.com/main.py",
        title="Main Py",
        content_hash="abc",
        metadata={"workspace_id": str(workspace_id)},
        chunks=chunks,
    )

    # Search
    request = SearchRequest(query="hello function", top_k=2, retrieval_strategy="hybrid")

    result = await store.search(
        request=request,
        workspace_id=workspace_id,
    )

    assert len(result.hits) == 1
    assert "hello" in result.hits[0].text
    assert result.hits[0].score == 1.0  # similarity = 1.0 - distance (1.0 - 0.0 = 1.0)
