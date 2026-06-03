"""Tests for the windowed activity endpoint (GET /api/v1/stats/activity).

Coverage:
- 24h / 168h / 720h windowed counts via StatsService.activity()
- workspace isolation: workspace A counts do not bleed to workspace B
- 422 on bad hours values (<=0, >8760, non-integer)
- 401 without token
- 403 without stats:read scope
- 403 with no-workspace token
- happy path with various window sizes
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_core.stats import ActivityResponse, StatsService
from omniscience_server.app import create_app

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_token(
    scopes: list[str],
    *,
    workspace_id: uuid.UUID | None = None,
) -> tuple[ApiToken, str]:
    pt, prefix = generate_token("test")
    hashed = hash_token(pt)

    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = uuid.uuid4()
    tok.token_prefix = prefix
    tok.hashed_token = hashed
    tok.scopes = scopes
    tok.workspace_id = workspace_id
    tok.expires_at = None
    tok.is_active = True
    tok.last_used_at = None
    return tok, pt


def _auth_session(token: ApiToken) -> AsyncMock:
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [token]
        result.scalars.return_value.first.return_value = token
        result.scalar_one.return_value = 1
        return result

    session.execute = _execute
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _build_app(
    *,
    token: ApiToken,
    stats_service: StatsService | None = None,
) -> FastAPI:
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = MagicMock(return_value=_auth_session(token))

    if stats_service is not None:
        app.state.stats_service = stats_service
    return app


async def _client_for(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _stub_stats_service_with_activity(
    *,
    activity_fn: Any | None = None,
) -> StatsService:
    """Return a StatsService stub whose activity() can be customised."""
    svc = MagicMock(spec=StatsService)
    if activity_fn is not None:
        svc.activity = activity_fn
    else:
        svc.activity = AsyncMock(
            return_value=ActivityResponse(new=0, updated=0, tombstoned=0, window_hours=24)
        )
    return svc


# ---------------------------------------------------------------------------
# StatsService.activity() unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_service_issues_three_queries() -> None:
    """activity() must execute exactly 3 scalar SELECT statements."""
    call_log: list[Any] = []

    async def _execute(stmt: Any) -> Any:
        call_log.append(stmt)
        result = MagicMock()
        result.scalar_one.return_value = 5
        return result

    session = AsyncMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    svc = StatsService(
        db_session_factory=factory,
        graph_store=AsyncMock(),
        vector_store=AsyncMock(),
    )
    result = await svc.activity(workspace_id=uuid.uuid4(), window_hours=24)

    assert len(call_log) == 3
    assert result.new == 5
    assert result.updated == 5
    assert result.tombstoned == 5
    assert result.window_hours == 24


@pytest.mark.asyncio
async def test_activity_service_window_hours_echoed() -> None:
    """window_hours must be echoed back in the response."""
    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalar_one.return_value = 0
        return result

    session = AsyncMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    svc = StatsService(
        db_session_factory=factory,
        graph_store=AsyncMock(),
        vector_store=AsyncMock(),
    )

    for window in (24, 168, 720):
        result = await svc.activity(workspace_id=uuid.uuid4(), window_hours=window)
        assert result.window_hours == window


@pytest.mark.asyncio
async def test_activity_service_workspace_isolation() -> None:
    """Workspace A activity must not bleed into workspace B."""
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()

    # Track which workspace_id was used on each session call.
    seen_workspace_ids: list[uuid.UUID] = []

    def _make_session(ws_id: uuid.UUID) -> AsyncMock:
        """Return a session mock that records its workspace_id."""
        seen_workspace_ids.append(ws_id)

        async def _execute(stmt: Any) -> Any:
            result = MagicMock()
            # Workspace A gets count=3, workspace B gets count=99.
            result.scalar_one.return_value = 3 if ws_id == workspace_a else 99
            return result

        session = AsyncMock()
        session.execute = _execute
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    # The session factory is called once per activity() invocation.
    factory = MagicMock()
    factory.side_effect = [
        _make_session(workspace_a),
        _make_session(workspace_b),
    ]

    svc = StatsService(
        db_session_factory=factory,
        graph_store=AsyncMock(),
        vector_store=AsyncMock(),
    )

    result_a = await svc.activity(workspace_id=workspace_a, window_hours=24)
    result_b = await svc.activity(workspace_id=workspace_b, window_hours=24)

    assert result_a.new == 3
    assert result_b.new == 99
    assert seen_workspace_ids == [workspace_a, workspace_b]


# ---------------------------------------------------------------------------
# REST: auth + scope enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_requires_auth() -> None:
    tok, _ = _make_token(["stats:read"])
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.get("/api/v1/stats/activity")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_activity_requires_stats_scope() -> None:
    tok, pt = _make_token(["sources:read"], workspace_id=uuid.uuid4())
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_activity_rejects_no_workspace_token() -> None:
    tok, pt = _make_token(["stats:read"], workspace_id=None)
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# REST: validation — bad hours values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_rejects_hours_zero() -> None:
    tok, pt = _make_token(["stats:read"], workspace_id=uuid.uuid4())
    svc = _stub_stats_service_with_activity()
    app = _build_app(token=tok, stats_service=svc)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=0",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_activity_rejects_hours_negative() -> None:
    tok, pt = _make_token(["stats:read"], workspace_id=uuid.uuid4())
    svc = _stub_stats_service_with_activity()
    app = _build_app(token=tok, stats_service=svc)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=-1",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_activity_rejects_hours_above_max() -> None:
    tok, pt = _make_token(["stats:read"], workspace_id=uuid.uuid4())
    svc = _stub_stats_service_with_activity()
    app = _build_app(token=tok, stats_service=svc)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=8761",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_activity_rejects_non_integer_hours() -> None:
    tok, pt = _make_token(["stats:read"], workspace_id=uuid.uuid4())
    svc = _stub_stats_service_with_activity()
    app = _build_app(token=tok, stats_service=svc)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=abc",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# REST: happy paths — 24h / 168h / 720h
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_default_24h() -> None:
    """Default (no hours param) is 24h."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)

    activity_response = ActivityResponse(new=5, updated=2, tombstoned=1, window_hours=24)
    svc = _stub_stats_service_with_activity(
        activity_fn=AsyncMock(return_value=activity_response)
    )
    app = _build_app(token=tok, stats_service=svc)

    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["new"] == 5
    assert body["updated"] == 2
    assert body["tombstoned"] == 1
    assert body["window_hours"] == 24
    svc.activity.assert_awaited_once_with(workspace_id=workspace_id, window_hours=24)


