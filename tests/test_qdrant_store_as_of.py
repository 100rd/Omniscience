"""Contract tests for the ADR-0008 §5 ``as_of`` predicate on Qdrant (issue #134).

These tests spin up a real Qdrant container (testcontainers) and
exercise the bitemporal read path end-to-end:

- A chunks fixture with multiple ``(valid_from, valid_to)`` ranges per
  ``document_id`` represents the v1/v2/v3 history of a single logical
  chunk.  ``search(query, workspace_id, as_of=T1)`` returns only the
  point whose validity window covers T1 — open-closed
  ``[valid_from, valid_to)`` per ADR-0008 §1.
- **Boundary semantics**: ``as_of == valid_from`` is included (``lte``);
  ``as_of == valid_to`` is excluded (strict ``gt``).  A row that ended
  exactly at ``as_of`` has stopped being valid by ``as_of``.
- **Cross-workspace isolation under ``as_of``** — MANDATORY contract
  per the issue: a search authenticated to workspace A returns no
  point from workspace B regardless of the ``as_of`` value or the
  overlap of their validity windows.  Direct vector-side analogue of
  the graph-side test in ``tests/test_neo4j_store_as_of.py``.
- **No-regression**: ``search(as_of=None)`` returns the same point set
  the pre-#134 contract returned (same wire-shape filter — no
  ``valid_*`` clauses at all).

Container-backed; opt-in via ``OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1``
to keep CI fast.  The shared fixture pattern follows
``tests/test_qdrant_contract.py``.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.qdrant")

from omniscience_core.storage import ChunkPayload
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_constants import (
    PAYLOAD_VALID_FROM,
    PAYLOAD_VALID_TO,
)
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

# ---------------------------------------------------------------------------
# Skip gate — opt-in via env flag (Docker + explicit acknowledgement).
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Cheap docker reachability probe."""
    if os.environ.get("SKIP_TESTCONTAINERS") == "1":
        return False
    try:
        import docker  # type: ignore[import-not-found]

        docker.from_env().ping()  # type: ignore[no-untyped-call]
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS", "0") != "1",
        reason=(
            "Qdrant as_of contract tests are opt-in. "
            "Set OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1 to run."
        ),
    ),
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker is not reachable; testcontainers cannot start a Qdrant container.",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures — Qdrant container + adapter, mirrors test_qdrant_contract.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def qdrant_container() -> Any:
    """Start a Qdrant container for the whole test session."""
    from testcontainers.qdrant import QdrantContainer

    with QdrantContainer(image="qdrant/qdrant:v1.12.4") as c:
        yield c


class _StubEmbedding:
    """Deterministic embedding provider — same shape as test_qdrant_contract."""

    dim = 8
    model_name = "stub-embedding"
    provider_name = "stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import math

        out: list[list[float]] = []
        for t in texts:
            h = hash(t)
            raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(self.dim)]
            n = math.sqrt(sum(x * x for x in raw)) or 1.0
            out.append([x / n for x in raw])
        return out

    async def close(self) -> None:
        return None


def _stub_vector(text: str, dim: int = 8) -> list[float]:
    """Same hash-based unit-norm vector ``_StubEmbedding`` produces."""
    import math

    h = hash(text)
    raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(dim)]
    n = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / n for x in raw]


@pytest_asyncio.fixture()
async def qdrant_store(qdrant_container: Any) -> AsyncIterator[QdrantVectorStore]:
    """Fresh adapter instance bound to the shared container."""
    host = qdrant_container.get_container_host_ip()
    http_port = int(qdrant_container.get_exposed_port(6333))
    grpc_port = int(qdrant_container.get_exposed_port(6334))
    config = QdrantConfig(
        host=host,
        grpc_port=grpc_port,
        http_port=http_port,
        api_key=None,
        prefer_grpc=False,
    )
    store = QdrantVectorStore(config=config, embedding_provider=_StubEmbedding())
    store._collection_name = (  # type: ignore[attr-defined]
        f"omniscience__stub-embedding__d8__t{uuid.uuid4().hex[:8]}"
    )
    await store.connect()
    try:
        yield store
    finally:
        if store._client is not None:  # type: ignore[attr-defined]
            client = cast("AsyncQdrantClient", store._client)  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                await client.delete_collection(store.collection_name)
        await store.close()


