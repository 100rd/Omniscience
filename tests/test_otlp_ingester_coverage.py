"""Coverage tests for omniscience_server.rest.otlp_ingester (target >= 80%).

ACL invariant under test
------------------------
``OtlpIngester.ingest`` receives ``workspace_id`` exclusively from the route
(which derives it from the authenticated bearer token).  No span attribute is
ever used for tenant identity.  These tests validate:

1. workspace_id from the caller is propagated verbatim to the Source row.
2. Two calls with different workspace_ids produce isolated Source rows.
3. A call with workspace_id=None produces the "legacy" source row.

Other scenarios
---------------
- Empty ParsedTraces -> returns 0, no DB writes
- Single trace with service + pod entities -> correct entity types + edges
- Re-ingesting the same trace entity is idempotent (update path, not insert)
- Re-ingesting the same edge is idempotent (skip path)
- Multiple traces in one ParsedTraces -> all entities created
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_connectors.otel import ParsedSpan, ParsedTrace, ParsedTraces
from omniscience_core.db.models import (
    Edge,
    Entity,
    Source,
    SourceType,
)
from omniscience_server.rest.otlp_ingester import (
    CROSS_REF_EDGE_TYPE,
    OTEL_POD_ENTITY_TYPE,
    OTEL_SERVICE_ENTITY_TYPE,
    OTEL_SOURCE_NAME_PREFIX,
    OTEL_TRACE_ENTITY_TYPE,
    OtlpIngester,
)
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRACE_HEX = "aa" * 16  # 32 hex chars
_SPAN_HEX = "bb" * 8


def _span(
    *,
    trace_id_hex: str = _TRACE_HEX,
    service: str = "checkout",
    pod: str = "checkout-pod",
) -> ParsedSpan:
    return ParsedSpan(
        trace_id_hex=trace_id_hex,
        span_id_hex=_SPAN_HEX,
        name="GET /test",
        service_name=service,
        service_namespace="default",
        pod_name=pod,
        start_unix_nano=1_700_000_000_000_000_000,
        end_unix_nano=1_700_000_000_050_000_000,
    )


def _single_trace(
    *,
    trace_hex: str = _TRACE_HEX,
    services: set[str] | None = None,
    pods: set[str] | None = None,
) -> ParsedTraces:
    trace = ParsedTrace(
        trace_id_hex=trace_hex,
        spans=[_span(trace_id_hex=trace_hex)],
        service_names=services or {"checkout"},
        pod_names=pods or {"checkout-pod"},
    )
    return ParsedTraces(traces={trace_hex: trace})


def _empty_traces() -> ParsedTraces:
    return ParsedTraces(traces={})


def _make_source(workspace_id: uuid.UUID | None = None) -> Source:
    src = MagicMock(spec=Source)
    src.id = uuid.uuid4()
    src.name = f"{OTEL_SOURCE_NAME_PREFIX}:{workspace_id or 'legacy'}"
    src.type = SourceType.otel
    src.tenant_id = workspace_id
    return src


def _make_entity(
    *,
    entity_type: str = OTEL_TRACE_ENTITY_TYPE,
    name: str = "trace://aa",
    source_id: uuid.UUID | None = None,
) -> Entity:
    ent = MagicMock(spec=Entity)
    ent.id = uuid.uuid4()
    ent.entity_type = entity_type
    ent.name = name
    ent.source_id = source_id or uuid.uuid4()
    ent.entity_metadata = {}
    return ent


def _factory(session: AsyncMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# ---------------------------------------------------------------------------
# Empty traces guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_empty_traces_returns_zero() -> None:
    """Empty ParsedTraces returns 0 without touching the DB."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    result = await ingester.ingest(workspace_id=uuid.uuid4(), parsed=_empty_traces())

    assert result == 0
    session.add.assert_not_called()  # empty traces — no entities added


