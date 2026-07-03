"""Round-2 scope-gate: worker-path authz review of the outbox consumer's
DLQ/unpark backfill loop (``omniscience_server.outbox_consumer``).

Context
-------
The action-point rationale for this round is that no src/**/*.py was
sampled in round 1, so token-scope/workspace-ACL, ``as_of``, NATS
DLQ/retry, the reconcile worker, and the retention S3 path had no
correctness/authz sign-off.  This file covers the NATS DLQ/retry
("unpark") worker path in ``OutboxConsumerWorker._unpark_loop``.

``_unpark_loop`` periodically re-publishes long-parked entities/edges by
reading them back from Postgres.  For a stale edge, the query joins
``Source`` via the edge's *source* entity only (mirroring the identical
gap found in ``reconcile_worker._check_edge_drift``, see
``tests/test_reconcile_worker.py::test_edge_drift_query_pins_workspace_on_target_entity_too``)
and republishes the edge tagged with that source-side ``tenant_id`` —
without validating that the *target* entity belongs to the same
workspace.  There is no DB constraint forcing an edge's source and
target entities into the same workspace, so a corrupted/cross-tenant
edge would be re-published into the wrong tenant's outbox stream on
unpark, exactly the class of anomaly this backfill path exists to
recover from safely.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_server.outbox_consumer import OutboxConsumerWorker


def _build_consumer_with_stale_edge(
    *, session_execute_side_effect: Any
) -> tuple[OutboxConsumerWorker, uuid.UUID]:
    """Build an OutboxConsumerWorker with one edge already past the unpark timeout."""
    worker = OutboxConsumerWorker(
        nats_conn=MagicMock(),
        graph_store=AsyncMock(),
        vector_store=AsyncMock(),
        embedding_provider=AsyncMock(),
        session_factory=MagicMock(),
    )
    worker._running = True
    worker._unpark_timeout_seconds = 1.0

    stale_edge_id = uuid.uuid4()
    # Parked far enough in the past (relative to time.monotonic()) to be stale.
    worker._parked_edges = {stale_edge_id: time.monotonic() - 10_000.0}

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=session_execute_side_effect)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    worker._session_factory = MagicMock(return_value=cm)

    return worker, stale_edge_id


async def _run_one_unpark_tick(worker: OutboxConsumerWorker) -> None:
    """Drive ``_unpark_loop`` through exactly one iteration then stop it.

    ``_unpark_loop`` is an infinite ``while self._running`` loop that sleeps
    60s between ticks.  We patch the trailing ``asyncio.sleep`` call to raise
    ``CancelledError`` — the loop's own ``except asyncio.CancelledError:
    break`` then exits cleanly after exactly one full iteration's work,
    with no task-cancellation race.
    """

    async def _stop_after_tick(*_a: Any, **_kw: Any) -> None:
        raise asyncio.CancelledError()

    with (
        patch("omniscience_server.outbox_consumer.QueueProducer", return_value=AsyncMock()),
        patch("asyncio.sleep", side_effect=_stop_after_tick),
    ):
        await worker._unpark_loop()


@pytest.mark.asyncio
async def test_unpark_loop_edge_query_pins_workspace_on_target_entity_too() -> None:
    """The stale-edge backfill query must validate tenancy on BOTH edge endpoints.

    White-box regression: capture the SELECT statement ``_unpark_loop`` issues
    for stale edges and assert it consults the entities/sources tables more
    than once in its FROM/JOIN clause — i.e. it re-validates the target
    entity's workspace, not only the source entity's.  Today it only joins
    once, so this fails.
    """
    captured: list[Any] = []

    async def _capture_execute(stmt: Any, *_a: Any, **_kw: Any) -> list[Any]:
        captured.append(stmt)
        return []  # no rows needed — we only inspect the query shape

    worker, _stale_edge_id = _build_consumer_with_stale_edge(
        session_execute_side_effect=_capture_execute
    )

    await _run_one_unpark_tick(worker)

    assert captured, "expected the stale-edge branch to issue a SELECT"
    edge_stmt = captured[-1]
    compiled_sql = str(edge_stmt.compile(compile_kwargs={"literal_binds": False}))
    from_clause = compiled_sql.split("WHERE")[0]
    join_hits = re.findall(r"\bjoin\s+entities\b", from_clause, re.IGNORECASE)
    assert len(join_hits) >= 2, (
        "the unpark stale-edge query only joins Entity/Source via the "
        "edge's SOURCE entity (one 'JOIN entities'); it must also "
        "join/validate the TARGET entity's tenant before republishing the "
        "edge tagged with this workspace_id.\n"
        f"Compiled SQL:\n{compiled_sql}"
    )