# ---------------------------------------------------------------------------
# Fixture builder — plant a chunk with explicit (valid_from, valid_to).
# ---------------------------------------------------------------------------


async def _plant_chunk_with_window(
    store: QdrantVectorStore,
    *,
    workspace_id: uuid.UUID,
    text: str,
    valid_from: datetime,
    valid_to: datetime | None,
    document_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    external_id: str | None = None,
) -> uuid.UUID:
    """Insert one Qdrant point directly with an explicit validity window.

    Uses the underlying Qdrant client to bypass the writer's "now()"
    defaults — necessary because the issue's non-goals forbid a
    historical-import path with caller-supplied ``valid_*`` on the
    public ``upsert_chunks`` API.  Tests-only utility.
    """
    from omniscience_index.stores.qdrant_constants import (
        NAMED_VECTOR_DENSE_PRIMARY,
        PAYLOAD_CHUNKER_STRATEGY,
        PAYLOAD_CONTENT_HASH,
        PAYLOAD_DOC_VERSION,
        PAYLOAD_DOCUMENT_ID,
        PAYLOAD_EMBEDDING_MODEL,
        PAYLOAD_EMBEDDING_PROVIDER,
        PAYLOAD_EXTERNAL_ID,
        PAYLOAD_INGESTION_RUN_ID,
        PAYLOAD_METADATA,
        PAYLOAD_ORD,
        PAYLOAD_PARSER_VERSION,
        PAYLOAD_RECORDED_AT,
        PAYLOAD_SOURCE_ID,
        PAYLOAD_SYMBOL,
        PAYLOAD_TEXT,
        PAYLOAD_TITLE,
        PAYLOAD_TOMBSTONED_AT,
        PAYLOAD_URI,
        PAYLOAD_WORKSPACE_ID,
    )

    doc_id = document_id or uuid.uuid4()
    src = source_id or uuid.uuid4()
    ext = external_id or f"ext-{uuid.uuid4().hex[:8]}"
    point_id = str(uuid.uuid4())
    payload = {
        PAYLOAD_WORKSPACE_ID: str(workspace_id),
        PAYLOAD_SOURCE_ID: str(src),
        PAYLOAD_DOCUMENT_ID: str(doc_id),
        PAYLOAD_EXTERNAL_ID: ext,
        PAYLOAD_URI: f"mem://{ext}",
        PAYLOAD_TITLE: ext,
        PAYLOAD_CONTENT_HASH: f"hash-{ext}",
        PAYLOAD_DOC_VERSION: 1,
        PAYLOAD_ORD: 0,
        PAYLOAD_TEXT: text,
        PAYLOAD_SYMBOL: None,
        PAYLOAD_METADATA: {},
        PAYLOAD_EMBEDDING_MODEL: "stub",
        PAYLOAD_EMBEDDING_PROVIDER: "stub",
        PAYLOAD_PARSER_VERSION: "stub",
        PAYLOAD_CHUNKER_STRATEGY: "stub",
        PAYLOAD_INGESTION_RUN_ID: None,
        PAYLOAD_TOMBSTONED_AT: None,
        PAYLOAD_RECORDED_AT: valid_from.isoformat(),
        PAYLOAD_VALID_FROM: valid_from.isoformat(),
        PAYLOAD_VALID_TO: valid_to.isoformat() if valid_to is not None else None,
        "doc_metadata": {},
    }
    point = qm.PointStruct(
        id=point_id,
        vector={NAMED_VECTOR_DENSE_PRIMARY: _stub_vector(text)},
        payload=payload,
    )
    client = cast("AsyncQdrantClient", store._client)  # type: ignore[attr-defined]
    await client.upsert(collection_name=store.collection_name, points=[point], wait=True)
    return doc_id


# ---------------------------------------------------------------------------
# Time anchors — three contiguous half-open windows tile the time axis.
# ---------------------------------------------------------------------------


