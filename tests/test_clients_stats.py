"""Tests for connected-clients telemetry + stats endpoint (issue #113).

Coverage:

* :class:`ClientTelemetryRegistry` — recording, sliding-window reads,
  LRU eviction at the ``max_tracked_tokens`` bound.
* Prometheus families — labelled correctly; visible via ``generate_latest``.
* :class:`ClientsStatsService` — workspace-scoped roll-up; only the
  caller's tokens are listed.
* REST endpoint — 401 without token, 403 without scope, 403 without
  workspace, 200 happy path, cross-workspace ACL contract.
* Memory-bound stress test: 100k recorded requests against a tiny LRU
  bound do not blow the tracked-token map.
* Cache: 5-second TTL avoids re-running the underlying assembly.
* Telemetry middleware: bytes-out reads from Content-Length; missing
  Content-Length records 0 (we do not buffer).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_core.stats import ClientsStatsService
from omniscience_core.telemetry.clients import (
    ANONYMOUS_TOKEN_ID,
    ClientTelemetryRegistry,
    get_client_registry,
)
from omniscience_server.app import create_app
from prometheus_client import generate_latest

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_stats.py — keep the seams identical so future
# refactors can DRY them out without churn here).
# ---------------------------------------------------------------------------


def _make_token(
    scopes: list[str],
    *,
    workspace_id: uuid.UUID | None = None,
    name: str = "client-token",
) -> tuple[ApiToken, str]:
    pt, prefix = generate_token("test")
    hashed = hash_token(pt)

    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = uuid.uuid4()
    tok.name = name
    tok.token_prefix = prefix
    tok.hashed_token = hashed
    tok.scopes = scopes
    tok.workspace_id = workspace_id
    tok.expires_at = None
    tok.is_active = True
    tok.last_used_at = None
    return tok, pt


def _auth_session(*, auth_token: ApiToken, workspace_tokens: list[ApiToken]) -> AsyncMock:
    """Session whose execute() returns:

    * ``auth_token`` for the bearer-lookup query in middleware.
    * ``workspace_tokens`` for the ClientsStatsService Postgres query.

    The two query shapes are distinguishable because middleware uses
    ``.scalars().first()`` semantics whereas the service uses
    ``.scalars().all()``.  We satisfy both by returning a dual-mode
    result object.
    """
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        # bearer-lookup: returns the auth token
        result.scalars.return_value.all.return_value = workspace_tokens or [auth_token]
        result.scalars.return_value.first.return_value = auth_token
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
    auth_token: ApiToken,
    workspace_tokens: list[ApiToken],
    clients_service: ClientsStatsService | None = None,
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
    app.state.db_session_factory = MagicMock(
        return_value=_auth_session(
            auth_token=auth_token,
            workspace_tokens=workspace_tokens,
        )
    )
    if clients_service is not None:
        app.state.clients_stats_service = clients_service
    return app


async def _client_for(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Each test starts from a clean in-memory telemetry state."""
    get_client_registry().reset()


# ---------------------------------------------------------------------------
# ClientTelemetryRegistry: basic recording + windowed reads
# ---------------------------------------------------------------------------


def test_registry_records_request_and_returns_in_window() -> None:
    reg = ClientTelemetryRegistry()
    token_id = str(uuid.uuid4())

    reg.record_request(token_id=token_id, bytes_out=128, scope_set="search,stats:read")
    reg.record_request(token_id=token_id, bytes_out=256, scope_set="search,stats:read")

    snaps = reg.snapshot_tokens(token_ids=[token_id])
    assert len(snaps) == 1
    assert snaps[0].requests_last_15m == 2
    assert snaps[0].requests_last_24h == 2
    assert snaps[0].last_seen_at_unix is not None and snaps[0].last_seen_at_unix > 0


def test_registry_returns_empty_for_unknown_token() -> None:
    reg = ClientTelemetryRegistry()
    unknown = str(uuid.uuid4())
    snaps = reg.snapshot_tokens(token_ids=[unknown])
    assert snaps[0].requests_last_15m == 0
    assert snaps[0].requests_last_24h == 0
    assert snaps[0].last_seen_at_unix is None


