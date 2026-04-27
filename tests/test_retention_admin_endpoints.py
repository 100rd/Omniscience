"""REST tests for the retention admin endpoints (issue #136, ADR-0009 §8).

Coverage:

* ``GET /api/v1/admin/retention/status``
    - 401 without token
    - 403 without ``stats:read`` scope
    - 403 with token that has scope but no workspace
    - 200 happy path returns workspace-scoped counts + lag + last_run_at
    - cross-workspace ACL: workspace A's status excludes workspace B's
      data
* ``POST /api/v1/admin/retention/run-now``
    - 401 without token
    - 403 without ``stats:read`` scope
    - 403 with token but no workspace
    - 202 Accepted on happy path; ``RetentionWorker.run_once`` invoked
      with ``workspace_filter`` = the caller's workspace
    - cross-workspace ACL: workspace A's run-now never touches B's data

The tests reuse the same ``_make_token`` / ``_auth_session`` helpers
from ``test_stats.py`` to keep the auth wiring identical to the stats
endpoints. The retention worker is mocked at ``app.state.retention_worker``
so we don't need a live Neo4j+Qdrant pair to exercise the REST surface.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.config import Settings
from omniscience_core.db.models import ApiToken
from omniscience_server.app import create_app
from omniscience_server.retention_worker import (
    RetentionWorker,
    RunReport,
    WorkspaceRetentionReport,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirror test_stats.py)
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


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
        retention_dry_run=False,
    )


def _make_graph_store(
    *,
    records_by_workspace: dict[uuid.UUID, dict[str, int]] | None = None,
) -> AsyncMock:
    """Mock Neo4jGraphStore — only the retention surface used by /status."""
    store = AsyncMock()

    async def _count_records_by_tier(*, workspace_id: uuid.UUID) -> dict[str, int]:
        if records_by_workspace is None:
            return {"hot": 0, "warm": 0}
        return records_by_workspace.get(workspace_id, {"hot": 0, "warm": 0})

    store.count_records_by_tier = AsyncMock(side_effect=_count_records_by_tier)
    return store


def _make_vector_store(
    *,
    chunks_by_workspace: dict[uuid.UUID, dict[str, int]] | None = None,
) -> AsyncMock:
    store = AsyncMock()

    async def _count_chunks_by_tier(*, workspace_id: uuid.UUID) -> dict[str, int]:
        if chunks_by_workspace is None:
            return {"hot": 0, "warm": 0}
        return chunks_by_workspace.get(workspace_id, {"hot": 0, "warm": 0})

    store.count_chunks_by_tier = AsyncMock(side_effect=_count_chunks_by_tier)
    return store


def _build_app(
    *,
    token: ApiToken,
    graph_store: AsyncMock | None = None,
    vector_store: AsyncMock | None = None,
    retention_worker: RetentionWorker | MagicMock | None = None,
) -> FastAPI:
    """Build an app with the retention admin surface wired."""
    settings = _settings()
    app = create_app(settings=settings)

    app.state.db_session_factory = MagicMock(return_value=_auth_session(token))
    if graph_store is not None:
        app.state.graph_store = graph_store
    else:
        app.state.graph_store = _make_graph_store()
    if vector_store is not None:
        app.state.vector_store = vector_store
    else:
        app.state.vector_store = _make_vector_store()
    app.state.retention_worker = retention_worker
    return app


async def _client_for(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# /admin/retention/status — auth + scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_requires_auth() -> None:
    tok, _ = _make_token(["stats:read"])
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.get("/api/v1/admin/retention/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_status_requires_stats_read_scope() -> None:
    """A token without stats:read must receive 403."""
    tok, pt = _make_token(["sources:read"], workspace_id=uuid.uuid4())
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/admin/retention/status",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_status_rejects_token_without_workspace() -> None:
    """A stats:read token with workspace_id=None fails closed with 403."""
    tok, pt = _make_token(["stats:read"], workspace_id=None)
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/admin/retention/status",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# /admin/retention/status — happy path + ACL contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_happy_path_returns_counts_and_dry_run_flag() -> None:
    """200 with workspace-scoped counts + dry_run flag."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)
    graph = _make_graph_store(
        records_by_workspace={workspace_id: {"hot": 100, "warm": 50}},
    )
    vector = _make_vector_store(
        chunks_by_workspace={workspace_id: {"hot": 80, "warm": 40}},
    )
    app = _build_app(token=tok, graph_store=graph, vector_store=vector)

    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/admin/retention/status",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == str(workspace_id)
    assert body["neo4j_hot"] == 100
    assert body["neo4j_warm"] == 50
    assert body["qdrant_hot"] == 80
    assert body["qdrant_warm"] == 40
    assert body["dry_run"] is False
    # No worker wired → last_run_at is None and lag is 0.
    assert body["last_run_at"] is None
    assert body["lag_seconds"] == 0.0