_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
_T3 = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Core ``as_of`` correctness — search returns only the version valid at T.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_as_of_returns_only_chunks_valid_at_t(
    qdrant_store: QdrantVectorStore,
) -> None:
    """A chunk's three versions tile [T0,T1) [T1,T2) [T2, ∞); search@T returns one."""
    from omniscience_retrieval.models import SearchRequest

    ws = uuid.uuid4()
    text = "bitemporal vector search"
    # Three versions of the same logical chunk.  Same text so vector
    # ordering is deterministic; distinct external_ids so each version
    # is a separate Qdrant point.
    v1 = await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T0,
        valid_to=_T1,
        external_id="v1",
    )
    v2 = await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T1,
        valid_to=_T2,
        external_id="v2",
    )
    v3 = await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T2,
        valid_to=None,
        external_id="v3",
    )

    # @T0 → v1 only.
    res_t0 = await qdrant_store.search(
        request=SearchRequest(query=text, top_k=10),
        workspace_id=ws,
        as_of=_T0,
    )
    docs_t0 = {h.document_id for h in res_t0.hits}
    assert docs_t0 == {v1}

    # @T1 (boundary): v1 ended, v2 begins.  Open-closed -> v2 wins.
    res_t1 = await qdrant_store.search(
        request=SearchRequest(query=text, top_k=10),
        workspace_id=ws,
        as_of=_T1,
    )
    docs_t1 = {h.document_id for h in res_t1.hits}
    assert docs_t1 == {v2}

    # Mid-T1 → v2 only.
    mid_t1 = _T1 + timedelta(days=10)
    res_mid = await qdrant_store.search(
        request=SearchRequest(query=text, top_k=10),
        workspace_id=ws,
        as_of=mid_t1,
    )
    docs_mid = {h.document_id for h in res_mid.hits}
    assert docs_mid == {v2}

    # @T3 → v3 only (still-open valid_to=NULL).
    res_t3 = await qdrant_store.search(
        request=SearchRequest(query=text, top_k=10),
        workspace_id=ws,
        as_of=_T3,
    )
    docs_t3 = {h.document_id for h in res_t3.hits}
    assert docs_t3 == {v3}


# ---------------------------------------------------------------------------
# Boundary semantics — ADR-0008 §1 open-closed contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boundary_as_of_equal_valid_from_includes(
    qdrant_store: QdrantVectorStore,
) -> None:
    """``as_of == valid_from`` → the row IS included (``lte`` predicate)."""
    from omniscience_retrieval.models import SearchRequest

    ws = uuid.uuid4()
    text = "boundary lower"
    doc = await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T1,
        valid_to=_T2,
        external_id="boundary-lower",
    )
    result = await qdrant_store.search(
        request=SearchRequest(query=text, top_k=10),
        workspace_id=ws,
        as_of=_T1,
    )
    assert {h.document_id for h in result.hits} == {doc}


@pytest.mark.asyncio
async def test_boundary_as_of_equal_valid_to_excludes(
    qdrant_store: QdrantVectorStore,
) -> None:
    """``as_of == valid_to`` → the row is EXCLUDED (strict ``gt`` predicate)."""
    from omniscience_retrieval.models import SearchRequest

    ws = uuid.uuid4()
    text = "boundary upper"
    await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T0,
        valid_to=_T1,
        external_id="boundary-upper",
    )
    result = await qdrant_store.search(
        request=SearchRequest(query=text, top_k=10),
        workspace_id=ws,
        as_of=_T1,  # equals valid_to → excluded
    )
    assert result.hits == []