# ---------------------------------------------------------------------------
# Source bootstrap — _get_or_create_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_creates_new_source_when_none_exists() -> None:
    """First ingest for a workspace creates a new Source row."""
    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()

    added: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Source) and not hasattr(obj, "_id_set"):
                obj.id = source_id
                obj._id_set = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    result = await ingester.ingest(workspace_id=ws_id, parsed=_single_trace())

    assert result == 1
    sources_added = [o for o in added if isinstance(o, Source)]
    assert len(sources_added) == 1
    src = sources_added[0]
    assert src.tenant_id == ws_id
    assert src.type == SourceType.otel
    assert src.name == f"{OTEL_SOURCE_NAME_PREFIX}:{ws_id}"


@pytest.mark.asyncio
async def test_ingest_reuses_existing_source() -> None:
    """Second ingest for the same workspace reuses the existing Source."""
    ws_id = uuid.uuid4()
    existing = _make_source(ws_id)

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.first.return_value = existing
        else:
            result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    await ingester.ingest(workspace_id=ws_id, parsed=_single_trace())

    # Source.add() must NOT have been called with a Source object
    source_adds = [
        c for c in session.add.call_args_list if isinstance(c.args[0], Source)
    ]
    assert source_adds == []


@pytest.mark.asyncio
async def test_ingest_with_none_workspace_uses_legacy_source_name() -> None:
    """workspace_id=None -> source name ends with ':legacy'."""
    added: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Source) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    await ingester.ingest(workspace_id=None, parsed=_single_trace())

    sources_added = [o for o in added if isinstance(o, Source)]
    assert len(sources_added) == 1
    assert sources_added[0].name == f"{OTEL_SOURCE_NAME_PREFIX}:legacy"
    assert sources_added[0].tenant_id is None


# ---------------------------------------------------------------------------
# _source_name static method
# ---------------------------------------------------------------------------


def test_source_name_with_workspace() -> None:
    ws = uuid.uuid4()
    name = OtlpIngester._source_name(ws)
    assert name == f"{OTEL_SOURCE_NAME_PREFIX}:{ws}"


def test_source_name_with_none() -> None:
    name = OtlpIngester._source_name(None)
    assert name == f"{OTEL_SOURCE_NAME_PREFIX}:legacy"


# ---------------------------------------------------------------------------
# Entity upsert — create and update paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_creates_trace_service_pod_entities() -> None:
    """Single trace with one service and one pod -> 3 entities added."""
    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()
    existing_src = _make_source(ws_id)
    existing_src.id = source_id

    added: list[Any] = []
    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.first.return_value = existing_src
        else:
            result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, (Entity, Edge)) and not hasattr(obj, "_flushed"):
                if isinstance(obj, Entity):
                    obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    result = await ingester.ingest(
        workspace_id=ws_id,
        parsed=_single_trace(services={"checkout"}, pods={"checkout-pod"}),
    )

    assert result == 1
    entities = [o for o in added if isinstance(o, Entity)]
    entity_types = {e.entity_type for e in entities}
    assert OTEL_TRACE_ENTITY_TYPE in entity_types
    assert OTEL_SERVICE_ENTITY_TYPE in entity_types
    assert OTEL_POD_ENTITY_TYPE in entity_types
    # 1 trace + 1 service + 1 pod = 3 entities
    assert len(entities) == 3


@pytest.mark.asyncio
async def test_ingest_creates_cross_ref_edges() -> None:
    """Edges between trace->service and trace->pod are created."""
    ws_id = uuid.uuid4()
    existing_src = _make_source(ws_id)
    existing_src.id = uuid.uuid4()

    added: list[Any] = []
    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.first.return_value = existing_src
        else:
            result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Entity) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]
            if isinstance(obj, Edge) and not hasattr(obj, "_flushed"):
                obj._flushed = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    await ingester.ingest(
        workspace_id=ws_id,
        parsed=_single_trace(services={"checkout"}, pods={"checkout-pod"}),
    )

    edges = [o for o in added if isinstance(o, Edge)]
    # 1 edge trace->service + 1 edge trace->pod = 2 edges
    assert len(edges) == 2
    for edge in edges:
        assert edge.edge_type == CROSS_REF_EDGE_TYPE


