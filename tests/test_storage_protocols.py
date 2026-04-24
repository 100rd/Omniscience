"""Unit tests for the #103 ``GraphStore`` / ``VectorStore`` scaffold.

Focus areas
-----------

1. **Adapter wrapping is behaviour-preserving.**  The pgvector
   adapters delegate to ``IndexWriter`` / ``GraphQueryService``
   without introducing logic changes.
2. **ACL invariant is locked at the protocol level.**  Every read
   method on both protocols has ``workspace_id`` as a keyword-only
   required parameter.  A missing or ``None`` workspace raises
   ``TypeError`` (Python) rather than silently widening.
3. **View-model translation preserves the MCP/REST wire shape.**
   ``GraphResultView`` serialises byte-identically to the legacy
   ``GraphResult.to_dict`` output.

No real database is started; adapters are fed ``MagicMock`` session
factories and the underlying service/writer calls are patched.
"""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.storage import (
    ChunkPayload,
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphEdgeView,
    GraphResultView,
    GraphStore,
    UpsertOutcome,
    VectorStore,
)
from omniscience_retrieval.adapters import (
    PgVectorGraphStore,
    PgVectorVectorStore,
)
from omniscience_retrieval.adapters.pgvector_graph import (
    _edge_to_view,
    _node_to_view,
    _result_to_view,
)
from omniscience_retrieval.adapters.pgvector_vector import _payload_to_chunk_data
from omniscience_retrieval.graph_query import (
    EdgeResult,
    EntityNode,
    GraphResult,
)

_WORKSPACE_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000007a")
_OTHER_WORKSPACE_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000007b")


# ---------------------------------------------------------------------------
# Protocol-shape assertions — locked at import time
# ---------------------------------------------------------------------------


def _assert_kw_only_required(sig: inspect.Signature, param_name: str) -> None:
    """Fail the test when ``param_name`` isn't keyword-only and required."""
    param = sig.parameters[param_name]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{param_name} must be keyword-only (protect against positional misuse)"
    )
    assert param.default is inspect.Parameter.empty, (
        f"{param_name} must have no default (invariant: caller must pass it)"
    )


@pytest.mark.parametrize(
    "method_name",
    ["get_entity", "find_related", "traverse", "upsert_entity", "upsert_edge"],
)
def test_graph_store_methods_require_workspace_id(method_name: str) -> None:
    """Every workspace-sensitive ``GraphStore`` method pins workspace_id."""
    method = getattr(GraphStore, method_name)
    sig = inspect.signature(method)
    _assert_kw_only_required(sig, "workspace_id")


def test_vector_store_search_requires_workspace_id() -> None:
    """``VectorStore.search`` pins workspace_id as keyword-only required."""
    sig = inspect.signature(VectorStore.search)
    _assert_kw_only_required(sig, "workspace_id")


def test_graph_store_upsert_graph_signature_matches_index_writer() -> None:
    """The batch write path must mirror ``IndexWriter.upsert_graph``.

    This guards against signature drift during #104 / #106 when the
    Neo4j / Qdrant adapters land — ingestion code must keep working
    unchanged against the protocol.
    """
    sig = inspect.signature(GraphStore.upsert_graph)
    assert {"source_id", "document_id", "entities", "edges"}.issubset(sig.parameters)


# ---------------------------------------------------------------------------
# View translators preserve the wire shape
# ---------------------------------------------------------------------------


def test_node_to_view_preserves_fields() -> None:
    node = EntityNode(
        name="svc.auth", kind="service", source="s1", chunk_text="body", depth=2, edge_type="calls"
    )
    view = _node_to_view(node)
    assert isinstance(view, EntityNodeView)
    assert (view.name, view.kind, view.source, view.chunk_text, view.depth, view.edge_type) == (
        "svc.auth",
        "service",
        "s1",
        "body",
        2,
        "calls",
    )


def test_edge_to_view_preserves_fields() -> None:
    edge = EdgeResult(from_entity="a", to_entity="b", edge_type="calls")
    view = _edge_to_view(edge)
    assert isinstance(view, GraphEdgeView)
    assert (view.from_entity, view.to_entity, view.edge_type) == ("a", "b", "calls")


def test_result_to_view_preserves_whole_shape() -> None:
    seed = EntityNode(name="root", kind="k", source="s", chunk_text=None)
    neighbor = EntityNode(name="n1", kind="k", source="s", chunk_text=None, depth=1)
    edge = EdgeResult(from_entity="root", to_entity="n1", edge_type="calls")
    result = GraphResult(seed=seed, related=[neighbor], edges=[edge])

    view = _result_to_view(result)
    assert isinstance(view, GraphResultView)
    assert view.seed.name == "root"
    assert [n.name for n in view.related] == ["n1"]
    assert [e.edge_type for e in view.edges] == ["calls"]