# ---------------------------------------------------------------------------
# Cross-workspace isolation under ``as_of`` — MANDATORY ACL contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_isolation_under_as_of(
    qdrant_store: QdrantVectorStore,
) -> None:
    """ADR-0006 §ACL + #134: workspace A's ``as_of`` filter never returns B's points.

    Setup:
      - Workspace A has a chunk valid in [T0, ∞).
      - Workspace B has a chunk with the SAME text and SAME validity
        window in [T0, ∞).
    A search in A @T2 must return only A's chunk; running the same
    query in B @T2 must return only B's chunk.  No ``as_of`` value or
    overlapping window may breach the workspace boundary.
    """
    from omniscience_retrieval.models import SearchRequest

    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    text = "cross-workspace under as_of"

    doc_a = await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws_a,
        text=text,
        valid_from=_T0,
        valid_to=None,
        external_id="docA",
    )
    doc_b = await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws_b,
        text=text,
        valid_from=_T0,
        valid_to=None,
        external_id="docB",
    )
    assert doc_a != doc_b

    request = SearchRequest(query=text, top_k=50)

    # Workspace A @T2 — only A's point.
    a_at_t2 = await qdrant_store.search(request=request, workspace_id=ws_a, as_of=_T2)
    a_docs = {h.document_id for h in a_at_t2.hits}
    assert doc_a in a_docs
    assert doc_b not in a_docs

    # Workspace B @T2 — only B's point.
    b_at_t2 = await qdrant_store.search(request=request, workspace_id=ws_b, as_of=_T2)
    b_docs = {h.document_id for h in b_at_t2.hits}
    assert doc_b in b_docs
    assert doc_a not in b_docs

    # Workspace C with no plant — empty regardless of as_of.
    ws_c = uuid.uuid4()
    c_at_t2 = await qdrant_store.search(request=request, workspace_id=ws_c, as_of=_T2)
    assert c_at_t2.hits == []

    # Even a far-future as_of cannot leak across workspaces.
    far_future = datetime(2099, 1, 1, tzinfo=UTC)
    a_far = await qdrant_store.search(request=request, workspace_id=ws_a, as_of=far_future)
    far_docs = {h.document_id for h in a_far.hits}
    assert doc_b not in far_docs


# ---------------------------------------------------------------------------
# No-regression — search(as_of=None) is byte-identical to pre-#134 contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_as_of_none_no_regression_on_filter_shape(
    qdrant_store: QdrantVectorStore,
) -> None:
    """The ``as_of=None`` filter shape after Stage 1 (refactor/bitemporal-vector-hybrid).

    Stage 1 amendment: ``as_of=None`` now includes ``with_current_only()``
    (``valid_to IS NULL``) so end-dated historical chunk versions from the
    write-path end-dating are excluded from current-state results.  The
    structural comparison is therefore against
    ``exclude_tombstoned().with_current_only()`` — not the plain pre-#134
    ``exclude_tombstoned()`` shape.  ``valid_from`` is still absent from
    the current-state filter; only ``valid_to IS NULL`` (an IsNullCondition,
    not a FieldCondition with a ``key``) is added.
    """
    from omniscience_index.stores.qdrant_constants import (
        PAYLOAD_VALID_FROM as _VF,
    )
    from omniscience_index.stores.qdrant_filters import QdrantFilterBuilder
    from omniscience_retrieval.models import SearchRequest
    from qdrant_client import models as qm

    ws = uuid.uuid4()
    request = SearchRequest(query="x", top_k=5)
    flt_with_none = qdrant_store._request_to_filter(  # type: ignore[attr-defined]
        request=request, workspace_id=ws, as_of=None
    )
    # Stage 1: current-state filter = workspace + exclude_tombstoned + current_only.
    flt_stage1 = (
        QdrantFilterBuilder(workspace_id=ws).exclude_tombstoned().with_current_only().build()
    )
    assert flt_with_none.model_dump() == flt_stage1.model_dump()
    # valid_from is NOT a FieldCondition key in the current-state filter.
    field_keys = [c.key for c in (flt_with_none.must or []) if isinstance(c, qm.FieldCondition)]
    assert _VF not in field_keys
    # valid_to IS present — but as an IsNullCondition, not a FieldCondition key.
    null_conditions = [c for c in (flt_with_none.must or []) if isinstance(c, qm.IsNullCondition)]
    # There should be exactly two IsNullCondition clauses:
    # one for tombstoned_at, one for valid_to.
    assert len(null_conditions) == 2