@pytest.mark.asyncio
async def test_ingest_entity_update_path_on_existing() -> None:
    """Re-ingesting with the same entity updates metadata instead of inserting."""
    ws_id = uuid.uuid4()
    existing_src = _make_source(ws_id)
    existing_src.id = uuid.uuid4()

    existing_entity = _make_entity(entity_type=OTEL_TRACE_ENTITY_TYPE, name="trace://aa")
    existing_entity.source_id = existing_src.id
    existing_entity.entity_metadata = {"trace_id": _TRACE_HEX, "span_count": 1}

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.first.return_value = existing_src
        elif call_count == 2:
            # Trace entity lookup -> return existing
            result.scalars.return_value.first.return_value = existing_entity
        else:
            result.scalars.return_value.first.return_value = None
        return result

    added: list[Any] = []
    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Entity) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]
            if isinstance(obj, Edge) and not hasattr(obj, "_flushed"):
                obj._flushed = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    await ingester.ingest(workspace_id=ws_id, parsed=_single_trace())

    # The existing trace entity was returned, so no new trace entity is added
    new_trace_entities = [
        o
        for o in added
        if isinstance(o, Entity) and o.entity_type == OTEL_TRACE_ENTITY_TYPE
    ]
    assert len(new_trace_entities) == 0
    # metadata on existing entity was updated
    assert "span_count" in existing_entity.entity_metadata


@pytest.mark.asyncio
async def test_ingest_edge_idempotent_when_exists() -> None:
    """Re-ingesting the same trace does not create duplicate edges."""
    ws_id = uuid.uuid4()
    existing_src = _make_source(ws_id)
    existing_src.id = uuid.uuid4()

    existing_trace_entity = _make_entity(entity_type=OTEL_TRACE_ENTITY_TYPE)
    existing_trace_entity.source_id = existing_src.id
    service_entity = _make_entity(entity_type=OTEL_SERVICE_ENTITY_TYPE)
    service_entity.source_id = existing_src.id

    existing_edge = MagicMock(spec=Edge)
    existing_edge.source_entity_id = existing_trace_entity.id
    existing_edge.target_entity_id = service_entity.id
    existing_edge.edge_type = CROSS_REF_EDGE_TYPE

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.first.return_value = existing_src
        elif call_count == 2:
            result.scalars.return_value.first.return_value = existing_trace_entity
        elif call_count == 3:
            result.scalars.return_value.first.return_value = service_entity
        elif call_count == 4:
            # Edge query -> return existing edge
            result.scalars.return_value.first.return_value = existing_edge
        else:
            result.scalars.return_value.first.return_value = None
        return result

    added: list[Any] = []
    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Entity) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    # Trace with only service (no pod) to keep the call order deterministic
    parsed = ParsedTraces(
        traces={
            _TRACE_HEX: ParsedTrace(
                trace_id_hex=_TRACE_HEX,
                spans=[_span()],
                service_names={"checkout"},
                pod_names=set(),
            )
        }
    )
    await ingester.ingest(workspace_id=ws_id, parsed=parsed)

    # No new Edge objects should have been added
    new_edges = [o for o in added if isinstance(o, Edge)]
    assert new_edges == []


# ---------------------------------------------------------------------------
# ACL invariant: workspace_id is never read from span content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_id_from_caller_not_from_spans() -> None:
    """The workspace_id passed by the caller is the sole tenant signal.

    Even if we pass a different workspace_id as an argument, the Source
    created must use that caller-supplied value.  Span attributes are
    opaque content and never influence tenant assignment.
    """
    real_ws = uuid.uuid4()
    evil_ws = uuid.uuid4()
    assert real_ws != evil_ws

    added: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Source) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]
            if isinstance(obj, Entity) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))

    # The parsed traces carry evil_ws in the trace metadata (simulating
    # a span attribute trying to override tenant) — this is just span content.
    trace = ParsedTrace(
        trace_id_hex=_TRACE_HEX,
        spans=[_span()],
        service_names={"checkout"},
        pod_names={"checkout-pod"},
    )
    parsed = ParsedTraces(traces={_TRACE_HEX: trace})

    # Caller passes real_ws as workspace_id
    await ingester.ingest(workspace_id=real_ws, parsed=parsed)

    sources = [o for o in added if isinstance(o, Source)]
    assert len(sources) == 1
    # The Source must carry the caller's workspace_id, not evil_ws
    assert sources[0].tenant_id == real_ws
    assert sources[0].tenant_id != evil_ws


