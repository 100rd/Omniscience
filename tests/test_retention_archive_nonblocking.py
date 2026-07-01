"""Retention archive path must not block the asyncio event loop.

The retention worker runs as a persistent asyncio task inside the
FastAPI/Uvicorn process (lifespan-driven, ADR-0009 §3) — it shares the
event loop with API request handling.  boto3 is synchronous, and the
archive phase performs three potentially slow synchronous steps per
snapshot date:

1. ``build_s3_client``     — botocore loads service models from disk
                             (~100ms+ cold);
2. ``build_parquet_bytes`` — CPU-bound pyarrow serialisation, scales
                             with snapshot size;
3. ``write_archive_object``— the S3 PUT itself (network-bound).

Acceptance criterion (review action point): NONE of these may execute
on the event loop thread — each must be offloaded (``asyncio.to_thread``
or ``run_in_executor``), so a slow archive tick cannot degrade API
latency.

The tests patch the three helpers *in the worker's module namespace*
with fakes that record the executing thread, drive one real
``run_once`` with an archive-eligible snapshot date, and assert the
thread is not the loop's own.  A final responsiveness test blocks all
three helpers with ``time.sleep`` and asserts a heartbeat task on the
loop keeps ticking throughout.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.config import Settings

import omniscience_server.retention_worker as retention_worker_module
from omniscience_server.retention_worker import RetentionWorker

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_retention_worker.py conventions)
# ---------------------------------------------------------------------------


_NOW: datetime = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_WS_A: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ARCHIVE_DATE: date = date(2025, 1, 1)


def _make_settings(**overrides: Any) -> Settings:
    """Settings with an archive bucket configured so the S3 phase runs."""
    base: dict[str, Any] = {
        "retention_hot_days": 90,
        "retention_warm_days": 365,
        "retention_archive_years": 7,
        "retention_tick_seconds": 60,
        "retention_batch_size": 500,
        "retention_dry_run": False,
        "retention_enabled": True,
        "retention_archive_bucket": "test-archive-bucket",
        "retention_archive_kms_key_arn": None,
        "retention_archive_s3_endpoint_url": None,
        "retention_archive_s3_region": "us-east-1",
        "retention_sample_size": 5,
    }
    base.update(overrides)
    return Settings(**base)


def _session_factory_for(*workspace_ids: uuid.UUID) -> Any:
    """Mock SQLAlchemy session factory yielding the given workspace ids."""
    session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.all.return_value = [(wid,) for wid in workspace_ids]
        return result

    session.execute = _execute
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_graph_store() -> AsyncMock:
    """Graph store with exactly one archive-eligible snapshot date."""
    store = AsyncMock()
    store.count_hot_to_warm_eligible = AsyncMock(return_value=(0, 0))
    store.count_warm_to_archive_eligible = AsyncMock(return_value=3)
    store.list_warm_to_archive_dates = AsyncMock(return_value=[_ARCHIVE_DATE])
    store.sample_hot_to_warm_eligible = AsyncMock(return_value=[])
    store.oldest_hot_to_warm_recorded_at = AsyncMock(return_value=None)
    store.mark_hot_to_warm = AsyncMock(return_value=(0, 0))
    store.move_hot_to_warm = AsyncMock(return_value=(0, 0))
    store.fetch_warm_snapshot_rows = AsyncMock(
        return_value=(
            [{"id": "ent-1", "metadata": "{}"}],
            [{"source_entity_id": "ent-1", "target_entity_id": "ent-2", "metadata": "{}"}],
        )
    )
    store.delete_warm_snapshot = AsyncMock(return_value=(3, 1))
    store.count_records_by_tier = AsyncMock(return_value={"hot": 0, "warm": 3})
    return store


def _make_vector_store() -> AsyncMock:
    store = AsyncMock()
    store.count_retention_eligible = AsyncMock(return_value=0)
    store.mark_retention_warm = AsyncMock(return_value=0)
    store.delete_retention_archive = AsyncMock(return_value=0)
    store.count_chunks_by_tier = AsyncMock(return_value={"hot": 0, "warm": 0})
    return store


def _make_worker(settings: Settings | None = None) -> RetentionWorker:
    return RetentionWorker(
        session_factory=_session_factory_for(_WS_A),
        graph_store=_make_graph_store(),
        vector_store=_make_vector_store(),
        settings=settings or _make_settings(),
    )


# ---------------------------------------------------------------------------
# Thread-placement assertions: each sync archive step runs OFF the loop
# ---------------------------------------------------------------------------


async def test_s3_put_runs_off_event_loop_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """``write_archive_object`` (boto3 PUT) must execute on a worker thread.

    Regression guard for the review action point: boto3 is synchronous;
    running the PUT on the loop thread stalls every in-flight API
    request for the duration of the upload.
    """
    loop_thread = threading.get_ident()
    put_threads: list[int] = []

    def fake_write(**_kwargs: Any) -> None:
        put_threads.append(threading.get_ident())

    monkeypatch.setattr(retention_worker_module, "write_archive_object", fake_write)
    monkeypatch.setattr(retention_worker_module, "build_s3_client", lambda **_kw: object())

    await _make_worker().run_once(now=_NOW)

    assert put_threads, "archive phase never invoked the S3 write"
    assert all(t != loop_thread for t in put_threads), (
        "write_archive_object executed on the event loop thread — the "
        "synchronous boto3 PUT must be offloaded (asyncio.to_thread / "
        "run_in_executor) so it cannot block API request handling."
    )


async def test_s3_client_construction_runs_off_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_s3_client`` must execute on a worker thread.

    ``boto3.client("s3")`` loads botocore service models from disk and
    resolves credentials — synchronous work that can take hundreds of
    milliseconds cold.  It must not run on the loop thread.
    """
    loop_thread = threading.get_ident()
    client_threads: list[int] = []

    def fake_build_client(**_kwargs: Any) -> Any:
        client_threads.append(threading.get_ident())
        return object()

    monkeypatch.setattr(retention_worker_module, "build_s3_client", fake_build_client)
    monkeypatch.setattr(retention_worker_module, "write_archive_object", lambda **_kw: None)

    await _make_worker().run_once(now=_NOW)

    assert client_threads, "archive phase never constructed the S3 client"
    assert all(t != loop_thread for t in client_threads), (
        "build_s3_client executed on the event loop thread — boto3 client "
        "construction is synchronous (disk I/O + credential resolution) "
        "and must be offloaded off the loop."
    )


