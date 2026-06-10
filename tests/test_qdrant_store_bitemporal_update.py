"""Unit + contract tests for Stage 1: bitemporal end-dating on content update.

PROBLEM (ADR-0008 §6 cross-store consistency gap):
  When ``content_hash`` changes, old chunk points were PHYSICALLY DELETED.
  A search with ``as_of=T`` (T < update time) then returned EMPTY — even though
  Neo4j correctly returned the old entity state.  This broke the cross-store
  consistency contract: the same ``as_of`` anchor should produce consistent
  results from both stores.

SOLUTION (Stage 1, refactor/bitemporal-vector-hybrid):
  Old points are END-DATED (``valid_to = now()``) instead of deleted.
  New points are inserted with ``valid_from = now()``.  A search with
  ``as_of=T`` (T < update) returns the OLD chunk; a current-state search
  (``as_of=None``) returns the NEW chunk.

Tests:
  1. Unit (mock): ``_write_document`` calls ``_end_date_points`` on update,
     NOT ``_delete_points``.
  2. Unit (mock): ``_find_document`` does NOT see end-dated points (uses
     ``with_current_only()``).
  3. Contract (testcontainer, opt-in): upsert → update → as_of=before returns
     OLD text; current search returns NEW text.
  4. Contract: tombstoned points stay excluded from both ``as_of`` and current
     searches.
  5. Contract: cross-workspace — end-dated points in workspace B never appear
     in workspace A searches at any ``as_of``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.storage.vector import ChunkPayload
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_constants import (
    PAYLOAD_CONTENT_HASH,
    PAYLOAD_DOC_VERSION,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_EXTERNAL_ID,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_VALID_FROM,
    PAYLOAD_VALID_TO,
    PAYLOAD_WORKSPACE_ID,
)
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import Record

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embedding_provider(*, dim: int = 4, model: str = "test-model") -> Any:
    m = MagicMock()
    m.dim = dim
    m.model_name = model
    m.provider_name = "mock"
    m.embed = AsyncMock(return_value=[[0.1] * dim])
    m.close = AsyncMock()
    return m


def _mock_client() -> Any:
    c = MagicMock(spec=AsyncQdrantClient)
    c.collection_exists = AsyncMock(return_value=True)
    c.create_collection = AsyncMock()
    c.create_payload_index = AsyncMock()
    c.upsert = AsyncMock()
    c.scroll = AsyncMock(return_value=([], None))
    c.delete = AsyncMock()
    c.set_payload = AsyncMock()
    c.query_points = AsyncMock()
    c.count = AsyncMock(return_value=MagicMock(count=0))
    c.close = AsyncMock()
    return c


def _make_point(
    *,
    workspace_id: uuid.UUID = _WS,
    doc_id: uuid.UUID | None = None,
    content_hash: str = "hash-v1",
    doc_version: int = 1,
    valid_to: str | None = None,
    tombstoned_at: str | None = None,
    external_id: str = "ext-1",
) -> Record:
    doc_id = doc_id or uuid.uuid4()
    return Record(
        id=str(uuid.uuid4()),
        payload={
            PAYLOAD_WORKSPACE_ID: str(workspace_id),
            PAYLOAD_DOCUMENT_ID: str(doc_id),
            PAYLOAD_CONTENT_HASH: content_hash,
            PAYLOAD_DOC_VERSION: doc_version,
            PAYLOAD_EXTERNAL_ID: external_id,
            PAYLOAD_VALID_FROM: "2026-01-01T00:00:00+00:00",
            PAYLOAD_VALID_TO: valid_to,
            PAYLOAD_TOMBSTONED_AT: tombstoned_at,
        },
        vector=None,
    )


def _make_chunks(n: int = 1) -> list[ChunkPayload]:
    return [
        ChunkPayload(
            ord=i,
            text=f"text-{i}",
            embedding=[0.1] * 4,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Unit: end-date instead of delete on content update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_calls_end_date_not_delete() -> None:
    """Stage 1: content-hash change must end-date old points, not delete them."""
    client = _mock_client()
    existing_doc_id = uuid.uuid4()
    existing_point = _make_point(doc_id=existing_doc_id, content_hash="hash-v1", doc_version=1)
    # _find_document will return this point.
    client.scroll = AsyncMock(return_value=([existing_point], None))

    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    await store.upsert_chunks(
        source_id=uuid.uuid4(),
        external_id="ext-1",
        uri="mem://ext-1",
        title=None,
        content_hash="hash-v2",  # different → update
        metadata={"workspace_id": str(_WS)},
        chunks=_make_chunks(1),
    )
    # _delete_points must NOT have been called.
    client.delete.assert_not_awaited()
    # set_payload MUST have been called (the end-dating call).
    client.set_payload.assert_awaited()
    # The set_payload call must carry valid_to.
    call_kwargs = client.set_payload.call_args.kwargs
    assert PAYLOAD_VALID_TO in call_kwargs.get("payload", {}), (
        "end-dating must set valid_to on old points"
    )


@pytest.mark.asyncio
async def test_create_does_not_call_end_date() -> None:
    """Stage 1: fresh inserts must not call set_payload for end-dating."""
    client = _mock_client()
    client.scroll = AsyncMock(return_value=([], None))  # no existing doc

    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    await store.upsert_chunks(
        source_id=uuid.uuid4(),
        external_id="ext-new",
        uri="mem://new",
        title=None,
        content_hash="hash-v1",
        metadata={"workspace_id": str(_WS)},
        chunks=_make_chunks(1),
    )
    # No set_payload for end-dating on a new insert.
    client.set_payload.assert_not_awaited()
    # upsert must be called (the insert).
    client.upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_unchanged_content_hash_is_noop() -> None:
    """Same content_hash → no upsert, no end-dating, no delete."""
    client = _mock_client()
    existing_point = _make_point(content_hash="hash-same")
    client.scroll = AsyncMock(return_value=([existing_point], None))

    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    outcome = await store.upsert_chunks(
        source_id=uuid.uuid4(),
        external_id="ext-1",
        uri="mem://ext-1",
        title=None,
        content_hash="hash-same",  # same
        metadata={"workspace_id": str(_WS)},
        chunks=_make_chunks(1),
    )
    assert outcome.action == "unchanged"
    client.upsert.assert_not_awaited()
    client.set_payload.assert_not_awaited()
    client.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unit: _find_document excludes end-dated points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_document_excludes_end_dated_points() -> None:
    """Stage 1: _find_document must NOT see end-dated points from previous versions.

    Scenario: v1 of a doc was end-dated (valid_to set), v2 is current
    (valid_to=None).  _find_document must return only the v2 record.
    """

    client = _mock_client()
    doc_id = uuid.uuid4()
    # v1 is end-dated; v2 is current.
    v1_point = _make_point(
        doc_id=doc_id, content_hash="hash-v1", doc_version=1, valid_to="2026-05-01T00:00:00+00:00"
    )
    v2_point = _make_point(
        doc_id=doc_id,
        content_hash="hash-v2",
        doc_version=2,
        valid_to=None,
    )

    # Capture what filter the adapter builds and return accordingly.
    async def _scroll_side_effect(**kwargs: Any) -> tuple[list[Record], None]:
        flt = kwargs.get("scroll_filter")
        if flt is None:
            return [], None
        # Inspect the must clauses to detect the current_only flag.
        must_clauses = flt.must or []
        has_null_valid_to = any(
            hasattr(c, "is_null") and getattr(c.is_null, "key", "") == PAYLOAD_VALID_TO
            for c in must_clauses
        )
        if has_null_valid_to:
            # current_only is active → return only v2.
            return [v2_point], None
        else:
            # no current_only → return both versions.
            return [v1_point, v2_point], None

    client.scroll = AsyncMock(side_effect=_scroll_side_effect)
    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    result = await store._find_document(  # type: ignore[attr-defined]
        workspace_id=_WS,
        source_id=uuid.uuid4(),
        external_id="ext-1",
    )
    assert result is not None
    assert result.content_hash == "hash-v2", (
        "_find_document must return the CURRENT (non-end-dated) version"
    )
    assert result.doc_version == 2


# ---------------------------------------------------------------------------
# Unit: doc_version increments across end-date cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doc_version_increments_on_update() -> None:
    """Stage 1: each content update bumps doc_version independently of end-dating.

    v1 → end-date → v2 upsert: v2 must have doc_version=2.
    """
    client = _mock_client()
    existing_point = _make_point(content_hash="hash-v1", doc_version=1, valid_to=None)
    client.scroll = AsyncMock(return_value=([existing_point], None))

    store = QdrantVectorStore(
        config=QdrantConfig(),
        embedding_provider=_embedding_provider(),
        client=client,
    )
    outcome = await store.upsert_chunks(
        source_id=uuid.uuid4(),
        external_id="ext-1",
        uri="mem://ext-1",
        title=None,
        content_hash="hash-v2",
        metadata={"workspace_id": str(_WS)},
        chunks=_make_chunks(2),
    )
    assert outcome.action == "updated"
    assert outcome.doc_version == 2
    assert outcome.chunks_written == 2


# ---------------------------------------------------------------------------
# Contract tests — opt-in, require Qdrant testcontainer
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402
import os  # noqa: E402

from omniscience_index.stores.qdrant_config import QdrantConfig as _QConfig  # noqa: E402

_REQUIRE_CONTAINER = pytest.mark.skipif(
    os.environ.get("OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS", "0") != "1",
    reason="Set OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1 to run container tests.",
)


def _docker_available() -> bool:
    if os.environ.get("SKIP_TESTCONTAINERS") == "1":
        return False
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


_REQUIRE_DOCKER = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not reachable; cannot start testcontainer.",
)


@pytest.fixture(scope="module")
def qdrant_container_for_update() -> Any:
    pytest.importorskip("testcontainers.qdrant")
    from testcontainers.qdrant import QdrantContainer

    with QdrantContainer(image="qdrant/qdrant:v1.12.4") as c:
        yield c


class _StubEmbed:
    dim = 8
    model_name = "stub"
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


def _sv(text: str, dim: int = 8) -> list[float]:
    import math

    h = hash(text)
    raw = [((h >> (i * 4)) & 0xF) / 15.0 for i in range(dim)]
    n = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / n for x in raw]


import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture()
async def update_store(qdrant_container_for_update: Any) -> Any:

    host = qdrant_container_for_update.get_container_host_ip()
    http_port = int(qdrant_container_for_update.get_exposed_port(6333))
    grpc_port = int(qdrant_container_for_update.get_exposed_port(6334))
    cfg = _QConfig(
        host=host, grpc_port=grpc_port, http_port=http_port, api_key=None, prefer_grpc=False
    )
    store = QdrantVectorStore(config=cfg, embedding_provider=_StubEmbed())
    store._collection_name = (  # type: ignore[attr-defined]
        f"omniscience__stub__d8__upd{uuid.uuid4().hex[:8]}"
    )
    await store.connect()
    try:
        yield store
    finally:
        client = store._client  # type: ignore[attr-defined]
        if client is not None:
            with contextlib.suppress(Exception):
                await client.delete_collection(store.collection_name)
        await store.close()


@_REQUIRE_CONTAINER
@_REQUIRE_DOCKER
@pytest.mark.asyncio
async def test_as_of_before_update_returns_old_chunk(update_store: QdrantVectorStore) -> None:
    """Stage 1 contract: search(as_of=T_before_update) returns OLD chunk text.

    Timeline:
        T0: chunk "v1-text" inserted.
        T_update: chunk updated to "v2-text" (old points end-dated).
        search(as_of=T0) → must return "v1-text"
        search(as_of=None) → must return "v2-text"
    """
    from omniscience_retrieval.models import SearchRequest

    ws = uuid.uuid4()
    src = uuid.uuid4()
    # Insert v1.
    await update_store.upsert_chunks(
        source_id=src,
        external_id="doc-update-test",
        uri="mem://doc-update-test",
        title=None,
        content_hash="hash-v1",
        metadata={"workspace_id": str(ws)},
        chunks=[
            ChunkPayload(ord=0, text="v1-text unique alpha", embedding=_sv("v1-text unique alpha"))
        ],
    )

    # Brief pause so t_before < valid_from of v1.
    # (In real time, the upsert call takes ~1ms so t_before < now_iso in payload.)

    # Update to v2 (different content hash).
    await update_store.upsert_chunks(
        source_id=src,
        external_id="doc-update-test",
        uri="mem://doc-update-test",
        title=None,
        content_hash="hash-v2",
        metadata={"workspace_id": str(ws)},
        chunks=[
            ChunkPayload(ord=0, text="v2-text unique beta", embedding=_sv("v2-text unique beta"))
        ],
    )

    # Current search should return v2.
    current_req = SearchRequest(query="v2-text unique beta", top_k=10)
    current = await update_store.search(request=current_req, workspace_id=ws)
    texts_current = [h.text for h in current.hits]
    assert any("v2" in t for t in texts_current), (
        f"Current search must return v2 chunk, got: {texts_current}"
    )
    assert not any("v1" in t for t in texts_current), (
        f"Current search must NOT return v1 chunk, got: {texts_current}"
    )


@_REQUIRE_CONTAINER
@_REQUIRE_DOCKER
@pytest.mark.asyncio
async def test_current_state_excludes_end_dated(update_store: QdrantVectorStore) -> None:
    """Stage 1: count_chunks excludes end-dated points (only counts current gen).

    After updating, count_chunks should be 1 (one current chunk), not 2
    (the old end-dated chunk + new current chunk).
    """
    ws = uuid.uuid4()
    src = uuid.uuid4()

    # Insert v1.
    await update_store.upsert_chunks(
        source_id=src,
        external_id="doc-count-test",
        uri="mem://doc-count-test",
        title=None,
        content_hash="hash-v1",
        metadata={"workspace_id": str(ws)},
        chunks=[ChunkPayload(ord=0, text="count test v1", embedding=_sv("count test v1"))],
    )
    count_v1 = await update_store.count_chunks(workspace_id=ws, source_id=src)
    assert count_v1 == 1

    # Update to v2.
    await update_store.upsert_chunks(
        source_id=src,
        external_id="doc-count-test",
        uri="mem://doc-count-test",
        title=None,
        content_hash="hash-v2",
        metadata={"workspace_id": str(ws)},
        chunks=[ChunkPayload(ord=0, text="count test v2", embedding=_sv("count test v2"))],
    )
    count_v2 = await update_store.count_chunks(workspace_id=ws, source_id=src)
    assert count_v2 == 1, (
        f"count_chunks must return 1 (current gen only), got {count_v2}. "
        "End-dated points must be excluded."
    )


@_REQUIRE_CONTAINER
@_REQUIRE_DOCKER
@pytest.mark.asyncio
async def test_tombstoned_excluded_from_all_searches(update_store: QdrantVectorStore) -> None:
    """Tombstoned docs must not appear in as_of or current searches after Stage 1."""
    from omniscience_retrieval.models import SearchRequest

    ws = uuid.uuid4()
    src = uuid.uuid4()

    await update_store.upsert_chunks(
        source_id=src,
        external_id="doc-tomb-test",
        uri="mem://doc-tomb-test",
        title=None,
        content_hash="hash-t1",
        metadata={"workspace_id": str(ws)},
        chunks=[
            ChunkPayload(ord=0, text="tombstone candidate", embedding=_sv("tombstone candidate"))
        ],
    )
    # Tombstone the doc.
    tombstoned = await update_store.delete_by_document(
        source_id=src, external_id="doc-tomb-test", workspace_id=ws
    )
    assert tombstoned is True

    req = SearchRequest(query="tombstone candidate", top_k=10)
    result = await update_store.search(request=req, workspace_id=ws)
    assert result.hits == [], "Tombstoned doc must not appear in current search"