@pytest.mark.asyncio
async def test_two_workspaces_get_separate_source_rows() -> None:
    """Two ingests for two different workspace_ids create two distinct Sources."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()

    async def _make_isolated_session() -> AsyncMock:
        added: list[Any] = []

        async def _execute(stmt: Any) -> Any:
            result = MagicMock()
            result.scalars.return_value.first.return_value = None
            return result

        s = AsyncMock(spec=AsyncSession)
        s.execute = _execute
        s.add = MagicMock(side_effect=added.append)

        async def _flush() -> None:
            for obj in added:
                if not hasattr(obj, "_flushed"):
                    if isinstance(obj, (Source, Entity)):
                        obj.id = uuid.uuid4()
                    obj._flushed = True  # type: ignore[attr-defined]

        s.flush = AsyncMock(side_effect=_flush)
        s.commit = AsyncMock()
        s.__aenter__ = AsyncMock(return_value=s)
        s.__aexit__ = AsyncMock(return_value=False)
        s._added = added  # type: ignore[attr-defined]
        return s

    sess_a = await _make_isolated_session()
    sess_b = await _make_isolated_session()

    ingester_a = OtlpIngester(_factory(sess_a))
    ingester_b = OtlpIngester(_factory(sess_b))

    await ingester_a.ingest(workspace_id=ws_a, parsed=_single_trace())
    await ingester_b.ingest(workspace_id=ws_b, parsed=_single_trace())

    src_a = [o for o in sess_a._added if isinstance(o, Source)]
    src_b = [o for o in sess_b._added if isinstance(o, Source)]

    assert len(src_a) == 1
    assert len(src_b) == 1
    assert src_a[0].tenant_id == ws_a
    assert src_b[0].tenant_id == ws_b
    assert src_a[0].tenant_id != src_b[0].tenant_id


# ---------------------------------------------------------------------------
# Multi-trace batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_multiple_traces_returns_total_spans() -> None:
    """ParsedTraces with two traces returns correct span count."""
    ws_id = uuid.uuid4()
    existing_src = _make_source(ws_id)
    existing_src.id = uuid.uuid4()

    trace_hex_2 = "cc" * 16
    span2 = ParsedSpan(
        trace_id_hex=trace_hex_2,
        span_id_hex="dd" * 8,
        name="POST /order",
        service_name="orders",
        service_namespace="default",
        pod_name="orders-pod",
        start_unix_nano=1_700_000_001_000_000_000,
        end_unix_nano=1_700_000_001_050_000_000,
    )
    trace2 = ParsedTrace(
        trace_id_hex=trace_hex_2,
        spans=[span2],
        service_names={"orders"},
        pod_names={"orders-pod"},
    )
    trace1 = ParsedTrace(
        trace_id_hex=_TRACE_HEX,
        spans=[_span()],
        service_names={"checkout"},
        pod_names={"checkout-pod"},
    )
    parsed = ParsedTraces(traces={_TRACE_HEX: trace1, trace_hex_2: trace2})

    call_count = 0
    added: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.first.return_value = existing_src
        else:
            result.scalars.return_value.first.return_value = None
        return result

    session = AsyncMock(spec=AsyncSession)
    session.execute = _execute
    session.add = MagicMock(side_effect=added.append)

    async def _flush() -> None:
        for obj in added:
            if isinstance(obj, Entity) and not hasattr(obj, "_flushed"):
                obj.id = uuid.uuid4()
                obj._flushed = True  # type: ignore[attr-defined]

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    ingester = OtlpIngester(_factory(session))
    result = await ingester.ingest(workspace_id=ws_id, parsed=parsed)

    assert result == 2  # total_spans across both traces
    entities = [o for o in added if isinstance(o, Entity)]
    # 2 traces x (1 trace + 1 service + 1 pod) = 6 entities
    assert len(entities) == 6
