"""Unit tests for ``QdrantVectorStore`` pure functions and mock-based flows.

These tests do not spin up a Qdrant container; they cover the
deterministic logic that does not depend on the backing cluster:

- collection-name derivation
- payload-construction from ``ChunkPayload``
- workspace extraction + rejection of missing workspace
- protocol compliance (runtime ``isinstance(store, VectorStore)``)
- ``delete_by_document`` / ``count`` fail-closed on missing workspace

Container-backed tests live in ``tests/test_qdrant_contract.py``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.storage import ChunkPayload, VectorStore
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_constants import (
    NAMED_VECTOR_DENSE_PRIMARY,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_ORD,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_TEXT,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_WORKSPACE_ID,
)
from omniscience_index.stores.qdrant_store import (
    QdrantVectorStore,
    build_collection_name,
)
from qdrant_client import AsyncQdrantClient

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Collection naming is deterministic + model/dim-bound
# ---------------------------------------------------------------------------


def test_collection_name_is_deterministic() -> None:
    name = build_collection_name(embedding_model="nomic-embed-text", dim=768)
    assert name == build_collection_name(embedding_model="nomic-embed-text", dim=768)


def test_collection_name_includes_model_and_dim() -> None:
    name = build_collection_name(embedding_model="mxbai-embed-large", dim=1024)
    assert "mxbai-embed-large" in name
    assert "d1024" in name


def test_collection_name_sanitises_separator_chars() -> None:
    """Model ids with ``/`` or ``:`` map to collection names Qdrant accepts."""
    a = build_collection_name(embedding_model="voyage-ai/voyage-3", dim=1024)
    b = build_collection_name(embedding_model="hf:BAAI/bge-m3", dim=1024)
    assert "/" not in a
    assert ":" not in b


def test_different_models_produce_different_collections() -> None:
    """One-collection-per-model invariant from ADR-0006 §Schema posture."""
    a = build_collection_name(embedding_model="ollama-nomic", dim=768)
    b = build_collection_name(embedding_model="voyage-3", dim=768)
    assert a != b


def test_different_dims_produce_different_collections() -> None:
    """A dim change is also a collection split — embeddings are non-mixable."""
    a = build_collection_name(embedding_model="some-model", dim=768)
    b = build_collection_name(embedding_model="some-model", dim=1024)
    assert a != b


# ---------------------------------------------------------------------------
# Adapter construction + runtime-protocol compliance
# ---------------------------------------------------------------------------


def _embedding_provider(*, dim: int = 4, model: str = "test-model") -> Any:
    """Build a Mock that satisfies the EmbeddingProvider protocol."""
    m = MagicMock()
    m.dim = dim
    m.model_name = model
    m.provider_name = "mock"
    m.embed = AsyncMock(return_value=[[0.1] * dim])
    m.close = AsyncMock()
    return m


def _mock_client() -> Any:
    """Return an ``AsyncQdrantClient``-shaped mock with async methods."""
    c = MagicMock(spec=AsyncQdrantClient)
    c.collection_exists = AsyncMock(return_value=True)
    c.create_collection = AsyncMock()
    c.create_payload_index = AsyncMock()
    c.upsert = AsyncMock()
    c.scroll = AsyncMock(return_value=([], None))
    c.delete = AsyncMock()
    c.set_payload = AsyncMock()
    c.query_points = AsyncMock()
    c.close = AsyncMock()
    return c


def test_qdrant_store_satisfies_vector_store_protocol() -> None:
    """Runtime ``isinstance`` — the adapter fulfils the protocol (#103)."""
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=_mock_client(),
    )
    assert isinstance(store, VectorStore)


def test_qdrant_store_uses_embedding_provider_for_collection_name() -> None:
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(dim=1024, model="m1"),
        client=_mock_client(),
    )
    assert store.collection_name == build_collection_name(embedding_model="m1", dim=1024)


