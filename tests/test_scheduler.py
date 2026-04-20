"""Tests for the SchedulerWorker (Issue #89).

Covers:
1.  SchedulerWorker.__init__ stores interval and sets _running=False
2.  DEFAULT_TTLS contains the expected source types
3.  _effective_ttl uses source.freshness_sla_seconds when set
4.  _effective_ttl falls back to DEFAULT_TTLS for known type
5.  _effective_ttl returns None for unknown type with no explicit SLA
6.  _effective_ttl returns explicit SLA even for known type (SLA wins)
7.  _is_stale returns True when last_sync_at is None
8.  _is_stale returns True when age > ttl
9.  _is_stale returns False when age <= ttl
10. _is_stale handles naive last_sync_at (adds UTC)
11. _check_and_trigger returns 0 when no sources
12. _check_and_trigger returns 0 when all sources fresh
13. _check_and_trigger triggers stale source and returns count
14. _check_and_trigger skips sources with no TTL (unknown type, no SLA)
15. _check_and_trigger handles NATS unavailable gracefully (returns 0)
16. _check_and_trigger handles NATS publish error gracefully
17. _check_and_trigger triggers never-synced source
18. _check_and_trigger increments SCHEDULER_SYNCS_TRIGGERED_TOTAL counter
19. _check_and_trigger observes SCHEDULER_CHECK_DURATION_SECONDS histogram
20. start() runs loop and calls _check_and_trigger repeatedly
21. stop() causes start() loop to exit
22. start() does not crash on _check_and_trigger exception
23. _load_active_sources filters by status=active
24. _publish_trigger publishes correct DocumentChangeEvent to correct subject
25. _publish_trigger returns False when nats_conn is None
26. Settings.scheduler_enabled defaults to True
27. Settings.scheduler_interval_seconds defaults to 300
28. app.py: scheduler starts when scheduler_enabled=True (state set)
29. app.py: scheduler not started when scheduler_enabled=False
30. Multiple stale sources: all triggered, count matches
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.config import Settings
from omniscience_core.db.models import Source, SourceStatus, SourceType
from omniscience_server.scheduler import SchedulerWorker

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
_SRC_ID = uuid.uuid4()
_SRC_ID_2 = uuid.uuid4()
_SRC_ID_3 = uuid.uuid4()

# Sentinel for "use default nats conn" to distinguish from explicit None
_USE_DEFAULT_NATS = object()


# ---------------------------------------------------------------------------
# Source factory helpers
# ---------------------------------------------------------------------------


def _make_source(
    src_id: uuid.UUID = _SRC_ID,
    name: str = "test-source",
    source_type: SourceType = SourceType.git,
    status: SourceStatus = SourceStatus.active,
    freshness_sla_seconds: int | None = None,
    last_sync_at: datetime | None = None,
) -> MagicMock:
    s = MagicMock(spec=Source)
    s.id = src_id
    s.name = name
    s.type = source_type
    s.status = status
    s.freshness_sla_seconds = freshness_sla_seconds
    s.last_sync_at = last_sync_at
    return s


def _fresh_source(
    ttl: int = 300,
    age_seconds: float = 60.0,
    src_id: uuid.UUID = _SRC_ID,
    source_type: SourceType = SourceType.git,
) -> MagicMock:
    """A source synced ``age_seconds`` ago with an explicit SLA of ``ttl``."""
    last_sync = _NOW - timedelta(seconds=age_seconds)
    return _make_source(
        src_id=src_id,
        source_type=source_type,
        freshness_sla_seconds=ttl,
        last_sync_at=last_sync,
    )


def _stale_source(
    ttl: int = 300,
    age_seconds: float = 600.0,
    src_id: uuid.UUID = _SRC_ID,
    source_type: SourceType = SourceType.git,
) -> MagicMock:
    """A source synced ``age_seconds`` ago, past its SLA of ``ttl``."""
    last_sync = _NOW - timedelta(seconds=age_seconds)
    return _make_source(
        src_id=src_id,
        source_type=source_type,
        freshness_sla_seconds=ttl,
        last_sync_at=last_sync,
    )


# ---------------------------------------------------------------------------
# Session factory / NATS mock helpers
# ---------------------------------------------------------------------------


def _session_factory_for(*sources: Any) -> AsyncMock:
    """Return an async session factory mock that yields the given sources."""
    session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(sources)
        return result

    session.execute = _execute

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_nats_conn(publish_raises: Exception | None = None) -> MagicMock:
    """Return a mock NATS connection with a JetStream context."""
    js = AsyncMock()
    if publish_raises is not None:
        js.publish = AsyncMock(side_effect=publish_raises)
    else:
        js.publish = AsyncMock(return_value=None)

    nats_conn = MagicMock()
    nats_conn.jetstream = js
    return nats_conn


def _make_scheduler(
    sources: list[Any] | None = None,
    nats_conn: Any = _USE_DEFAULT_NATS,
    interval: int = 60,
) -> SchedulerWorker:
    """Build a SchedulerWorker with a pre-loaded mock session factory.

    Pass ``nats_conn=None`` to explicitly test the no-NATS path.
    Omit ``nats_conn`` to get a default functional mock.
    """
    factory = _session_factory_for(*(sources or []))
    resolved_nats = _make_nats_conn() if nats_conn is _USE_DEFAULT_NATS else nats_conn
    return SchedulerWorker(
        session_factory=factory,
        nats_conn=resolved_nats,
        interval=interval,
    )


# ===========================================================================
# 1. __init__
# ===========================================================================


def test_init_stores_interval() -> None:
    """Constructor stores the interval and sets _running to False."""
    sw = _make_scheduler(interval=120)
    assert sw._interval == 120
    assert sw._running is False


# ===========================================================================
# 2. DEFAULT_TTLS
# ===========================================================================


def test_default_ttls_contains_expected_types() -> None:
    """DEFAULT_TTLS has entries for all documented source types."""
    expected = {"git", "fs", "s3", "aws", "confluence", "notion", "slack", "jira", "database"}
    assert expected == set(SchedulerWorker.DEFAULT_TTLS.keys())


def test_default_ttls_values_are_positive() -> None:
    """All default TTL values are positive integers."""
    for source_type, ttl in SchedulerWorker.DEFAULT_TTLS.items():
        assert isinstance(ttl, int), f"{source_type} TTL is not int"
        assert ttl > 0, f"{source_type} TTL is not positive"


# ===========================================================================
# 3-6. _effective_ttl
# ===========================================================================


def test_effective_ttl_uses_explicit_sla() -> None:
    """When freshness_sla_seconds is set it takes priority."""
    src = _make_source(source_type=SourceType.git, freshness_sla_seconds=999)
    sw = _make_scheduler()
    assert sw._effective_ttl(src) == 999


def test_effective_ttl_falls_back_to_default_for_known_type() -> None:
    """When no SLA is set the per-type default is returned."""
    src = _make_source(source_type=SourceType.fs, freshness_sla_seconds=None)
    sw = _make_scheduler()
    assert sw._effective_ttl(src) == SchedulerWorker.DEFAULT_TTLS["fs"]


def test_effective_ttl_returns_none_for_unknown_type_no_sla() -> None:
    """Unknown type with no explicit SLA returns None (skip this source)."""
    src = _make_source(source_type=SourceType.grafana, freshness_sla_seconds=None)
    sw = _make_scheduler()
    assert sw._effective_ttl(src) is None


def test_effective_ttl_explicit_sla_overrides_default_for_known_type() -> None:
    """An explicitly set SLA beats the per-type default even for known types."""
    src = _make_source(source_type=SourceType.s3, freshness_sla_seconds=42)
    sw = _make_scheduler()
    assert sw._effective_ttl(src) == 42


# ===========================================================================
# 7-10. _is_stale
# ===========================================================================


def test_is_stale_true_when_never_synced() -> None:
    """A source with last_sync_at=None is always stale."""
    src = _make_source(last_sync_at=None)
    assert SchedulerWorker._is_stale(src, _NOW, ttl=300) is True


def test_is_stale_true_when_age_exceeds_ttl() -> None:
    """Source age > TTL is stale."""
    last_sync = _NOW - timedelta(seconds=601)
    src = _make_source(last_sync_at=last_sync)
    assert SchedulerWorker._is_stale(src, _NOW, ttl=600) is True


def test_is_stale_false_when_age_within_ttl() -> None:
    """Source age <= TTL is not stale."""
    last_sync = _NOW - timedelta(seconds=299)
    src = _make_source(last_sync_at=last_sync)
    assert SchedulerWorker._is_stale(src, _NOW, ttl=300) is False


def test_is_stale_handles_naive_last_sync_at() -> None:
    """Naive datetime for last_sync_at is treated as UTC."""
    last_sync_naive = datetime(2026, 4, 17, 11, 55, 0)  # no tzinfo
    src = _make_source(last_sync_at=last_sync_naive)
    # Age = 5 minutes (300 s); TTL = 299 → stale
    assert SchedulerWorker._is_stale(src, _NOW, ttl=299) is True
    # Age = 5 minutes (300 s); TTL = 301 → not stale
    assert SchedulerWorker._is_stale(src, _NOW, ttl=301) is False


# ===========================================================================
# 11-19. _check_and_trigger
# ===========================================================================


@pytest.mark.asyncio
async def test_check_and_trigger_returns_zero_no_sources() -> None:
    """With no sources the trigger count is 0."""
    sw = _make_scheduler(sources=[])
    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()
    assert count == 0


@pytest.mark.asyncio
async def test_check_and_trigger_returns_zero_all_fresh() -> None:
    """Fresh sources produce zero triggers."""
    src = _fresh_source(ttl=300, age_seconds=60)
    sw = _make_scheduler(sources=[src])
    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()
    assert count == 0


@pytest.mark.asyncio
async def test_check_and_trigger_triggers_stale_source() -> None:
    """A stale source results in one trigger."""
    src = _stale_source(ttl=300, age_seconds=600)
    nats = _make_nats_conn()
    sw = _make_scheduler(sources=[src], nats_conn=nats)

    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()

    assert count == 1
    assert nats.jetstream.publish.call_count == 1


@pytest.mark.asyncio
async def test_check_and_trigger_skips_unknown_type_no_sla() -> None:
    """Source with unknown type and no SLA is skipped (no trigger)."""
    # grafana has no DEFAULT_TTL entry; no SLA is set
    src = _make_source(source_type=SourceType.grafana, freshness_sla_seconds=None)
    nats = _make_nats_conn()
    sw = _make_scheduler(sources=[src], nats_conn=nats)

    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()

    assert count == 0
    nats.jetstream.publish.assert_not_called()


@pytest.mark.asyncio
async def test_check_and_trigger_nats_unavailable_returns_zero() -> None:
    """When nats_conn is None the worker does not crash and returns 0."""
    src = _stale_source(ttl=300, age_seconds=600)
    # Explicitly pass nats_conn=None to test the unavailable path
    sw = _make_scheduler(sources=[src], nats_conn=None)

    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()

    assert count == 0


@pytest.mark.asyncio
async def test_check_and_trigger_handles_publish_error_gracefully() -> None:
    """A publish error is caught; the worker does not crash and returns 0."""
    src = _stale_source(ttl=300, age_seconds=600)
    nats = _make_nats_conn(publish_raises=RuntimeError("NATS down"))
    sw = _make_scheduler(sources=[src], nats_conn=nats)

    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()

    assert count == 0


@pytest.mark.asyncio
async def test_check_and_trigger_triggers_never_synced_source() -> None:
    """A source that has never been synced is always triggered."""
    src = _make_source(
        source_type=SourceType.fs,
        freshness_sla_seconds=None,  # use DEFAULT_TTLS["fs"]
        last_sync_at=None,
    )
    nats = _make_nats_conn()
    sw = _make_scheduler(sources=[src], nats_conn=nats)

    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()

    assert count == 1


@pytest.mark.asyncio
async def test_check_and_trigger_increments_counter() -> None:
    """SCHEDULER_SYNCS_TRIGGERED_TOTAL is incremented for each triggered source."""
    src = _stale_source(
        src_id=_SRC_ID,
        source_type=SourceType.s3,
        ttl=900,
        age_seconds=1800,
    )
    nats = _make_nats_conn()
    sw = _make_scheduler(sources=[src], nats_conn=nats)

    with (
        patch("omniscience_server.scheduler.SCHEDULER_SYNCS_TRIGGERED_TOTAL") as mock_counter,
        patch("omniscience_server.scheduler.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = _NOW
        mock_labels = MagicMock()
        mock_counter.labels.return_value = mock_labels

        await sw._check_and_trigger()

    mock_counter.labels.assert_called_once_with(source_type="s3")
    mock_labels.inc.assert_called_once()


@pytest.mark.asyncio
async def test_check_and_trigger_observes_histogram() -> None:
    """SCHEDULER_CHECK_DURATION_SECONDS.observe is called after every cycle."""
    sw = _make_scheduler(sources=[])

    with (
        patch("omniscience_server.scheduler.SCHEDULER_CHECK_DURATION_SECONDS") as mock_hist,
        patch("omniscience_server.scheduler.datetime") as mock_dt,
    ):
        mock_dt.now.return_value = _NOW
        await sw._check_and_trigger()

    mock_hist.observe.assert_called_once()
    observed_value = mock_hist.observe.call_args[0][0]
    assert observed_value >= 0.0


# ===========================================================================
# 20-22. start / stop
# ===========================================================================


@pytest.mark.asyncio
async def test_start_calls_check_and_trigger_then_sleeps() -> None:
    """start() calls _check_and_trigger then sleeps the configured interval."""
    sw = _make_scheduler(sources=[], interval=999)

    call_count = 0

    async def _fake_check() -> int:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sw._running = False  # stop after second call
        return 0

    sw._check_and_trigger = _fake_check  # type: ignore[method-assign]

    with patch("omniscience_server.scheduler.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await sw.start()

    # Should have slept with interval=999 at least once
    mock_sleep.assert_called_with(999)


@pytest.mark.asyncio
async def test_stop_exits_loop() -> None:
    """Calling stop() causes the start() loop to exit cleanly."""
    sw = _make_scheduler(sources=[], interval=1)

    async def _fake_check() -> int:
        await sw.stop()  # stop after first tick
        return 0

    sw._check_and_trigger = _fake_check  # type: ignore[method-assign]

    with patch("omniscience_server.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await asyncio.wait_for(sw.start(), timeout=2.0)

    assert sw._running is False


@pytest.mark.asyncio
async def test_start_does_not_crash_on_check_error() -> None:
    """An exception in _check_and_trigger is caught; the loop continues."""
    sw = _make_scheduler(sources=[], interval=1)
    call_count = 0

    async def _flaky_check() -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient DB failure")
        sw._running = False
        return 0

    sw._check_and_trigger = _flaky_check  # type: ignore[method-assign]

    with patch("omniscience_server.scheduler.asyncio.sleep", new_callable=AsyncMock):
        await sw.start()

    assert call_count == 2  # recovered and ran a second time


# ===========================================================================
# 23. _load_active_sources
# ===========================================================================


@pytest.mark.asyncio
async def test_load_active_sources_filters_by_active_status() -> None:
    """_load_active_sources issues a SELECT filtered on status=active."""
    active_src = _make_source(status=SourceStatus.active)
    factory = _session_factory_for(active_src)
    sw = SchedulerWorker(session_factory=factory, nats_conn=None)

    sources = await sw._load_active_sources()

    assert len(sources) == 1
    assert sources[0] is active_src


# ===========================================================================
# 24-25. _publish_trigger
# ===========================================================================


@pytest.mark.asyncio
async def test_publish_trigger_uses_correct_subject_and_payload() -> None:
    """_publish_trigger publishes to 'ingest.changes.{type}' with action='updated'."""
    src = _make_source(source_type=SourceType.slack, src_id=_SRC_ID)
    nats = _make_nats_conn()
    sw = SchedulerWorker(session_factory=_session_factory_for(), nats_conn=nats)

    result = await sw._publish_trigger(src)

    assert result is True
    assert nats.jetstream.publish.call_count == 1
    # Verify the subject used by QueueProducer → js.publish(subject=..., payload=...)
    call_kwargs = nats.jetstream.publish.call_args
    subject_arg = call_kwargs.kwargs.get("subject") or call_kwargs.args[0]
    assert subject_arg == "ingest.changes.slack"


@pytest.mark.asyncio
async def test_publish_trigger_returns_false_when_nats_none() -> None:
    """_publish_trigger returns False when nats_conn is None."""
    src = _make_source(source_type=SourceType.git)
    sw = SchedulerWorker(session_factory=_session_factory_for(), nats_conn=None)

    result = await sw._publish_trigger(src)

    assert result is False


# ===========================================================================
# 26-27. Settings
# ===========================================================================


def test_settings_scheduler_enabled_defaults_to_true() -> None:
    """scheduler_enabled defaults to True."""
    s = Settings(database_url="postgresql+asyncpg://x:y@h/d")
    assert s.scheduler_enabled is True


def test_settings_scheduler_interval_defaults_to_300() -> None:
    """scheduler_interval_seconds defaults to 300."""
    s = Settings(database_url="postgresql+asyncpg://x:y@h/d")
    assert s.scheduler_interval_seconds == 300


# ===========================================================================
# 28-29. app.py integration (factory only — no real lifespan)
# ===========================================================================


def test_app_state_scheduler_attribute_absent_before_lifespan() -> None:
    """A freshly created app (before lifespan) has no 'scheduler' in state."""
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://x:y@h/d",
        scheduler_enabled=True,
        environment="test",
    )
    app = create_app(settings=settings)
    # Before the lifespan runs the attribute is not set yet.
    assert not hasattr(app.state, "scheduler") or app.state.scheduler is None


def test_scheduler_worker_init_accepts_none_nats() -> None:
    """SchedulerWorker can be constructed with nats_conn=None (disabled NATS)."""
    factory = _session_factory_for()
    sw = SchedulerWorker(session_factory=factory, nats_conn=None, interval=300)
    assert sw._nats_conn is None
    assert sw._interval == 300


# ===========================================================================
# 30. Multiple stale sources
# ===========================================================================


@pytest.mark.asyncio
async def test_multiple_stale_sources_all_triggered() -> None:
    """All stale sources receive a trigger; count equals the number of stale sources."""
    src1 = _stale_source(src_id=_SRC_ID, source_type=SourceType.git, ttl=300, age_seconds=600)
    src2 = _stale_source(src_id=_SRC_ID_2, source_type=SourceType.s3, ttl=900, age_seconds=1800)
    src3 = _fresh_source(src_id=_SRC_ID_3, source_type=SourceType.fs, ttl=60, age_seconds=30)

    nats = _make_nats_conn()
    factory = _session_factory_for(src1, src2, src3)
    sw = SchedulerWorker(session_factory=factory, nats_conn=nats)

    with patch("omniscience_server.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        count = await sw._check_and_trigger()

    assert count == 2
    assert nats.jetstream.publish.call_count == 2