async def test_parquet_serialisation_runs_off_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_parquet_bytes`` must execute on a worker thread.

    Parquet serialisation is CPU-bound pyarrow work that scales with
    snapshot size; on the loop thread a large snapshot day stalls the
    API for the whole encode.
    """
    loop_thread = threading.get_ident()
    build_threads: list[int] = []

    def fake_build_parquet(**_kwargs: Any) -> bytes:
        build_threads.append(threading.get_ident())
        return b"\x00\x00\x00\x00"

    monkeypatch.setattr(retention_worker_module, "build_parquet_bytes", fake_build_parquet)
    monkeypatch.setattr(retention_worker_module, "build_s3_client", lambda **_kw: object())
    monkeypatch.setattr(retention_worker_module, "write_archive_object", lambda **_kw: None)

    await _make_worker().run_once(now=_NOW)

    assert build_threads, "archive phase never serialised the parquet payload"
    assert all(t != loop_thread for t in build_threads), (
        "build_parquet_bytes executed on the event loop thread — CPU-bound "
        "pyarrow serialisation must be offloaded off the loop."
    )


# ---------------------------------------------------------------------------
# End-to-end responsiveness: a slow archive tick must not stall the loop
# ---------------------------------------------------------------------------


async def test_event_loop_stays_responsive_during_slow_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat coroutine keeps ticking while every archive step is slow.

    Simulates the failure mode from the review action point end-to-end:
    each synchronous archive helper blocks for ``_BLOCK_S`` seconds
    (cold boto3 client, big parquet encode, slow S3 PUT).  If any of
    them runs on the loop thread, the heartbeat gap jumps to at least
    ``_BLOCK_S``; when all are offloaded, the loop keeps scheduling and
    the max observed gap stays far below it.
    """
    _BLOCK_S = 0.25
    _MAX_ALLOWED_GAP_S = 0.15  # << _BLOCK_S; generous vs. scheduler jitter

    def slow_build_client(**_kwargs: Any) -> Any:
        time.sleep(_BLOCK_S)
        return object()

    def slow_build_parquet(**_kwargs: Any) -> bytes:
        time.sleep(_BLOCK_S)
        return b"\x00\x00\x00\x00"

    def slow_write(**_kwargs: Any) -> None:
        time.sleep(_BLOCK_S)

    monkeypatch.setattr(retention_worker_module, "build_s3_client", slow_build_client)
    monkeypatch.setattr(retention_worker_module, "build_parquet_bytes", slow_build_parquet)
    monkeypatch.setattr(retention_worker_module, "write_archive_object", slow_write)

    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    hb_task = asyncio.create_task(heartbeat())
    try:
        await _make_worker().run_once(now=_NOW)
    finally:
        stop.set()
        await hb_task

    assert gaps, "heartbeat never ticked"
    max_gap = max(gaps)
    assert max_gap < _MAX_ALLOWED_GAP_S, (
        f"event loop stalled for {max_gap:.3f}s during the archive phase "
        f"(allowed {_MAX_ALLOWED_GAP_S}s) — a synchronous archive step "
        "(boto3 client build, parquet encode, or S3 PUT) is running on "
        "the loop thread instead of a worker thread."
    )