@pytest.mark.asyncio
async def test_as_of_none_returns_same_results_as_before(
    qdrant_store: QdrantVectorStore,
) -> None:
    """Behavioural no-regression: ``as_of=None`` matches the no-``as_of`` call.

    Same hit set, same order, same chunk ids — explicit ``None`` is
    indistinguishable from omitting the kwarg.
    """
    from omniscience_retrieval.models import SearchRequest

    ws = uuid.uuid4()
    src = uuid.uuid4()
    chunks: list[ChunkPayload] = [
        {"ord": 0, "text": "alpha quantum", "embedding": _stub_vector("alpha quantum")},
        {"ord": 1, "text": "beta quantum", "embedding": _stub_vector("beta quantum")},
    ]
    await qdrant_store.upsert_chunks(
        source_id=src,
        external_id="doc1",
        uri="mem://doc1",
        title=None,
        content_hash="h1",
        metadata={"workspace_id": str(ws)},
        chunks=chunks,
    )
    request = SearchRequest(query="alpha quantum", top_k=10)
    res_no_kw = await qdrant_store.search(request=request, workspace_id=ws)
    res_explicit_none = await qdrant_store.search(request=request, workspace_id=ws, as_of=None)
    assert [h.text for h in res_no_kw.hits] == [h.text for h in res_explicit_none.hits]
    assert {h.chunk_id for h in res_no_kw.hits} == {h.chunk_id for h in res_explicit_none.hits}


# ---------------------------------------------------------------------------
# Writer-side: every fresh upsert populates valid_from / valid_to defaults.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_populates_valid_from_and_null_valid_to(
    qdrant_store: QdrantVectorStore,
) -> None:
    """ADR-0008 §6 + #134: every chunk carries the bitemporal triple.

    ``valid_from = recorded_at`` (writer default), ``valid_to = None``
    (still-valid sentinel from ADR-0008 §1).
    """
    ws = uuid.uuid4()
    src = uuid.uuid4()
    await qdrant_store.upsert_chunks(
        source_id=src,
        external_id="docW",
        uri="mem://docW",
        title=None,
        content_hash="hW",
        metadata={"workspace_id": str(ws)},
        chunks=[{"ord": 0, "text": "hello", "embedding": _stub_vector("hello")}],
    )
    # Scroll the collection and inspect the planted point's payload.
    client = cast("AsyncQdrantClient", qdrant_store._client)  # type: ignore[attr-defined]
    records, _ = await client.scroll(
        collection_name=qdrant_store.collection_name,
        limit=10,
        with_payload=True,
        with_vectors=False,
    )
    assert len(records) == 1
    payload = records[0].payload or {}
    assert payload[PAYLOAD_VALID_FROM] is not None
    # valid_from defaults to recorded_at.
    assert payload[PAYLOAD_VALID_FROM] == payload["recorded_at"]
    # valid_to is the still-open sentinel (None / null).
    assert payload[PAYLOAD_VALID_TO] is None


# ---------------------------------------------------------------------------
# count + count_chunks — ``as_of`` is honoured on the stats path too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_chunks_honours_as_of(
    qdrant_store: QdrantVectorStore,
) -> None:
    """``count_chunks(as_of=T)`` returns the count valid at T, not the historical total."""
    ws = uuid.uuid4()
    # Three versions; only one is valid at T1.
    text = "count windowed"
    await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T0,
        valid_to=_T1,
        external_id="c1",
    )
    await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T1,
        valid_to=_T2,
        external_id="c2",
    )
    await _plant_chunk_with_window(
        qdrant_store,
        workspace_id=ws,
        text=text,
        valid_from=_T2,
        valid_to=None,
        external_id="c3",
    )
    # Stage 1 (refactor/bitemporal-vector-hybrid): count_chunks(as_of=None)
    # applies ``with_current_only()`` (valid_to IS NULL) so only the
    # still-open current generation is counted.  Of the three planted
    # chunks, only c3 has ``valid_to=None`` — c1 and c2 have explicit
    # closed windows that Stage 1 treats as historical versions.
    total = await qdrant_store.count_chunks(workspace_id=ws)
    at_t1 = await qdrant_store.count_chunks(workspace_id=ws, as_of=_T1)
    assert total == 1, (
        f"count_chunks(as_of=None) must count only current-gen points "
        f"(valid_to IS NULL); c1+c2 have closed valid_to, only c3 is current. "
        f"Got {total}."
    )
    assert at_t1 == 1