@pytest.mark.asyncio
async def test_status_admin_scope_inherits_stats_read() -> None:
    """An ``admin`` token satisfies the stats:read requirement."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["admin"], workspace_id=workspace_id)
    graph = _make_graph_store(
        records_by_workspace={workspace_id: {"hot": 1, "warm": 0}},
    )
    app = _build_app(token=tok, graph_store=graph)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/admin/retention/status",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_status_uses_worker_last_report_for_last_run_at_and_lag() -> None:
    """When the worker has a recent report, /status surfaces it."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)
    finished = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    started = finished - timedelta(seconds=2)
    last_report = RunReport(
        started_at=started,
        finished_at=finished,
        dry_run=False,
        per_workspace=[
            WorkspaceRetentionReport(
                workspace_id=workspace_id,
                eligible_hot_to_warm_entity_states=0,
                eligible_hot_to_warm_edges=0,
                eligible_hot_to_warm_chunks=0,
                eligible_warm_to_archive_entity_snapshots=0,
                eligible_warm_to_archive_dates=[],
                lag_seconds=42.0,
            )
        ],
        duration_seconds=2.0,
    )
    worker = MagicMock(spec=RetentionWorker)
    worker.last_report = last_report
    app = _build_app(token=tok, retention_worker=worker)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/admin/retention/status",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_run_at"] is not None
    assert body["lag_seconds"] == 42.0


@pytest.mark.asyncio
async def test_status_workspace_acl_isolation() -> None:
    """Workspace A's /status MUST NOT include workspace B counts.

    The store mocks return per-workspace data; we issue requests with
    two different tokens and assert each receives only its own data.
    Cross-workspace bleed would mean the underlying store call was
    made with the wrong workspace_id — the second line of defence
    after the protocol-level ``workspace_id`` invariant on
    ``GraphStore`` / ``VectorStore``.
    """
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()
    graph = _make_graph_store(
        records_by_workspace={
            workspace_a: {"hot": 11, "warm": 1},
            workspace_b: {"hot": 99, "warm": 9},
        },
    )
    vector = _make_vector_store(
        chunks_by_workspace={
            workspace_a: {"hot": 22, "warm": 2},
            workspace_b: {"hot": 88, "warm": 8},
        },
    )

    tok_a, pt_a = _make_token(["stats:read"], workspace_id=workspace_a)
    app_a = _build_app(token=tok_a, graph_store=graph, vector_store=vector)
    async with await _client_for(app_a) as client:
        resp_a = await client.get(
            "/api/v1/admin/retention/status",
            headers={"Authorization": f"Bearer {pt_a}"},
        )

    tok_b, pt_b = _make_token(["stats:read"], workspace_id=workspace_b)
    app_b = _build_app(token=tok_b, graph_store=graph, vector_store=vector)
    async with await _client_for(app_b) as client:
        resp_b = await client.get(
            "/api/v1/admin/retention/status",
            headers={"Authorization": f"Bearer {pt_b}"},
        )

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    body_a = resp_a.json()
    body_b = resp_b.json()
    assert body_a["neo4j_hot"] == 11 and body_a["qdrant_hot"] == 22
    assert body_b["neo4j_hot"] == 99 and body_b["qdrant_hot"] == 88
    # Cross-bleed check: the response payloads are distinct.
    assert body_a["neo4j_hot"] != body_b["neo4j_hot"]
    assert body_a["workspace_id"] == str(workspace_a)
    assert body_b["workspace_id"] == str(workspace_b)