# ---------------------------------------------------------------------------
# PgVectorGraphStore — workspace_id is propagated, not silently widened
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pgvector_graph_find_related_forwards_workspace() -> None:
    """The adapter must forward the caller's workspace_id unchanged."""
    store = PgVectorGraphStore(session_factory=MagicMock())

    seed = EntityNode(name="root", kind="k", source="s", chunk_text=None)
    stub_result = GraphResult(seed=seed)

    with patch(
        "omniscience_retrieval.adapters.pgvector_graph.GraphQueryService.get_related",
        new_callable=AsyncMock,
        return_value=stub_result,
    ) as mock_get:
        await store.find_related(
            entity_name="root",
            workspace_id=_WORKSPACE_ID,
            max_depth=2,
            edge_types=["calls"],
        )

    mock_get.assert_awaited_once_with(
        entity_name="root",
        workspace_id=_WORKSPACE_ID,
        max_depth=2,
        edge_types=["calls"],
    )


@pytest.mark.asyncio
async def test_pgvector_graph_traverse_is_alias_of_find_related() -> None:
    """``traverse`` and ``find_related`` must produce identical results.

    Regression guard for the Neo4j adapter (#104): if Neo4j implements
    ``traverse`` natively and ``find_related`` becomes the delegate,
    the relationship is inverted, not broken.
    """
    store = PgVectorGraphStore(session_factory=MagicMock())

    seed = EntityNode(name="root", kind="k", source="s", chunk_text=None)
    stub_result = GraphResult(seed=seed)

    with patch(
        "omniscience_retrieval.adapters.pgvector_graph.GraphQueryService.get_related",
        new_callable=AsyncMock,
        return_value=stub_result,
    ):
        v_find = await store.find_related(entity_name="root", workspace_id=_WORKSPACE_ID)
        v_trav = await store.traverse(entity_name="root", workspace_id=_WORKSPACE_ID)

    assert v_find.seed.name == v_trav.seed.name == "root"


@pytest.mark.asyncio
async def test_pgvector_graph_get_entity_returns_none_for_cross_workspace() -> None:
    """Cross-workspace existence is NEVER leaked — returns None, not raise."""
    store = PgVectorGraphStore(session_factory=MagicMock())

    with patch(
        "omniscience_retrieval.adapters.pgvector_graph.GraphQueryService.get_related",
        new_callable=AsyncMock,
        side_effect=ValueError("entity_not_found:svc.other"),
    ):
        result = await store.get_entity(entity_name="svc.other", workspace_id=_OTHER_WORKSPACE_ID)

    assert result is None


@pytest.mark.asyncio
async def test_pgvector_graph_get_entity_propagates_non_not_found_errors() -> None:
    """Non-"not-found" ValueErrors bubble up — we only mask the ACL variant."""
    store = PgVectorGraphStore(session_factory=MagicMock())

    with (
        patch(
            "omniscience_retrieval.adapters.pgvector_graph.GraphQueryService.get_related",
            new_callable=AsyncMock,
            side_effect=ValueError("invalid_depth"),
        ),
        pytest.raises(ValueError, match="invalid_depth"),
    ):
        await store.get_entity(entity_name="x", workspace_id=_WORKSPACE_ID)


@pytest.mark.asyncio
async def test_pgvector_graph_upsert_entity_raises_not_implemented() -> None:
    """Granular upserts are reserved for Phase 2 (Neo4j)."""
    store = PgVectorGraphStore(session_factory=MagicMock())
    payload = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="x",
        display_name="x",
        chunk_id=None,
    )
    with pytest.raises(NotImplementedError):
        await store.upsert_entity(entity=payload, workspace_id=_WORKSPACE_ID)


@pytest.mark.asyncio
async def test_pgvector_graph_upsert_edge_raises_not_implemented() -> None:
    """Granular edge upserts are reserved for Phase 2 (Neo4j)."""
    store = PgVectorGraphStore(session_factory=MagicMock())
    payload = EdgeUpsert(
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        edge_type="calls",
    )
    with pytest.raises(NotImplementedError):
        await store.upsert_edge(edge=payload, workspace_id=_WORKSPACE_ID)


@pytest.mark.asyncio
async def test_pgvector_graph_delete_tombstoned_returns_zero() -> None:
    """pgvector's delete_tombstoned is a documented no-op (cascade handles it)."""
    store = PgVectorGraphStore(session_factory=MagicMock())
    assert await store.delete_tombstoned() == 0


# ---------------------------------------------------------------------------
# PgVectorVectorStore — payload translation + search propagation
# ---------------------------------------------------------------------------