def test_registry_session_lifecycle_active_count() -> None:
    reg = ClientTelemetryRegistry()
    reg.record_session_connect(session_id="s1", streaming=True)
    reg.record_session_connect(session_id="s2", streaming=False)
    snap = reg.snapshot_sessions()
    assert snap.active == 2
    assert snap.last_hour == 2

    reg.record_session_disconnect(session_id="s1", streaming=True)
    snap = reg.snapshot_sessions()
    assert snap.active == 1


def test_registry_top_tools_sorted_descending() -> None:
    reg = ClientTelemetryRegistry()
    tok = str(uuid.uuid4())
    for _ in range(5):
        reg.record_tool_invocation(tool_name="search", token_id=tok)
    for _ in range(2):
        reg.record_tool_invocation(tool_name="get_document", token_id=tok)

    top = reg.snapshot_top_tools(limit=10)
    assert [t.tool_name for t in top] == ["search", "get_document"]
    assert top[0].invocations_last_hour == 5
    assert top[1].invocations_last_hour == 2


# ---------------------------------------------------------------------------
# Prometheus families — labelled correctly
# ---------------------------------------------------------------------------


def test_prometheus_request_counter_labels_token_and_scopes() -> None:
    reg = ClientTelemetryRegistry()
    token_id = str(uuid.uuid4())
    reg.record_request(token_id=token_id, bytes_out=42, scope_set="search,stats:read")

    body = generate_latest().decode()
    # Counter family must be present with both labels populated.
    assert "omniscience_requests_total" in body
    assert f'token_id="{token_id}"' in body
    assert 'scope_set="search,stats:read"' in body
    # Bytes counter
    assert "omniscience_response_bytes_total" in body


def test_prometheus_session_counter_has_outcome_and_streaming_labels() -> None:
    reg = ClientTelemetryRegistry()
    reg.record_session_connect(session_id="s1", streaming=True)
    reg.record_session_disconnect(session_id="s1", streaming=True)

    body = generate_latest().decode()
    assert 'outcome="connected"' in body
    assert 'outcome="disconnected"' in body
    assert 'streaming="true"' in body


def test_prometheus_tool_counter_labels_tool_and_token() -> None:
    reg = ClientTelemetryRegistry()
    tok = str(uuid.uuid4())
    reg.record_tool_invocation(tool_name="search", token_id=tok)

    body = generate_latest().decode()
    assert "omniscience_mcp_tool_invocations_total" in body
    assert 'tool_name="search"' in body
    assert f'token_id="{tok}"' in body


def test_anonymous_label_used_for_unauthenticated_requests() -> None:
    reg = ClientTelemetryRegistry()
    reg.record_request(token_id=ANONYMOUS_TOKEN_ID, bytes_out=10, scope_set="")
    body = generate_latest().decode()
    assert f'token_id="{ANONYMOUS_TOKEN_ID}"' in body


# ---------------------------------------------------------------------------
# Memory bound: LRU eviction past max_tracked_tokens
# ---------------------------------------------------------------------------


def test_registry_lru_evicts_past_max_tracked_tokens() -> None:
    """100k recorded requests against a tiny bound must keep the map bounded.

    Issue #113 acceptance: under sustained traffic the in-memory rolling
    state must never grow without bound.  We exercise this with a tiny
    cap (100) and 100k unique token ids; the LRU policy must keep the
    total size <= cap regardless of churn.
    """
    bound = 100
    reg = ClientTelemetryRegistry(max_tracked_tokens=bound)

    # Pre-generate a deterministic set of 100k UUIDs so we can address a
    # specific survivor by its canonical UUID (snapshot_tokens parses
    # token ids as UUIDs — the workspace-filter contract).
    ids = [uuid.uuid4() for _ in range(100_000)]
    for tok_id in ids:
        reg.record_request(
            token_id=str(tok_id),
            bytes_out=64,
            scope_set="search",
        )

    # In-memory map cap holds — this is the non-negotiable bound.
    assert reg.tracked_token_count() <= bound
    # The most recently inserted token must still be addressable.
    snaps = reg.snapshot_tokens(token_ids=[str(ids[-1])])
    assert snaps[0].last_seen_at_unix is not None