# ---------------------------------------------------------------------------
# /admin/retention/run-now — auth + scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_now_requires_auth() -> None:
    tok, _ = _make_token(["stats:read"])
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.post("/api/v1/admin/retention/run-now")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_run_now_requires_stats_read_scope() -> None:
    tok, pt = _make_token(["sources:read"], workspace_id=uuid.uuid4())
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/admin/retention/run-now",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_run_now_rejects_token_without_workspace() -> None:
    tok, pt = _make_token(["stats:read"], workspace_id=None)
    app = _build_app(token=tok)
    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/admin/retention/run-now",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /admin/retention/run-now — happy path + workspace filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_now_invokes_worker_with_caller_workspace_filter() -> None:
    """202 with ``RetentionWorker.run_once(workspace_filter=caller)``."""
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)

    finished = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    started = finished - timedelta(seconds=1)
    run_report = RunReport(
        started_at=started,
        finished_at=finished,
        dry_run=False,
        per_workspace=[
            WorkspaceRetentionReport(
                workspace_id=workspace_id,
                eligible_hot_to_warm_entity_states=3,
                eligible_hot_to_warm_edges=0,
                eligible_hot_to_warm_chunks=0,
                eligible_warm_to_archive_entity_snapshots=0,
                eligible_warm_to_archive_dates=[],
                lag_seconds=10.0,
            )
        ],
        duration_seconds=1.0,
    )
    worker = MagicMock(spec=RetentionWorker)
    worker.dry_run = False
    worker.run_once = AsyncMock(return_value=run_report)
    app = _build_app(token=tok, retention_worker=worker)

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/admin/retention/run-now",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["workspace_id"] == str(workspace_id)
    assert body["duration_seconds"] == 1.0
    assert body["dry_run"] is False
    assert body["lag_seconds"] == 10.0
    # The worker MUST be called with workspace_filter = the caller's
    # workspace_id — never None, never another workspace.
    worker.run_once.assert_awaited_once()
    call = worker.run_once.await_args
    assert call.kwargs["workspace_filter"] == workspace_id


@pytest.mark.asyncio
async def test_run_now_workspace_acl_filter_isolates_a_from_b() -> None:
    """Workspace A's run-now never invokes worker.run_once for B.

    This is the cross-workspace ACL invariant for the run-now path.
    The worker's own ``workspace_filter`` argument is the structural
    enforcement point — we assert the REST handler forwards the
    *caller's* workspace_id to that argument and never another's.
    """
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()

    seen_filters: list[uuid.UUID | None] = []

    async def _run_once(*, workspace_filter: uuid.UUID | None = None) -> RunReport:
        seen_filters.append(workspace_filter)
        return RunReport(
            started_at=datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 4, 24, 10, 0, 1, tzinfo=UTC),
            dry_run=False,
            per_workspace=[
                WorkspaceRetentionReport(
                    workspace_id=workspace_filter or uuid.uuid4(),
                    eligible_hot_to_warm_entity_states=0,
                    eligible_hot_to_warm_edges=0,
                    eligible_hot_to_warm_chunks=0,
                    eligible_warm_to_archive_entity_snapshots=0,
                    eligible_warm_to_archive_dates=[],
                    lag_seconds=0.0,
                )
            ],
            duration_seconds=1.0,
        )

    worker = MagicMock(spec=RetentionWorker)
    worker.dry_run = False
    worker.run_once = AsyncMock(side_effect=_run_once)

    tok_a, pt_a = _make_token(["stats:read"], workspace_id=workspace_a)
    app_a = _build_app(token=tok_a, retention_worker=worker)
    async with await _client_for(app_a) as client_a:
        resp_a = await client_a.post(
            "/api/v1/admin/retention/run-now",
            headers={"Authorization": f"Bearer {pt_a}"},
        )

    tok_b, pt_b = _make_token(["stats:read"], workspace_id=workspace_b)
    app_b = _build_app(token=tok_b, retention_worker=worker)
    async with await _client_for(app_b) as client_b:
        resp_b = await client_b.post(
            "/api/v1/admin/retention/run-now",
            headers={"Authorization": f"Bearer {pt_b}"},
        )

    assert resp_a.status_code == 202
    assert resp_b.status_code == 202
    # Each request triggered exactly one run_once call with the caller's
    # own workspace_id — never None, never the other workspace's id.
    assert seen_filters == [workspace_a, workspace_b]


@pytest.mark.asyncio
async def test_run_now_propagates_dry_run_flag_from_live_worker() -> None:
    """Caller sees the live worker's dry_run flag in the response.

    Operators flipping the worker live for the first time on a
    deployment use the dry_run flag to confirm "I am about to run
    something that mutates", and the response surfaces what the
    worker's current configuration is.
    """
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)
    run_report = RunReport(
        started_at=datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 24, 10, 0, 1, tzinfo=UTC),
        dry_run=True,
        per_workspace=[
            WorkspaceRetentionReport(
                workspace_id=workspace_id,
                eligible_hot_to_warm_entity_states=5,
                eligible_hot_to_warm_edges=0,
                eligible_hot_to_warm_chunks=0,
                eligible_warm_to_archive_entity_snapshots=0,
                eligible_warm_to_archive_dates=[],
                lag_seconds=0.0,
            )
        ],
        duration_seconds=1.0,
    )
    worker = MagicMock(spec=RetentionWorker)
    worker.dry_run = True
    worker.run_once = AsyncMock(return_value=run_report)
    app = _build_app(token=tok, retention_worker=worker)
    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/admin/retention/run-now",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["dry_run"] is True