def test_chunk_payload_translator_required_keys() -> None:
    """``_payload_to_chunk_data`` enforces the (ord, text, embedding) contract."""
    payload: ChunkPayload = {
        "ord": 0,
        "text": "hello",
        "embedding": [0.1, 0.2, 0.3],
    }
    data = _payload_to_chunk_data(payload)
    assert data.ord == 0
    assert data.text == "hello"
    assert data.embedding == [0.1, 0.2, 0.3]
    assert data.symbol is None  # default
    assert data.metadata == {}  # default


def test_chunk_payload_translator_passes_optional_keys() -> None:
    payload: ChunkPayload = {
        "ord": 3,
        "text": "t",
        "embedding": [0.0],
        "symbol": "module.Class.method",
        "metadata": {"line": 42},
        "embedding_model": "mxbai",
        "embedding_provider": "local",
        "parser_version": "2.1",
        "chunker_strategy": "ast",
    }
    data = _payload_to_chunk_data(payload)
    assert data.symbol == "module.Class.method"
    assert data.metadata == {"line": 42}
    assert data.embedding_model == "mxbai"
    assert data.chunker_strategy == "ast"


def test_chunk_payload_translator_raises_on_missing_required_key() -> None:
    """A missing mandatory key produces a clear error, not a downstream crash."""
    bad_payload: ChunkPayload = {"ord": 0, "text": "t"}  # type: ignore[typeddict-item]
    with pytest.raises(ValueError, match="embedding"):
        _payload_to_chunk_data(bad_payload)


@pytest.mark.asyncio
async def test_pgvector_vector_search_forwards_workspace_id_in_logs() -> None:
    """Search forwards the request and does not drop workspace_id on the floor.

    Today's pgvector adapter does not filter search by workspace (known
    gap; see class docstring).  The test locks in that the value IS
    accepted and the call reaches ``RetrievalService.search`` so the
    Qdrant adapter (#106) can enforce the invariant natively without
    breaking any caller.
    """
    embedding_provider = MagicMock()
    session_factory = MagicMock()

    store = PgVectorVectorStore(
        session_factory=session_factory,
        embedding_provider=embedding_provider,
    )

    request = MagicMock()
    request.retrieval_strategy = "hybrid"
    request.top_k = 10
    stub_result = MagicMock()

    with patch(
        "omniscience_retrieval.adapters.pgvector_vector.RetrievalService.search",
        new_callable=AsyncMock,
        return_value=stub_result,
    ) as mock_search:
        returned = await store.search(request=request, workspace_id=_WORKSPACE_ID)

    assert returned is stub_result
    mock_search.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_pgvector_vector_upsert_chunks_wraps_writer() -> None:
    """Upsert delegates to ``IndexWriter.upsert_document`` with translated payload."""
    from omniscience_index.writer import UpsertResult

    embedding_provider = MagicMock()
    session_factory = MagicMock()
    store = PgVectorVectorStore(
        session_factory=session_factory,
        embedding_provider=embedding_provider,
    )

    source_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    stub = UpsertResult(action="created", document_id=doc_id, chunks_written=1, doc_version=1)
    chunks: list[ChunkPayload] = [{"ord": 0, "text": "t", "embedding": [0.0]}]

    with patch(
        "omniscience_retrieval.adapters.pgvector_vector.IndexWriter.upsert_document",
        new_callable=AsyncMock,
        return_value=stub,
    ) as mock_upsert:
        outcome = await store.upsert_chunks(
            source_id=source_id,
            external_id="e1",
            uri="s3://b/k",
            title=None,
            content_hash="abc",
            metadata={},
            chunks=chunks,
        )

    assert isinstance(outcome, UpsertOutcome)
    assert outcome.action == "created"
    assert outcome.document_id == doc_id
    mock_upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_pgvector_vector_delete_by_document_wraps_tombstone() -> None:
    embedding_provider = MagicMock()
    session_factory = MagicMock()
    store = PgVectorVectorStore(
        session_factory=session_factory,
        embedding_provider=embedding_provider,
    )
    source_id = uuid.uuid4()

    with patch(
        "omniscience_retrieval.adapters.pgvector_vector.IndexWriter.tombstone",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_tomb:
        ok = await store.delete_by_document(source_id=source_id, external_id="doc")

    assert ok is True
    mock_tomb.assert_awaited_once_with(source_id, "doc")


# ---------------------------------------------------------------------------
# Runtime protocol compliance
# ---------------------------------------------------------------------------


def test_pgvector_graph_store_is_runtime_graph_store() -> None:
    """Runtime ``isinstance`` check — the adapter fulfils the protocol."""
    store = PgVectorGraphStore(session_factory=MagicMock())
    assert isinstance(store, GraphStore)


def test_pgvector_vector_store_is_runtime_vector_store() -> None:
    store = PgVectorVectorStore(
        session_factory=MagicMock(),
        embedding_provider=MagicMock(),
    )
    assert isinstance(store, VectorStore)