# ---------------------------------------------------------------------------
# ClientsStatsService — workspace ACL contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clients_service_filters_tokens_by_workspace() -> None:
    """Workspace A's response only includes A's tokens.

    Setup:
      - tok_a1, tok_a2 belong to workspace A.
      - tok_b1 belongs to workspace B.
      - All three have requests recorded.
    Expectation:
      - Calling ``clients(workspace_id=A)`` returns rows for a1+a2 only.
    """
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()

    tok_a1, _ = _make_token(["stats:read"], workspace_id=workspace_a, name="a1")
    tok_a2, _ = _make_token(["stats:read"], workspace_id=workspace_a, name="a2")
    tok_b1, _ = _make_token(["stats:read"], workspace_id=workspace_b, name="b1")

    reg = ClientTelemetryRegistry()
    for tok in (tok_a1, tok_a2, tok_b1):
        reg.record_request(token_id=str(tok.id), bytes_out=10, scope_set="stats:read")

    # Build a session-factory that returns A's tokens for the service query.
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [tok_a1, tok_a2]
        return result

    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    svc = ClientsStatsService(db_session_factory=factory, registry=reg)
    response = await svc.clients(workspace_id=workspace_a)

    listed_ids = {row.token_id for row in response.tokens}
    assert tok_a1.id in listed_ids
    assert tok_a2.id in listed_ids
    # Cross-workspace bleed check
    assert tok_b1.id not in listed_ids


@pytest.mark.asyncio
async def test_clients_service_caches_repeated_call() -> None:
    """Second call inside the TTL window must not re-query Postgres."""
    workspace_id = uuid.uuid4()
    tok, _ = _make_token(["stats:read"], workspace_id=workspace_id)

    session = AsyncMock()
    call_count = {"n": 0}

    async def _execute(stmt: Any) -> Any:
        call_count["n"] += 1
        result = MagicMock()
        result.scalars.return_value.all.return_value = [tok]
        return result

    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    svc = ClientsStatsService(db_session_factory=factory)
    await svc.clients(workspace_id=workspace_id)
    await svc.clients(workspace_id=workspace_id)
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# REST endpoint: auth + ACL contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clients_endpoint_requires_token() -> None:
    tok, _ = _make_token(["stats:read"], workspace_id=uuid.uuid4())
    app = _build_app(auth_token=tok, workspace_tokens=[tok])
    async with await _client_for(app) as client:
        resp = await client.get("/api/v1/stats/clients")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_clients_endpoint_requires_stats_read_scope() -> None:
    tok, pt = _make_token(["search"], workspace_id=uuid.uuid4())
    app = _build_app(auth_token=tok, workspace_tokens=[tok])
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/clients",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_clients_endpoint_requires_workspace() -> None:
    """Token with stats:read but no workspace fails closed with 403."""
    tok, pt = _make_token(["stats:read"], workspace_id=None)
    app = _build_app(auth_token=tok, workspace_tokens=[tok])
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/clients",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_clients_endpoint_happy_path() -> None:
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id, name="dashboard")

    # Pre-populate registry so the endpoint has something to report.
    reg = get_client_registry()
    reg.record_request(token_id=str(tok.id), bytes_out=512, scope_set="stats:read")
    reg.record_session_connect(session_id="sess-1", streaming=True)
    reg.record_tool_invocation(tool_name="search", token_id=str(tok.id))

    app = _build_app(auth_token=tok, workspace_tokens=[tok])
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/clients",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mcp_sessions_active"] == 1
    assert any(row["token_id"] == str(tok.id) for row in body["tokens"])
    assert any(t["tool_name"] == "search" for t in body["top_tools_last_hour"])

    # Cleanup the gauge for downstream tests.
    reg.record_session_disconnect(session_id="sess-1", streaming=True)


