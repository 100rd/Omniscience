"""Tests for freshness SLO enforcement.

Covers:
1.  FreshnessReport fields for fresh source (within SLA)
2.  FreshnessReport fields for stale source (past SLA)
3.  FreshnessReport when SLA is None (never stale)
4.  FreshnessReport when never synced + SLA set (immediately stale)
5.  FreshnessReport when never synced + no SLA (not stale)
6.  staleness_margin_seconds is negative when within SLA
7.  staleness_margin_seconds is positive when past SLA
8.  staleness_margin_seconds is NaN when no SLA
9.  FreshnessChecker.check_all returns one report per source
10. FreshnessChecker.check_source returns correct report
11. FreshnessChecker.check_source raises KeyError for unknown source
12. FreshnessWorker updates FRESHNESS_STALE_TOTAL gauge
13. FreshnessWorker updates FRESHNESS_AGE_SECONDS gauge per source
14. FreshnessWorker logs WARNING for stale source
15. FreshnessWorker does NOT log WARNING for fresh source
16. REST GET /api/v1/freshness returns 200 with all sources
17. REST GET /api/v1/freshness returns stale_count correctly
18. REST GET /api/v1/freshness/{source_id} returns 200 for known source
19. REST GET /api/v1/freshness/{source_id} returns 404 for unknown source
20. REST GET /api/v1/freshness requires sources:read scope (401 without token)
21. REST GET /api/v1/freshness requires sources:read scope (403 wrong scope)
22. MCP list_sources includes is_stale field
23. MCP list_sources includes age_seconds field
24. MCP source_stats includes is_stale field
25. MCP source_stats includes staleness_margin_seconds field
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from omniscience_core.db.models import Source, SourceStatus, SourceType
from omniscience_core.freshness import FreshnessChecker, FreshnessReport, _compute_report

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

# Fixed reference time used ONLY for pure _compute_report unit tests.
_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
_SRC_ID = uuid.uuid4()
_SRC_ID_2 = uuid.uuid4()

_SLA = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Source factory helpers
# ---------------------------------------------------------------------------


def _make_source(
    src_id: uuid.UUID = _SRC_ID,
    name: str = "test-source",
    freshness_sla_seconds: int | None = _SLA,
    last_sync_at: datetime | None = None,
) -> MagicMock:
    s = MagicMock(spec=Source)
    s.id = src_id
    s.name = name
    s.type = SourceType.git
    s.status = SourceStatus.active
    s.freshness_sla_seconds = freshness_sla_seconds
    s.last_sync_at = last_sync_at
    s.last_error = None
    s.last_error_at = None
    return s


def _fresh_source_at(ref: datetime, age_seconds: float = 60.0) -> MagicMock:
    """Source synced *age_seconds* ago relative to *ref*."""
    last_sync = ref - timedelta(seconds=age_seconds)
    return _make_source(last_sync_at=last_sync)


def _stale_source_at(ref: datetime, age_seconds: float = 600.0) -> MagicMock:
    """Source synced *age_seconds* ago relative to *ref* — past the default SLA."""
    last_sync = ref - timedelta(seconds=age_seconds)
    return _make_source(last_sync_at=last_sync)


# Convenience helpers using the fixed _NOW reference (for _compute_report tests only).
def _fresh_source(age_seconds: float = 60.0) -> MagicMock:
    return _fresh_source_at(_NOW, age_seconds)


def _stale_source(age_seconds: float = 600.0) -> MagicMock:
    return _stale_source_at(_NOW, age_seconds)


# ---------------------------------------------------------------------------
# Session factory mock helpers
# ---------------------------------------------------------------------------


def _session_factory_for(*sources: Any) -> AsyncMock:
    """Return an async session factory mock that yields the given sources."""
    session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(sources)
        return result

    session.execute = _execute

    async def _get(_model: Any, pk: Any) -> Any:
        for s in sources:
            if s.id == pk:
                return s
        return None

    session.get = _get

    # Support async context manager usage.
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# ===========================================================================
# 1-8: _compute_report unit tests (no I/O — use fixed _NOW reference)
# ===========================================================================


def test_fresh_source_is_not_stale() -> None:
    """Report for a source that was recently synced is not stale."""
    src = _fresh_source(age_seconds=60.0)
    report = _compute_report(src, _NOW)

    assert report.is_stale is False
    assert report.age_seconds == pytest.approx(60.0, abs=1.0)


def test_stale_source_is_stale() -> None:
    """Report for a source past its SLA is marked stale."""
    src = _stale_source(age_seconds=600.0)
    report = _compute_report(src, _NOW)

    assert report.is_stale is True
    assert report.age_seconds > _SLA


def test_no_sla_never_stale() -> None:
    """A source with no SLO configured is never considered stale."""
    src = _make_source(freshness_sla_seconds=None, last_sync_at=_NOW - timedelta(days=30))
    report = _compute_report(src, _NOW)

    assert report.is_stale is False
    assert report.freshness_sla_seconds is None


def test_never_synced_with_sla_is_stale() -> None:
    """A source that has never been synced but has an SLO is immediately stale."""
    src = _make_source(freshness_sla_seconds=_SLA, last_sync_at=None)
    report = _compute_report(src, _NOW)

    assert report.is_stale is True
    assert math.isinf(report.age_seconds)
    assert math.isinf(report.staleness_margin_seconds)


def test_never_synced_no_sla_not_stale() -> None:
    """A source that has never been synced with no SLO is not stale."""
    src = _make_source(freshness_sla_seconds=None, last_sync_at=None)
    report = _compute_report(src, _NOW)

    assert report.is_stale is False
    assert math.isinf(report.age_seconds)


def test_staleness_margin_negative_when_within_sla() -> None:
    """staleness_margin_seconds is negative when the source is within its SLA."""
    src = _fresh_source(age_seconds=60.0)
    report = _compute_report(src, _NOW)

    assert report.staleness_margin_seconds < 0
    assert report.staleness_margin_seconds == pytest.approx(60.0 - _SLA, abs=1.0)


def test_staleness_margin_positive_when_past_sla() -> None:
    """staleness_margin_seconds is positive when the source is past its SLA."""
    src = _stale_source(age_seconds=600.0)
    report = _compute_report(src, _NOW)

    assert report.staleness_margin_seconds > 0
    assert report.staleness_margin_seconds == pytest.approx(600.0 - _SLA, abs=1.0)


def test_staleness_margin_nan_when_no_sla() -> None:
    """staleness_margin_seconds is NaN (no meaning) when there is no SLO."""
    src = _make_source(freshness_sla_seconds=None, last_sync_at=_NOW - timedelta(seconds=60))
    report = _compute_report(src, _NOW)

    assert math.isnan(report.staleness_margin_seconds)


# ===========================================================================
# 9-11: FreshnessChecker — patch datetime.now inside the module
# ===========================================================================


@pytest.mark.asyncio
async def test_check_all_returns_one_report_per_source() -> None:
    """check_all produces one FreshnessReport for each source row."""
    ref = _NOW
    src1 = _fresh_source_at(ref, age_seconds=30)
    src1.id = _SRC_ID
    src2 = _stale_source_at(ref, age_seconds=999)
    src2.id = _SRC_ID_2

    factory = _session_factory_for(src1, src2)
    checker = FreshnessChecker(factory)

    with patch("omniscience_core.freshness.datetime") as mock_dt:
        mock_dt.now.return_value = ref
        reports = await checker.check_all()

    assert len(reports) == 2
    assert all(isinstance(r, FreshnessReport) for r in reports)


@pytest.mark.asyncio
async def test_check_source_returns_correct_report() -> None:
    """check_source returns the report for the requested source id."""
    ref = _NOW
    src = _fresh_source_at(ref, age_seconds=120)
    src.id = _SRC_ID

    factory = _session_factory_for(src)
    checker = FreshnessChecker(factory)

    with patch("omniscience_core.freshness.datetime") as mock_dt:
        mock_dt.now.return_value = ref
        report = await checker.check_source(_SRC_ID)

    assert report.source_id == _SRC_ID
    assert report.is_stale is False


@pytest.mark.asyncio
async def test_check_source_raises_key_error_for_unknown() -> None:
    """check_source raises KeyError when the source does not exist."""
    factory = _session_factory_for()  # empty DB
    checker = FreshnessChecker(factory)

    with pytest.raises(KeyError, match=str(_SRC_ID)):
        await checker.check_source(_SRC_ID)


# ===========================================================================
# 12-15: FreshnessWorker — patch datetime.now and Prometheus gauges
# ===========================================================================


@pytest.mark.asyncio
async def test_freshness_worker_updates_stale_total_gauge() -> None:
    """FreshnessWorker sets FRESHNESS_STALE_TOTAL to the stale source count."""
    from omniscience_server.freshness_worker import FreshnessWorker

    ref = _NOW
    stale_src = _stale_source_at(ref, age_seconds=900)
    fresh_src = _fresh_source_at(ref, age_seconds=30)
    stale_src.id = uuid.uuid4()
    fresh_src.id = uuid.uuid4()
    stale_src.name = "stale-src"
    fresh_src.name = "fresh-src"

    factory = _session_factory_for(stale_src, fresh_src)
    worker = FreshnessWorker(factory, interval_seconds=9999)

    with (
        patch("omniscience_core.freshness.datetime") as mock_dt,
        patch("omniscience_server.freshness_worker.FRESHNESS_STALE_TOTAL") as mock_stale_gauge,
        patch("omniscience_server.freshness_worker.FRESHNESS_AGE_SECONDS"),
    ):
        mock_dt.now.return_value = ref
        await worker._tick()

    mock_stale_gauge.set.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_freshness_worker_updates_age_gauge_per_source() -> None:
    """FreshnessWorker sets FRESHNESS_AGE_SECONDS for each source."""
    from omniscience_server.freshness_worker import FreshnessWorker

    ref = _NOW
    src = _fresh_source_at(ref, age_seconds=45)
    src.id = uuid.uuid4()
    src.name = "my-source"

    factory = _session_factory_for(src)
    worker = FreshnessWorker(factory, interval_seconds=9999)

    age_label_mock = MagicMock()
    age_gauge_mock = MagicMock()
    age_gauge_mock.labels.return_value = age_label_mock

    with (
        patch("omniscience_core.freshness.datetime") as mock_dt,
        patch("omniscience_server.freshness_worker.FRESHNESS_AGE_SECONDS", age_gauge_mock),
        patch("omniscience_server.freshness_worker.FRESHNESS_STALE_TOTAL"),
    ):
        mock_dt.now.return_value = ref
        await worker._tick()

    age_gauge_mock.labels.assert_called_once_with(
        source_id=str(src.id),
        source_name="my-source",
    )
    age_label_mock.set.assert_called_once()
    age_value = age_label_mock.set.call_args[0][0]
    assert age_value == pytest.approx(45.0, abs=5.0)


@pytest.mark.asyncio
async def test_freshness_worker_logs_warning_for_stale_source() -> None:
    """FreshnessWorker emits a WARNING log when a source is stale."""
    import structlog.testing
    from omniscience_server.freshness_worker import FreshnessWorker

    ref = _NOW
    stale_src = _stale_source_at(ref, age_seconds=900)
    stale_src.id = uuid.uuid4()
    stale_src.name = "stale-source"

    factory = _session_factory_for(stale_src)
    worker = FreshnessWorker(factory, interval_seconds=9999)

    with (
        patch("omniscience_core.freshness.datetime") as mock_dt,
        patch("omniscience_server.freshness_worker.FRESHNESS_STALE_TOTAL"),
        patch("omniscience_server.freshness_worker.FRESHNESS_AGE_SECONDS"),
        structlog.testing.capture_logs() as captured,
    ):
        mock_dt.now.return_value = ref
        await worker._tick()

    warning_events = [e for e in captured if e.get("log_level") == "warning"]
    stale_events = [e for e in warning_events if e.get("event") == "source_stale"]
    assert len(stale_events) == 1
    assert stale_events[0]["source_name"] == "stale-source"


@pytest.mark.asyncio
async def test_freshness_worker_no_warning_for_fresh_source() -> None:
    """FreshnessWorker does NOT emit a WARNING log for fresh sources."""
    import structlog.testing
    from omniscience_server.freshness_worker import FreshnessWorker

    ref = _NOW
    fresh_src = _fresh_source_at(ref, age_seconds=30)
    fresh_src.id = uuid.uuid4()
    fresh_src.name = "fresh-source"

    factory = _session_factory_for(fresh_src)
    worker = FreshnessWorker(factory, interval_seconds=9999)

    with (
        patch("omniscience_core.freshness.datetime") as mock_dt,
        patch("omniscience_server.freshness_worker.FRESHNESS_STALE_TOTAL"),
        patch("omniscience_server.freshness_worker.FRESHNESS_AGE_SECONDS"),
        structlog.testing.capture_logs() as captured,
    ):
        mock_dt.now.return_value = ref
        await worker._tick()

    warning_events = [e for e in captured if e.get("log_level") == "warning"]
    assert len(warning_events) == 0


# ===========================================================================
# 16-21: REST API endpoints
# ===========================================================================

# Reuse the token / session helpers from test_rest_api.py style.


def _make_api_token(scopes: list[str]) -> tuple[Any, str]:
    from omniscience_core.auth.tokens import generate_token, hash_token

    pt, prefix = generate_token("test")
    hashed = hash_token(pt)

    tok = MagicMock()
    tok.id = uuid.uuid4()
    tok.token_prefix = prefix
    tok.hashed_token = hashed
    tok.scopes = scopes
    tok.expires_at = None
    tok.is_active = True
    tok.last_used_at = None
    return tok, pt


def _make_rest_session(sources: list[Any]) -> AsyncMock:
    session = AsyncMock()

    async def _get(_model: Any, pk: Any) -> Any:
        for s in sources:
            if s.id == pk:
                return s
        return None

    session.get = _get

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(sources)
        return result

    session.execute = _execute

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.asyncio
async def test_rest_freshness_all_returns_200() -> None:
    """GET /api/v1/freshness returns 200 with sources list."""
    import httpx
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    tok, pt = _make_api_token(["sources:read"])
    ref = datetime.now(tz=UTC)
    src = _fresh_source_at(ref, age_seconds=60)
    src.id = uuid.uuid4()
    src.name = "demo"
    factory = _make_rest_session([src])
    app.state.db_session_factory = factory

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=tok,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/freshness",
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert "sources" in data
    assert "total" in data
    assert "stale_count" in data


@pytest.mark.asyncio
async def test_rest_freshness_all_stale_count() -> None:
    """GET /api/v1/freshness reports accurate stale_count."""
    import httpx
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    tok, pt = _make_api_token(["sources:read"])
    ref = datetime.now(tz=UTC)
    stale = _stale_source_at(ref, age_seconds=9999)
    stale.id = uuid.uuid4()
    stale.name = "stale"
    fresh = _fresh_source_at(ref, age_seconds=10)
    fresh.id = uuid.uuid4()
    fresh.name = "fresh"
    factory = _make_rest_session([stale, fresh])
    app.state.db_session_factory = factory

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=tok,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/freshness",
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["stale_count"] == 1
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_rest_freshness_single_returns_200() -> None:
    """GET /api/v1/freshness/{source_id} returns 200 for existing source."""
    import httpx
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    tok, pt = _make_api_token(["sources:read"])
    ref = datetime.now(tz=UTC)
    src_id = uuid.uuid4()
    src = _fresh_source_at(ref, age_seconds=45)
    src.id = src_id
    src.name = "target"
    factory = _make_rest_session([src])
    app.state.db_session_factory = factory

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=tok,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/freshness/{src_id}",
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["source_id"] == str(src_id)
    assert "is_stale" in data
    assert "age_seconds" in data


@pytest.mark.asyncio
async def test_rest_freshness_single_404_for_unknown() -> None:
    """GET /api/v1/freshness/{source_id} returns 404 for unknown source."""
    import httpx
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    tok, pt = _make_api_token(["sources:read"])
    factory = _make_rest_session([])  # empty DB
    app.state.db_session_factory = factory

    unknown_id = uuid.uuid4()
    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=tok,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/freshness/{unknown_id}",
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rest_freshness_requires_auth() -> None:
    """GET /api/v1/freshness returns 401 when no token is provided."""
    import httpx
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _make_rest_session([])

    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=None,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/freshness")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rest_freshness_requires_sources_read_scope() -> None:
    """GET /api/v1/freshness returns 403 when token lacks sources:read scope."""
    import httpx
    from omniscience_core.config import Settings
    from omniscience_server.app import create_app

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _make_rest_session([])

    tok, pt = _make_api_token(["search"])  # wrong scope
    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=tok,
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/freshness",
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 403


# ===========================================================================
# 22-25: MCP tool freshness fields
# ===========================================================================


@pytest.mark.asyncio
async def test_mcp_list_sources_includes_is_stale() -> None:
    """mcp_list_sources response includes is_stale per source."""
    from omniscience_server.mcp.tools import mcp_list_sources

    ref = datetime.now(tz=UTC)
    stale_src = _stale_source_at(ref, age_seconds=9999)
    stale_src.id = uuid.uuid4()
    stale_src.name = "stale"

    factory = _make_rest_session([stale_src])
    app = MagicMock()
    app.state.db_session_factory = factory

    result = await mcp_list_sources(app=app)

    assert "sources" in result
    assert len(result["sources"]) == 1
    src_dict = result["sources"][0]
    assert "is_stale" in src_dict
    assert src_dict["is_stale"] is True


@pytest.mark.asyncio
async def test_mcp_list_sources_includes_age_seconds() -> None:
    """mcp_list_sources response includes age_seconds per source."""
    from omniscience_server.mcp.tools import mcp_list_sources

    ref = datetime.now(tz=UTC)
    fresh_src = _fresh_source_at(ref, age_seconds=30)
    fresh_src.id = uuid.uuid4()
    fresh_src.name = "fresh"

    factory = _make_rest_session([fresh_src])
    app = MagicMock()
    app.state.db_session_factory = factory

    result = await mcp_list_sources(app=app)

    src_dict = result["sources"][0]
    assert "age_seconds" in src_dict
    assert src_dict["age_seconds"] == pytest.approx(30.0, abs=5.0)


@pytest.mark.asyncio
async def test_mcp_source_stats_includes_is_stale() -> None:
    """mcp_source_stats response includes is_stale."""
    from omniscience_server.mcp.tools import mcp_source_stats

    ref = datetime.now(tz=UTC)
    stale_src = _stale_source_at(ref, age_seconds=999)
    src_id = uuid.uuid4()
    stale_src.id = src_id
    stale_src.name = "stale"

    factory = _make_rest_session([stale_src])
    app = MagicMock()
    app.state.db_session_factory = factory

    result = await mcp_source_stats(app=app, source_id=str(src_id))

    assert "is_stale" in result
    assert result["is_stale"] is True


@pytest.mark.asyncio
async def test_mcp_source_stats_includes_staleness_margin() -> None:
    """mcp_source_stats response includes staleness_margin_seconds."""
    from omniscience_server.mcp.tools import mcp_source_stats

    ref = datetime.now(tz=UTC)
    stale_src = _stale_source_at(ref, age_seconds=900)
    src_id = uuid.uuid4()
    stale_src.id = src_id
    stale_src.name = "overdue"

    factory = _make_rest_session([stale_src])
    app = MagicMock()
    app.state.db_session_factory = factory

    result = await mcp_source_stats(app=app, source_id=str(src_id))

    assert "staleness_margin_seconds" in result
    # 900s age - 300s SLA = 600s margin
    assert result["staleness_margin_seconds"] == pytest.approx(600.0, abs=10.0)