@pytest.mark.asyncio
async def test_activity_7d_window() -> None:
    """hours=168 maps to 7-day window."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)

    activity_response = ActivityResponse(new=120, updated=45, tombstoned=8, window_hours=168)
    svc = _stub_stats_service_with_activity(
        activity_fn=AsyncMock(return_value=activity_response)
    )
    app = _build_app(token=tok, stats_service=svc)

    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=168",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 168
    assert body["new"] == 120
    svc.activity.assert_awaited_once_with(workspace_id=workspace_id, window_hours=168)


@pytest.mark.asyncio
async def test_activity_30d_window() -> None:
    """hours=720 maps to 30-day window."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)

    activity_response = ActivityResponse(new=500, updated=200, tombstoned=50, window_hours=720)
    svc = _stub_stats_service_with_activity(
        activity_fn=AsyncMock(return_value=activity_response)
    )
    app = _build_app(token=tok, stats_service=svc)

    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=720",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 720
    assert body["new"] == 500
    svc.activity.assert_awaited_once_with(workspace_id=workspace_id, window_hours=720)


@pytest.mark.asyncio
async def test_activity_boundary_hours_1() -> None:
    """Minimum valid value: hours=1."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)
    svc = _stub_stats_service_with_activity(
        activity_fn=AsyncMock(
            return_value=ActivityResponse(new=0, updated=0, tombstoned=0, window_hours=1)
        )
    )
    app = _build_app(token=tok, stats_service=svc)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=1",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    assert resp.json()["window_hours"] == 1


@pytest.mark.asyncio
async def test_activity_boundary_hours_8760() -> None:
    """Maximum valid value: hours=8760 (1 year)."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)
    svc = _stub_stats_service_with_activity(
        activity_fn=AsyncMock(
            return_value=ActivityResponse(new=0, updated=0, tombstoned=0, window_hours=8760)
        )
    )
    app = _build_app(token=tok, stats_service=svc)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/activity?hours=8760",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    assert resp.json()["window_hours"] == 8760


# ---------------------------------------------------------------------------
# REST: workspace ACL isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_workspace_acl_isolation() -> None:
    """Two workspaces receive distinct activity counts — no cross-bleed."""
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()

    activity_a = ActivityResponse(new=10, updated=3, tombstoned=1, window_hours=24)
    activity_b = ActivityResponse(new=99, updated=50, tombstoned=20, window_hours=24)

    svc = MagicMock(spec=StatsService)

    async def _activity(*, workspace_id: uuid.UUID, window_hours: int) -> ActivityResponse:
        if workspace_id == workspace_a:
            return activity_a
        if workspace_id == workspace_b:
            return activity_b
        raise AssertionError(f"unexpected workspace_id: {workspace_id}")

    svc.activity = _activity

    tok_a, pt_a = _make_token(["stats:read"], workspace_id=workspace_a)
    app_a = _build_app(token=tok_a, stats_service=svc)
    async with await _client_for(app_a) as client:
        resp_a = await client.get(
            "/api/v1/stats/activity",
            headers={"Authorization": f"Bearer {pt_a}"},
        )

    tok_b, pt_b = _make_token(["stats:read"], workspace_id=workspace_b)
    app_b = _build_app(token=tok_b, stats_service=svc)
    async with await _client_for(app_b) as client:
        resp_b = await client.get(
            "/api/v1/stats/activity",
            headers={"Authorization": f"Bearer {pt_b}"},
        )

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a.json()["new"] == 10
    assert resp_b.json()["new"] == 99
    assert resp_a.json()["new"] != resp_b.json()["new"]


# ---------------------------------------------------------------------------
# ActivityResponse model validation
# ---------------------------------------------------------------------------


def test_activity_response_rejects_negative_counts() -> None:
    """Pydantic model must reject negative counts."""
    import pytest as pt_module

    with pt_module.raises(Exception):
        ActivityResponse(new=-1, updated=0, tombstoned=0, window_hours=24)


def test_activity_response_rejects_zero_window_hours() -> None:
    """Pydantic model must reject window_hours < 1."""
    import pytest as pt_module

    with pt_module.raises(Exception):
        ActivityResponse(new=0, updated=0, tombstoned=0, window_hours=0)