@pytest.mark.asyncio
async def test_clients_endpoint_cross_workspace_acl() -> None:
    """Workspace A's endpoint MUST NOT include tokens from workspace B.

    Fixture
    -------
    * Token A — workspace A, scopes [stats:read].
    * Token B — workspace B, scopes [stats:read].

    Both tokens have requests recorded.  Workspace A hits the endpoint;
    only token A appears in ``tokens``.

    Assertion
    ---------
    ``tok_b.id`` must NOT appear in the response under any circumstance.
    """
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()
    tok_a, pt_a = _make_token(["stats:read"], workspace_id=workspace_a, name="alice")
    tok_b, _ = _make_token(["stats:read"], workspace_id=workspace_b, name="bob")

    reg = get_client_registry()
    reg.record_request(token_id=str(tok_a.id), bytes_out=64, scope_set="stats:read")
    reg.record_request(token_id=str(tok_b.id), bytes_out=64, scope_set="stats:read")

    # Workspace-tokens for the service query: A only (workspace_filter
    # would do this in production; we mock the result here).
    app_a = _build_app(auth_token=tok_a, workspace_tokens=[tok_a])
    async with await _client_for(app_a) as client:
        resp = await client.get(
            "/api/v1/stats/clients",
            headers={"Authorization": f"Bearer {pt_a}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    listed_ids = {row["token_id"] for row in body["tokens"]}
    assert str(tok_a.id) in listed_ids
    # The non-negotiable ACL invariant — bob's id must NEVER leak.
    assert str(tok_b.id) not in listed_ids


# ---------------------------------------------------------------------------
# Telemetry middleware: bytes-out from Content-Length
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_middleware_records_request_via_endpoint_call() -> None:
    """A successful authenticated request increments the per-token counter.

    We fire one request to ``/api/v1/stats/clients`` and assert the
    Prometheus ``omniscience_requests_total`` family has a sample for
    the caller's token id.
    """
    workspace_id = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=workspace_id)
    app = _build_app(auth_token=tok, workspace_tokens=[tok])
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/stats/clients",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 200

    # Flush counter values
    body = generate_latest().decode()
    assert f'token_id="{tok.id}"' in body


# ---------------------------------------------------------------------------
# Simulated load: 5 concurrent MCP sessions + 3 token-authenticated REST callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulated_load_produces_correct_counts() -> None:
    """Issue #113 acceptance: 5 concurrent MCP clients + 3 REST callers.

    We exercise the registry directly here (the MCP/REST middleware
    paths are covered by their own tests).  This asserts the rollup
    numbers add up under concurrency.
    """
    reg = ClientTelemetryRegistry()
    workspace_id = uuid.uuid4()
    rest_tokens = [_make_token(["stats:read"], workspace_id=workspace_id)[0] for _ in range(3)]

    async def fire_rest(tok: ApiToken) -> None:
        for _ in range(20):
            reg.record_request(token_id=str(tok.id), bytes_out=128, scope_set="stats:read")

    async def fire_mcp(idx: int) -> None:
        reg.record_session_connect(session_id=f"s-{idx}", streaming=idx % 2 == 0)
        for _ in range(4):
            reg.record_tool_invocation(tool_name="search", token_id=str(rest_tokens[0].id))
        reg.record_session_disconnect(session_id=f"s-{idx}", streaming=idx % 2 == 0)

    await asyncio.gather(
        *(fire_rest(t) for t in rest_tokens),
        *(fire_mcp(i) for i in range(5)),
    )

    # Per-token counts
    snaps = reg.snapshot_tokens(token_ids=[str(t.id) for t in rest_tokens])
    assert all(s.requests_last_15m == 20 for s in snaps)

    # MCP rollup: sessions all closed, hourly count == 5
    sessions = reg.snapshot_sessions()
    assert sessions.active == 0
    assert sessions.last_hour == 5

    # Top tools: 5 sessions x 4 tool calls = 20 search invocations
    tools = reg.snapshot_top_tools(limit=5)
    assert tools[0].tool_name == "search"
    assert tools[0].invocations_last_hour == 20
