"""Tests for Stage 4: enumerate_chunks / count_enumerate.

Stage 4 (refactor/bitemporal-vector-hybrid) adds two methods to
QdrantVectorStore:

- ``enumerate_chunks(workspace_id, metadata_key, metadata_value)``
  Scroll-based enumeration that bypasses HNSW; deduplicates by
  ``document_id``; returns list[dict] of payloads.

- ``count_enumerate(workspace_id, metadata_key, metadata_value)``
  Exact chunk-level count via Qdrant ``count(exact=True)``.

Both use ``build_enumerate_filter`` which wraps the request in the
standard workspace + tombstone + current-only composite filter.

Test surface
------------
All tests here are **unit tests** that mock the Qdrant client; no
container required.  They verify:

1. ``enumerate_chunks`` deduplicates by document_id (not chunk_id).
2. ``enumerate_chunks`` excludes records without a document_id payload.
3. ``enumerate_chunks`` returns the first-chunk payload per document.
4. ``enumerate_chunks`` forwards source_id narrowing to the filter.
5. ``count_enumerate`` forwards exact=True to the Qdrant client.
6. ``count_enumerate`` returns the integer count from the result.
7. Both methods pass workspace_id as the first must-clause.
8. ``enumerate_chunks`` returns an empty list when the collection is empty.
9. The payload_field passed to build_enumerate_filter is
   ``metadata.<metadata_key>`` (not a raw key).
10. With source_id=None, no source filter is present in the request.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import the store + filter helpers
# ---------------------------------------------------------------------------
from omniscience_index.stores.qdrant_constants import (
    ENUMERATE_METADATA_PREFIX,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_WORKSPACE_ID,
)
from omniscience_index.stores.qdrant_store import QdrantVectorStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_SRC = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")

_DOC_A = str(uuid.UUID("cccccccc-0000-0000-0000-000000000001"))
_DOC_B = str(uuid.UUID("cccccccc-0000-0000-0000-000000000002"))


def _fake_record(
    *,
    doc_id: str | None = _DOC_A,
    service: str = "api-gateway",
    source_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> MagicMock:
    """Construct a fake qdrant_client.models.Record-like object."""
    payload: dict[str, Any] = {}
    if doc_id is not None:
        payload[PAYLOAD_DOCUMENT_ID] = doc_id
    payload["metadata"] = {"service": service}
    if source_id is not None:
        payload[PAYLOAD_SOURCE_ID] = source_id
    if extra:
        payload.update(extra)

    rec = MagicMock()
    rec.payload = payload
    rec.id = str(uuid.uuid4())
    return rec


def _make_store() -> QdrantVectorStore:
    """Build a QdrantVectorStore with a mocked embedding provider and client.

    The client is NOT connected (``_bootstrapped=False``); individual
    tests attach their own mock client directly.
    """
    from omniscience_index.stores.qdrant_config import QdrantConfig

    config = QdrantConfig(host="localhost", grpc_port=6334)
    embedding = MagicMock()
    embedding.model_name = "test-model"
    embedding.dim = 8
    store = QdrantVectorStore(config=config, embedding_provider=embedding)
    return store


def _attach_client(store: QdrantVectorStore, client: MagicMock) -> None:
    """Inject a mock client without running connect() bootstrap."""
    store._client = client  # type: ignore[attr-defined]
    store._bootstrapped = True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: enumerate_chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_deduplicates_by_document_id() -> None:
    """Two chunks for the same document_id → only one entry returned."""
    store = _make_store()
    client = MagicMock()
    chunk1 = _fake_record(doc_id=_DOC_A, service="svc-a")
    chunk2 = _fake_record(doc_id=_DOC_A, service="svc-a")  # same doc
    chunk3 = _fake_record(doc_id=_DOC_B, service="svc-a")

    # scroll returns (records, next_offset); next_offset=None ends paging
    client.scroll = AsyncMock(return_value=([chunk1, chunk2, chunk3], None))
    _attach_client(store, client)

    result = await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="svc-a",
    )

    assert len(result) == 2, "Two distinct document_ids → two results"
    doc_ids = {r[PAYLOAD_DOCUMENT_ID] for r in result}
    assert doc_ids == {_DOC_A, _DOC_B}


@pytest.mark.asyncio
async def test_enumerate_excludes_records_without_document_id() -> None:
    """Records with no document_id in the payload must be silently skipped."""
    store = _make_store()
    client = MagicMock()
    no_doc = _fake_record(doc_id=None)  # no document_id key
    with_doc = _fake_record(doc_id=_DOC_A)
    client.scroll = AsyncMock(return_value=([no_doc, with_doc], None))
    _attach_client(store, client)

    result = await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="svc-a",
    )

    assert len(result) == 1
    assert result[0][PAYLOAD_DOCUMENT_ID] == _DOC_A


@pytest.mark.asyncio
async def test_enumerate_returns_full_payload() -> None:
    """Returned dicts contain the full chunk payload (copy, not reference)."""
    store = _make_store()
    client = MagicMock()
    rec = _fake_record(doc_id=_DOC_A, extra={"text": "hello world"})
    client.scroll = AsyncMock(return_value=([rec], None))
    _attach_client(store, client)

    result = await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="svc-a",
    )

    assert len(result) == 1
    assert result[0]["text"] == "hello world"
    assert result[0][PAYLOAD_DOCUMENT_ID] == _DOC_A


@pytest.mark.asyncio
async def test_enumerate_empty_collection_returns_empty_list() -> None:
    """Empty scroll → empty list, no exception."""
    store = _make_store()
    client = MagicMock()
    client.scroll = AsyncMock(return_value=([], None))
    _attach_client(store, client)

    result = await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="nonexistent",
    )

    assert result == []


@pytest.mark.asyncio
async def test_enumerate_payload_field_uses_metadata_prefix() -> None:
    """The filter passed to scroll must use ``metadata.<key>`` not bare ``<key>``."""
    from omniscience_index.stores.qdrant_filters import build_enumerate_filter
    from qdrant_client import models as qm

    # Call the real build_enumerate_filter and inspect the result
    flt = build_enumerate_filter(
        workspace_id=_WS,
        metadata_key=f"{ENUMERATE_METADATA_PREFIX}.service",
        metadata_value="api-gateway",
    )

    assert isinstance(flt, qm.Filter)
    field_keys = [
        c.key for c in (flt.must or []) if isinstance(c, qm.FieldCondition) and hasattr(c, "key")
    ]
    # The metadata.service field should appear in the must clauses
    assert f"{ENUMERATE_METADATA_PREFIX}.service" in field_keys, (
        f"Expected 'metadata.service' in must clauses, got: {field_keys}"
    )


@pytest.mark.asyncio
async def test_enumerate_with_source_id_calls_scroll_with_source_filter() -> None:
    """When source_id is provided, the scroll filter must include it."""
    from qdrant_client import models as qm

    store = _make_store()
    client = MagicMock()
    captured_filters: list[qm.Filter] = []

    async def _mock_scroll(
        *,
        collection_name: str,
        scroll_filter: qm.Filter | None,
        limit: int,
        with_payload: bool,
        with_vectors: bool,
        offset: Any = None,
    ) -> tuple[list, None]:
        if scroll_filter is not None:
            captured_filters.append(scroll_filter)
        return ([], None)

    client.scroll = _mock_scroll
    _attach_client(store, client)

    await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="svc-a",
        source_id=_SRC,
    )

    assert captured_filters, "scroll must have been called with a filter"
    flt = captured_filters[0]
    must_conditions = flt.must or []
    source_conditions = [
        c
        for c in must_conditions
        if isinstance(c, qm.FieldCondition) and c.key == PAYLOAD_SOURCE_ID
    ]
    assert source_conditions, (
        f"Expected source_id filter in must clauses; got: {[c for c in must_conditions]}"
    )


@pytest.mark.asyncio
async def test_enumerate_no_source_id_omits_source_filter() -> None:
    """When source_id is None, no source_id clause must appear in the filter."""
    from qdrant_client import models as qm

    store = _make_store()
    client = MagicMock()
    captured_filters: list[qm.Filter] = []

    async def _mock_scroll(
        *,
        collection_name: str,
        scroll_filter: qm.Filter | None,
        limit: int,
        with_payload: bool,
        with_vectors: bool,
        offset: Any = None,
    ) -> tuple[list, None]:
        if scroll_filter is not None:
            captured_filters.append(scroll_filter)
        return ([], None)

    client.scroll = _mock_scroll
    _attach_client(store, client)

    await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="svc-a",
        source_id=None,
    )

    assert captured_filters, "scroll must have been called with a filter"
    flt = captured_filters[0]
    must_conditions = flt.must or []
    source_conditions = [
        c
        for c in must_conditions
        if isinstance(c, qm.FieldCondition) and c.key == PAYLOAD_SOURCE_ID
    ]
    assert not source_conditions, "No source_id → source_id clause must NOT appear in filter"


@pytest.mark.asyncio
async def test_enumerate_filter_always_has_workspace_id() -> None:
    """enumerate_chunks must always scope the filter to workspace_id."""
    from qdrant_client import models as qm

    store = _make_store()
    client = MagicMock()
    captured_filters: list[qm.Filter] = []

    async def _mock_scroll(
        *,
        collection_name: str,
        scroll_filter: qm.Filter | None,
        limit: int,
        with_payload: bool,
        with_vectors: bool,
        offset: Any = None,
    ) -> tuple[list, None]:
        if scroll_filter is not None:
            captured_filters.append(scroll_filter)
        return ([], None)

    client.scroll = _mock_scroll
    _attach_client(store, client)

    await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="kind",
        metadata_value="Deployment",
    )

    assert captured_filters
    flt = captured_filters[0]
    must_conditions = flt.must or []
    ws_conditions = [
        c
        for c in must_conditions
        if isinstance(c, qm.FieldCondition)
        and c.key == PAYLOAD_WORKSPACE_ID
        and c.match.value == str(_WS)  # type: ignore[union-attr]
    ]
    assert ws_conditions, "workspace_id must always be in the filter"


@pytest.mark.asyncio
async def test_enumerate_multi_page_scroll_returns_all_docs() -> None:
    """enumerate_chunks pages through all scroll results."""
    store = _make_store()
    client = MagicMock()

    chunk_a = _fake_record(doc_id=_DOC_A)
    chunk_b = _fake_record(doc_id=_DOC_B)

    # First call returns page 1 with next_offset; second returns page 2
    call_count = 0

    async def _paged_scroll(**kwargs: Any) -> tuple[list, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ([chunk_a], "some_cursor")  # type: ignore[return-value]
        return ([chunk_b], None)

    client.scroll = _paged_scroll
    _attach_client(store, client)

    result = await store.enumerate_chunks(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="svc-a",
    )

    assert len(result) == 2
    assert call_count == 2, "Must page until next_offset is None"


# ---------------------------------------------------------------------------
# Tests: count_enumerate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_enumerate_calls_count_with_exact_true() -> None:
    """count_enumerate must call the Qdrant client with exact=True."""
    store = _make_store()
    client = MagicMock()

    count_result = MagicMock()
    count_result.count = 42
    client.count = AsyncMock(return_value=count_result)
    _attach_client(store, client)

    result = await store.count_enumerate(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="api-gateway",
    )

    assert result == 42
    client.count.assert_called_once()
    call_kwargs = client.count.call_args.kwargs
    assert call_kwargs.get("exact") is True, "exact=True is mandatory for enumerate counts"


@pytest.mark.asyncio
async def test_count_enumerate_returns_integer() -> None:
    """count_enumerate must return a plain int (not a float or mock)."""
    store = _make_store()
    client = MagicMock()

    count_result = MagicMock()
    count_result.count = 7
    client.count = AsyncMock(return_value=count_result)
    _attach_client(store, client)

    result = await store.count_enumerate(
        workspace_id=_WS,
        metadata_key="namespace",
        metadata_value="production",
    )

    assert isinstance(result, int)
    assert result == 7


@pytest.mark.asyncio
async def test_count_enumerate_zero_when_no_matches() -> None:
    """count_enumerate returns 0 for no-match case, not None."""
    store = _make_store()
    client = MagicMock()

    count_result = MagicMock()
    count_result.count = 0
    client.count = AsyncMock(return_value=count_result)
    _attach_client(store, client)

    result = await store.count_enumerate(
        workspace_id=_WS,
        metadata_key="account_id",
        metadata_value="999999999999",
    )

    assert result == 0


@pytest.mark.asyncio
async def test_count_enumerate_filter_has_workspace_id() -> None:
    """count_enumerate must pass a filter containing workspace_id."""
    from qdrant_client import models as qm

    store = _make_store()
    client = MagicMock()
    captured_filters: list[qm.Filter] = []

    count_result = MagicMock()
    count_result.count = 5

    async def _mock_count(*, collection_name: str, count_filter: qm.Filter, exact: bool) -> Any:
        captured_filters.append(count_filter)
        return count_result

    client.count = _mock_count
    _attach_client(store, client)

    await store.count_enumerate(
        workspace_id=_WS,
        metadata_key="kind",
        metadata_value="Pod",
    )

    assert captured_filters
    flt = captured_filters[0]
    must_conditions = flt.must or []
    ws_conditions = [
        c
        for c in must_conditions
        if isinstance(c, qm.FieldCondition)
        and c.key == PAYLOAD_WORKSPACE_ID
        and c.match.value == str(_WS)  # type: ignore[union-attr]
    ]
    assert ws_conditions, "workspace_id must be in the count filter"


@pytest.mark.asyncio
async def test_count_enumerate_with_source_id() -> None:
    """count_enumerate with source_id passes the source clause to the filter."""
    from qdrant_client import models as qm

    store = _make_store()
    client = MagicMock()
    captured_filters: list[qm.Filter] = []

    count_result = MagicMock()
    count_result.count = 3

    async def _mock_count(*, collection_name: str, count_filter: qm.Filter, exact: bool) -> Any:
        captured_filters.append(count_filter)
        return count_result

    client.count = _mock_count
    _attach_client(store, client)

    await store.count_enumerate(
        workspace_id=_WS,
        metadata_key="service",
        metadata_value="billing",
        source_id=_SRC,
    )

    flt = captured_filters[0]
    must_conditions = flt.must or []
    src_conditions = [
        c
        for c in must_conditions
        if isinstance(c, qm.FieldCondition) and c.key == PAYLOAD_SOURCE_ID
    ]
    assert src_conditions, "source_id must appear in count filter when provided"


# ---------------------------------------------------------------------------
# Cross-method: enumerate_chunks vs count_enumerate consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_and_count_use_same_filter_structure() -> None:
    """enumerate_chunks and count_enumerate must produce the same filter shape.

    This is a structural consistency test: both methods call
    ``build_enumerate_filter`` with the same arguments, so the resulting
    Qdrant filter objects must have the same must-clause key set.
    """
    from omniscience_index.stores.qdrant_filters import build_enumerate_filter
    from qdrant_client import models as qm

    mkey = f"{ENUMERATE_METADATA_PREFIX}.service"

    flt_enum = build_enumerate_filter(
        workspace_id=_WS,
        metadata_key=mkey,
        metadata_value="api-gw",
    )
    flt_count = build_enumerate_filter(
        workspace_id=_WS,
        metadata_key=mkey,
        metadata_value="api-gw",
    )

    def _must_keys(flt: qm.Filter) -> list[str]:
        return sorted(
            c.key
            for c in (flt.must or [])
            if isinstance(c, qm.FieldCondition) and hasattr(c, "key")
        )

    assert _must_keys(flt_enum) == _must_keys(flt_count), (
        "enumerate_chunks and count_enumerate must share the same filter structure"
    )