# ---------------------------------------------------------------------------
# ACL: upsert rejects missing workspace, delete/count fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_rejects_missing_workspace_id() -> None:
    """ADR-0006 §ACL: non-null workspace_id is mandatory on every point."""
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=_mock_client(),
    )
    chunks: list[ChunkPayload] = [{"ord": 0, "text": "t", "embedding": [0.0, 0.0, 0.0, 0.0]}]
    with pytest.raises(ValueError, match="workspace_id"):
        await store.upsert_chunks(
            source_id=uuid.uuid4(),
            external_id="e1",
            uri="u",
            title=None,
            content_hash="h",
            metadata={},  # missing workspace_id
            chunks=chunks,
        )


@pytest.mark.asyncio
async def test_upsert_rejects_null_workspace_id() -> None:
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=_mock_client(),
    )
    chunks: list[ChunkPayload] = [{"ord": 0, "text": "t", "embedding": [0.0, 0.0, 0.0, 0.0]}]
    with pytest.raises(ValueError, match="workspace_id"):
        await store.upsert_chunks(
            source_id=uuid.uuid4(),
            external_id="e1",
            uri="u",
            title=None,
            content_hash="h",
            metadata={"workspace_id": None},
            chunks=chunks,
        )


@pytest.mark.asyncio
async def test_delete_by_document_fails_closed_on_missing_workspace() -> None:
    """Without a workspace, delete is cross-tenant — fail closed per #117 carry-forward."""
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=_mock_client(),
    )
    with pytest.raises(ValueError, match="workspace_id"):
        await store.delete_by_document(source_id=uuid.uuid4(), external_id="e1")


@pytest.mark.asyncio
async def test_count_fails_closed_on_missing_workspace() -> None:
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=_mock_client(),
    )
    with pytest.raises(ValueError, match="workspace_id"):
        await store.count(source_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# Upsert happy-path: points carry required payload fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_creates_points_with_workspace_payload() -> None:
    client = _mock_client()
    client.scroll = AsyncMock(return_value=([], None))  # no existing doc
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    source_id = uuid.uuid4()
    chunks: list[ChunkPayload] = [{"ord": 0, "text": "hello", "embedding": [0.1, 0.2, 0.3, 0.4]}]
    outcome = await store.upsert_chunks(
        source_id=source_id,
        external_id="e1",
        uri="u",
        title="t",
        content_hash="h",
        metadata={"workspace_id": str(_WS)},
        chunks=chunks,
    )
    assert outcome.action == "created"
    assert outcome.chunks_written == 1
    assert outcome.doc_version == 1
    # Verify the Qdrant upsert was called with a properly-shaped point.
    call = client.upsert.await_args
    points = call.kwargs["points"]
    assert len(points) == 1
    p = points[0]
    assert NAMED_VECTOR_DENSE_PRIMARY in p.vector
    assert p.payload[PAYLOAD_WORKSPACE_ID] == str(_WS)
    assert p.payload[PAYLOAD_SOURCE_ID] == str(source_id)
    assert PAYLOAD_DOCUMENT_ID in p.payload
    assert p.payload[PAYLOAD_TEXT] == "hello"
    assert p.payload[PAYLOAD_ORD] == 0
    assert p.payload[PAYLOAD_TOMBSTONED_AT] is None


@pytest.mark.asyncio
async def test_upsert_is_unchanged_when_content_hash_matches() -> None:
    """Same-hash re-ingest is a no-op (mirrors pgvector writer semantics)."""
    from qdrant_client.http.models import Record

    existing_doc_id = uuid.uuid4()
    existing_point = Record(
        id=str(uuid.uuid4()),
        payload={
            PAYLOAD_WORKSPACE_ID: str(_WS),
            PAYLOAD_DOCUMENT_ID: str(existing_doc_id),
            "content_hash": "h1",
            "doc_version": 3,
        },
        vector=None,
    )
    client = _mock_client()
    client.scroll = AsyncMock(return_value=([existing_point], None))
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    chunks: list[ChunkPayload] = [{"ord": 0, "text": "t", "embedding": [0.0, 0.0, 0.0, 0.0]}]
    outcome = await store.upsert_chunks(
        source_id=uuid.uuid4(),
        external_id="e1",
        uri="u",
        title=None,
        content_hash="h1",  # same as existing
        metadata={"workspace_id": str(_WS)},
        chunks=chunks,
    )
    assert outcome.action == "unchanged"
    assert outcome.chunks_written == 0
    assert outcome.document_id == existing_doc_id
    client.upsert.assert_not_awaited()
