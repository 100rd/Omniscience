"""Tests for the REST API v1 endpoints.

Covers:
- Auth required (401 without token)
- Scope enforcement (403 with wrong scope)
- Rate limiting (429 after burst)
- Error response format (spec-compliant)
- Search endpoint structure
- Sources CRUD
- Documents endpoint
- Ingestion runs endpoints
- Webhook signature verification
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import (
    ApiToken,
    Chunk,
    Document,
    IngestionRun,
    IngestionRunStatus,
    Source,
    SourceStatus,
    SourceType,
)
from omniscience_retrieval.models import QueryStats, SearchHit, SearchResult
from omniscience_server.app import create_app
from omniscience_server.rest.rate_limit import (
    check_rate_limit,
    clear_all_buckets,
    reset_admission_controller_cache,
)

_WS_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(
    scopes: list[str],
    plaintext: str | None = None,
    workspace_id: uuid.UUID | None = None,
) -> tuple[ApiToken, str]:
    """Build an ApiToken mock and matching plaintext."""
    pt, prefix = generate_token("test")
    if plaintext is not None:
        pt = plaintext
    hashed = hash_token(pt)

    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = uuid.uuid4()
    tok.token_prefix = prefix
    tok.hashed_token = hashed
    tok.scopes = scopes
    # workspace_id: use the explicitly supplied value when provided;
    # otherwise leave the MagicMock spec attribute in place so that
    # existing tests (which never set workspace_id) still route through
    # _require_workspace without an explicit None.
    if workspace_id is not None:
        tok.workspace_id = workspace_id
    tok.expires_at = None
    tok.is_active = True
    tok.last_used_at = None
    return tok, pt


def _make_session(
    *,
    get_result: Any = None,
    scalars: list[Any] | None = None,
) -> AsyncMock:
    """Build a reusable fake async session context manager."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        items = scalars or []
        result.scalars.return_value.all.return_value = items
        result.scalars.return_value.first.return_value = items[0] if items else None
        result.scalar_one.return_value = len(items)
        return result

    session.execute = _execute
    session.get = AsyncMock(return_value=get_result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_app_with_token(token: ApiToken, session: AsyncMock | None = None) -> FastAPI:
    """Create the full Omniscience app with mocked auth + DB."""
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    sess = session or _make_session(scalars=[token])
    app.state.db_session_factory = MagicMock(return_value=sess)

    return app


async def _client_for(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def search_token() -> tuple[ApiToken, str]:
    return _make_token(["search"])


@pytest.fixture()
def read_token() -> tuple[ApiToken, str]:
    return _make_token(["sources:read"])


@pytest.fixture()
def write_token() -> tuple[ApiToken, str]:
    return _make_token(["sources:write"])


@pytest.fixture()
def admin_token() -> tuple[ApiToken, str]:
    return _make_token(["admin"])


@pytest.fixture(autouse=True)
def reset_rate_buckets() -> None:
    """Clear rate-limit state between every test."""
    clear_all_buckets()
    reset_admission_controller_cache()


# ---------------------------------------------------------------------------
# Error response format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_format_401() -> None:
    """401 responses use the standard error envelope."""
    tok, _ = _make_token(["search"])
    app = _make_app_with_token(tok)

    async with await _client_for(app) as client:
        resp = await client.post("/api/v1/search", json={"query": "hello"})

    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "unauthorized"
    assert "message" in body["error"]
    assert "details" in body["error"]


@pytest.mark.asyncio
async def test_error_format_403() -> None:
    """403 responses use the standard error envelope."""
    tok, pt = _make_token(["sources:read"])
    app = _make_app_with_token(tok)

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/search",
            json={"query": "hello"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Auth: 401 without token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_requires_auth() -> None:
    tok, _ = _make_token(["search"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.post("/api/v1/search", json={"query": "hello"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_sources_requires_auth() -> None:
    tok, _ = _make_token(["sources:read"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.get("/api/v1/sources")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_source_requires_auth() -> None:
    tok, _ = _make_token(["sources:write"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.post("/api/v1/sources", json={"type": "git", "name": "my-repo"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_document_requires_auth() -> None:
    tok, _ = _make_token(["search"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.get(f"/api/v1/documents/{uuid.uuid4()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_ingestion_runs_requires_auth() -> None:
    tok, _ = _make_token(["sources:read"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.get("/api/v1/ingestion-runs")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scope enforcement: 403 with wrong scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_requires_search_scope() -> None:
    tok, pt = _make_token(["sources:read"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/search",
            json={"query": "hello"},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_sources_requires_read_scope() -> None:
    tok, pt = _make_token(["search"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/sources",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_source_requires_write_scope() -> None:
    tok, pt = _make_token(["sources:read"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/sources",
            json={"type": "git", "name": "my-repo"},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_source_requires_write_scope() -> None:
    tok, pt = _make_token(["sources:read"])
    app = _make_app_with_token(tok)
    async with await _client_for(app) as client:
        resp = await client.delete(
            f"/api/v1/sources/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_scope_grants_all() -> None:
    """Admin-scoped token can access sources:read protected endpoints."""
    tok, pt = _make_token(["admin"])

    auth_session = _make_session(scalars=[tok])
    sources_session = _make_session(scalars=[])  # Empty list of sources

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else sources_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        # Admin has sources:read — should succeed with 200 (empty list)
        resp = await client.get(
            "/api/v1/sources",
            headers={"Authorization": f"Bearer {pt}"},
        )
    # Admin scope grants sources:read — must not be 403
    assert resp.status_code != 403
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_allows_burst() -> None:
    """First N requests within capacity should be allowed."""
    token_id = str(uuid.uuid4())
    for _ in range(5):
        allowed, _ = check_rate_limit(token_id, rpm=60)
        assert allowed is True


def test_rate_limit_blocks_over_capacity() -> None:
    """Once the bucket is exhausted, requests should be blocked."""
    token_id = str(uuid.uuid4())
    rpm = 3

    # Drain the bucket
    for _ in range(rpm):
        check_rate_limit(token_id, rpm=rpm)

    # Next one should be blocked
    allowed, retry_after = check_rate_limit(token_id, rpm=rpm)
    assert allowed is False
    assert retry_after > 0


def test_rate_limit_retry_after_positive() -> None:
    """Retry-after is a positive float when rate-limited."""
    token_id = str(uuid.uuid4())
    rpm = 1

    check_rate_limit(token_id, rpm=rpm)  # First request (drains bucket)
    allowed, retry_after = check_rate_limit(token_id, rpm=rpm)

    assert allowed is False
    assert retry_after > 0.0


def test_rate_limit_refills_over_time() -> None:
    """Bucket refills over time — a 1-rpm limit should recover after 60s."""
    token_id = str(uuid.uuid4())
    rpm = 60

    # Drain all tokens
    for _ in range(rpm):
        check_rate_limit(token_id, rpm=rpm)

    allowed_empty, _ = check_rate_limit(token_id, rpm=rpm)
    assert allowed_empty is False

    # Fast-forward by patching time.monotonic to simulate 1 second elapsed
    # After 1 second at 1 tok/sec, bucket gains 1 token. The bucket math
    # lives in admission.process_local (issue #350 refactor) — patch there.
    from omniscience_server.admission import process_local as admission_process_local

    original_time = admission_process_local.time.monotonic
    fake_now = original_time() + 2.0  # advance by 2 seconds
    with patch.object(admission_process_local.time, "monotonic", return_value=fake_now):
        allowed_refilled, _ = check_rate_limit(token_id, rpm=rpm)
    assert allowed_refilled is True


@pytest.mark.asyncio
async def test_rate_limit_endpoint_returns_429() -> None:
    """After rate limit is hit on an endpoint, the response is 429."""
    tok, pt = _make_token(["search"])
    tok.id = uuid.uuid4()
    session = _make_session(scalars=[tok])
    app = _make_app_with_token(tok, session)

    clear_all_buckets()

    # Exhaust the bucket by calling check_rate_limit directly
    token_id = str(tok.id)
    for _ in range(60):
        check_rate_limit(token_id, rpm=60)

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/search",
            json={"query": "hello"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "rate_limited"
    assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_incident_lane_has_separate_budget_from_background_lane() -> None:
    """AC-SCALE-2: a token carrying incidents:write resolves to the
    incident lane, a separately-bounded budget from a search-only token's
    background lane — draining one lane's bucket must not affect the
    other's, even though both share the same process-local backend."""
    from omniscience_core.config import Settings

    def _app_for(token: ApiToken) -> FastAPI:
        settings = Settings(
            database_url="postgresql+asyncpg://test:test@localhost:5432/test",
            nats_url="nats://localhost:4222",
            log_level="WARNING",
            otlp_endpoint=None,
            environment="test",
            admission_lane_incident_budget=2,
            admission_lane_background_budget=2,
        )
        app = create_app(settings=settings)
        app.state.db_session_factory = MagicMock(return_value=_make_session(scalars=[token]))
        return app

    tok_background, pt_background = _make_token(["search"])
    tok_background.id = uuid.uuid4()
    tok_incident, pt_incident = _make_token(["search", "incidents:write"])
    tok_incident.id = uuid.uuid4()

    clear_all_buckets()

    background_app = _app_for(tok_background)
    async with await _client_for(background_app) as client:
        for _ in range(2):
            await client.post(
                "/api/v1/search",
                json={"query": "hello"},
                headers={"Authorization": f"Bearer {pt_background}"},
            )
        exhausted = await client.post(
            "/api/v1/search",
            json={"query": "hello"},
            headers={"Authorization": f"Bearer {pt_background}"},
        )
    assert exhausted.status_code == 429

    incident_app = _app_for(tok_incident)
    async with await _client_for(incident_app) as client:
        allowed = await client.post(
            "/api/v1/search",
            json={"query": "hello"},
            headers={"Authorization": f"Bearer {pt_incident}"},
        )
    assert allowed.status_code != 429


@pytest.mark.asyncio
async def test_admission_backend_unavailable_returns_503() -> None:
    """AC-SCALE-3/4: selecting a non-'disabled' admission backend activates
    the fail-closed SharedAdmissionBackend stub — every request degrades to
    a stable 503 admission_backend_unavailable, never a silent process-local
    fallback and never a 429 (which would imply the backend was consulted
    and merely exhausted, not lost)."""
    from omniscience_core.config import Settings

    tok, pt = _make_token(["search"])
    tok.id = uuid.uuid4()
    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
        admission_backend="shared-redis-lease",
        admission_backend_identity="redis://admission.internal:6379/0",
        admission_lane_incident_budget=10,
        admission_lane_background_budget=10,
        admission_queue_depth_max=100,
        admission_latency_threshold_ms=250.0,
        admission_error_rate_threshold=0.01,
        admission_failure_reserve_fraction=0.2,
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = MagicMock(return_value=_make_session(scalars=[tok]))

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/search",
            json={"query": "hello"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "admission_backend_unavailable"


def test_default_disabled_posture_has_no_lane_split() -> None:
    """BP review MEDIUM #3 backward-compat proof: the true zero-touch
    default (admission_backend="disabled", no lane budgets set) resolves
    every lane to the single pre-#350 shared bucket at rate_limit_rpm
    capacity — lane-split only activates once a lane budget is explicitly
    configured (see docs/runbooks/read-admission.md "Posture model")."""
    from omniscience_core.config import Settings
    from omniscience_server.admission.lanes import BACKGROUND_LANE, INCIDENT_LANE
    from omniscience_server.rest.rate_limit import _DEFAULT_LANE, _lane_capacity_and_bucket

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    assert settings.admission_backend == "disabled"
    assert settings.admission_lane_incident_budget is None
    assert settings.admission_lane_background_budget is None

    incident_capacity, incident_bucket = _lane_capacity_and_bucket(settings, INCIDENT_LANE)
    background_capacity, background_bucket = _lane_capacity_and_bucket(settings, BACKGROUND_LANE)

    assert incident_bucket == _DEFAULT_LANE
    assert background_bucket == _DEFAULT_LANE
    assert incident_capacity == settings.rate_limit_rpm
    assert background_capacity == settings.rate_limit_rpm


def test_controller_for_caches_shared_backend_across_calls() -> None:
    """BP review MEDIUM #5: a non-'disabled' admission backend must not be
    rebuilt (and any future connection-pool/lease state discarded) on every
    request — the same config must resolve to the same cached controller."""
    from omniscience_core.config import Settings
    from omniscience_server.rest.rate_limit import (
        _controller_for,
        reset_admission_controller_cache,
    )

    reset_admission_controller_cache()
    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
        admission_backend="shared-redis-lease",
        admission_backend_identity="redis://admission.internal:6379/0",
        admission_lane_incident_budget=10,
        admission_lane_background_budget=10,
        admission_queue_depth_max=100,
        admission_latency_threshold_ms=250.0,
        admission_error_rate_threshold=0.01,
        admission_failure_reserve_fraction=0.2,
    )

    first = _controller_for(settings)
    second = _controller_for(settings)
    assert first is second

    other_identity_settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
        admission_backend="shared-redis-lease",
        admission_backend_identity="redis://other-admission.internal:6379/0",
        admission_lane_incident_budget=10,
        admission_lane_background_budget=10,
        admission_queue_depth_max=100,
        admission_latency_threshold_ms=250.0,
        admission_error_rate_threshold=0.01,
        admission_failure_reserve_fraction=0.2,
    )
    third = _controller_for(other_identity_settings)
    assert third is not first

    reset_admission_controller_cache()
    fourth = _controller_for(settings)
    assert fourth is not first


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------


def _make_search_result() -> SearchResult:
    from omniscience_retrieval.models import ChunkLineage, Citation, SourceInfo

    hit = SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=0.95,
        text="The quick brown fox",
        source=SourceInfo(id=uuid.uuid4(), name="my-repo", type="git"),
        citation=Citation(
            uri="https://github.com/org/repo/blob/main/README.md",
            title="README",
            indexed_at=datetime.now(tz=UTC),
            doc_version=1,
        ),
        lineage=ChunkLineage(
            ingestion_run_id=uuid.uuid4(),
            embedding_model="all-minilm-l6-v2",
            embedding_provider="sentence-transformers",
            parser_version="1.0.0",
            chunker_strategy="fixed_window",
        ),
        metadata={},
    )
    return SearchResult(
        hits=[hit],
        query_stats=QueryStats(
            total_matches_before_filters=1,
            vector_matches=1,
            text_matches=0,
            duration_ms=12.5,
        ),
    )


@pytest.mark.asyncio
async def test_search_returns_correct_structure() -> None:
    """POST /api/v1/search returns a SearchResult with hits and query_stats."""
    tok, pt = _make_token(["search"])
    session = _make_session(scalars=[tok])
    app = _make_app_with_token(tok, session)

    result = _make_search_result()
    mock_retrieval = AsyncMock()
    mock_retrieval.search = AsyncMock(return_value=result)
    app.state.retrieval_service = mock_retrieval

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/search",
            json={"query": "quick brown fox", "top_k": 5},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "hits" in body
    assert "query_stats" in body
    assert len(body["hits"]) == 1
    hit = body["hits"][0]
    assert "chunk_id" in hit
    assert "document_id" in hit
    assert "score" in hit
    assert "text" in hit
    assert "source" in hit
    assert "citation" in hit
    assert "lineage" in hit


@pytest.mark.asyncio
async def test_search_passes_request_to_service() -> None:
    """POST /api/v1/search forwards the SearchRequest body to RetrievalService."""
    tok, pt = _make_token(["search"])
    session = _make_session(scalars=[tok])
    app = _make_app_with_token(tok, session)

    result = _make_search_result()
    mock_retrieval = AsyncMock()
    mock_retrieval.search = AsyncMock(return_value=result)
    app.state.retrieval_service = mock_retrieval

    async with await _client_for(app) as client:
        await client.post(
            "/api/v1/search",
            json={"query": "test query", "top_k": 3, "retrieval_strategy": "hybrid"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    mock_retrieval.search.assert_called_once()
    call_args = mock_retrieval.search.call_args[0][0]
    assert call_args.query == "test query"
    assert call_args.top_k == 3
    assert call_args.retrieval_strategy == "hybrid"


@pytest.mark.asyncio
async def test_search_503_when_no_retrieval_service() -> None:
    """POST /api/v1/search returns 503 when retrieval service is not configured."""
    tok, pt = _make_token(["search"])
    session = _make_session(scalars=[tok])
    app = _make_app_with_token(tok, session)
    # Deliberately do not set app.state.retrieval_service

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/search",
            json={"query": "hello"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Sources CRUD
# ---------------------------------------------------------------------------


def _make_source(
    name: str = "test-repo",
    source_type: SourceType = SourceType.git,
    status: SourceStatus = SourceStatus.active,
) -> Source:
    src: Source = MagicMock(spec=Source)
    src.id = uuid.uuid4()
    src.type = source_type
    src.name = name
    src.config = {}
    src.secrets_ref = None
    src.status = status
    src.last_sync_at = None
    src.last_error = None
    src.last_error_at = None
    src.freshness_sla_seconds = None
    src.tenant_id = None
    src.created_at = datetime.now(tz=UTC)
    src.updated_at = datetime.now(tz=UTC)
    return src


@pytest.mark.asyncio
async def test_list_sources_returns_list() -> None:
    """GET /api/v1/sources returns a list of sources."""
    tok, pt = _make_token(["sources:read"])
    src = _make_source()
    # We need separate sessions for auth and sources
    call_count: list[int] = [0]
    auth_session = _make_session(scalars=[tok])
    sources_session = _make_session(scalars=[src])

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else sources_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/sources",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_source_404() -> None:
    """GET /api/v1/sources/{id} returns 404 when source is not found."""
    tok, pt = _make_token(["sources:read"])

    auth_session = _make_session(scalars=[tok])
    get_session = _make_session(get_result=None)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else get_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.get(
            f"/api/v1/sources/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_create_source_returns_201() -> None:
    """POST /api/v1/sources returns 201 with the created source."""
    tok, pt = _make_token(["sources:write"])
    src = _make_source()

    auth_session = _make_session(scalars=[tok])

    # Create session that sets ID on refresh
    create_session = AsyncMock()

    async def _refresh(obj: Any) -> None:
        obj.id = src.id
        obj.type = src.type
        obj.name = src.name
        obj.config = src.config
        obj.secrets_ref = src.secrets_ref
        obj.status = src.status
        obj.last_sync_at = src.last_sync_at
        obj.last_error = src.last_error
        obj.last_error_at = src.last_error_at
        obj.freshness_sla_seconds = src.freshness_sla_seconds
        obj.tenant_id = src.tenant_id
        obj.created_at = src.created_at
        obj.updated_at = src.updated_at

    create_session.add = MagicMock()
    create_session.flush = AsyncMock()
    create_session.refresh = AsyncMock(side_effect=_refresh)
    create_session.commit = AsyncMock()
    create_session.__aenter__ = AsyncMock(return_value=create_session)
    create_session.__aexit__ = AsyncMock(return_value=False)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else create_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/sources",
            json={"type": "git", "name": "test-repo"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "test-repo"
    assert body["type"] == "git"


@pytest.mark.asyncio
async def test_delete_source_204() -> None:
    """DELETE /api/v1/sources/{id} returns 204 when source exists."""
    tok, pt = _make_token(["sources:write"], workspace_id=_WS_A)
    src = _make_source()
    src.tenant_id = _WS_A

    auth_session = _make_session(scalars=[tok])
    del_session = _make_session(get_result=src)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else del_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.delete(
            f"/api/v1/sources/{src.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_source_404() -> None:
    """DELETE /api/v1/sources/{id} returns 404 when source does not exist."""
    tok, pt = _make_token(["sources:write"])

    auth_session = _make_session(scalars=[tok])
    del_session = _make_session(get_result=None)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else del_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.delete(
            f"/api/v1/sources/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_source_requires_write_scope() -> None:
    """PATCH /api/v1/sources/{id} returns 403 with read-only scope."""
    tok, pt = _make_token(["sources:read"])
    app = _make_app_with_token(tok)

    async with await _client_for(app) as client:
        resp = await client.patch(
            f"/api/v1/sources/{uuid.uuid4()}",
            json={"name": "new-name"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_trigger_sync_creates_run() -> None:
    """POST /api/v1/sources/{id}/sync creates a run and returns run_id."""
    tok, pt = _make_token(["sources:write"], workspace_id=_WS_A)
    src = _make_source()
    src.tenant_id = _WS_A
    run_id = uuid.uuid4()

    auth_session = _make_session(scalars=[tok])

    sync_session = AsyncMock()
    sync_session.get = AsyncMock(return_value=src)
    sync_session.add = MagicMock()
    sync_session.flush = AsyncMock()
    sync_session.commit = AsyncMock()

    async def _refresh(obj: Any) -> None:
        obj.id = run_id
        obj.source_id = src.id
        obj.status = IngestionRunStatus.running
        obj.started_at = datetime.now(tz=UTC)
        obj.finished_at = None
        obj.docs_new = 0
        obj.docs_updated = 0
        obj.docs_removed = 0
        obj.run_errors = {}

    sync_session.refresh = AsyncMock(side_effect=_refresh)
    sync_session.__aenter__ = AsyncMock(return_value=sync_session)
    sync_session.__aexit__ = AsyncMock(return_value=False)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else sync_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.post(
            f"/api/v1/sources/{src.id}/sync",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert "run_id" in body
    assert body["run_id"] == str(run_id)


# ---------------------------------------------------------------------------
# Documents endpoint
# ---------------------------------------------------------------------------


def _make_document(source_id: uuid.UUID | None = None) -> Document:
    doc: Document = MagicMock(spec=Document)
    doc.id = uuid.uuid4()
    doc.source_id = source_id or uuid.uuid4()
    doc.external_id = "file.md"
    doc.uri = "https://github.com/org/repo/blob/main/file.md"
    doc.title = "File"
    doc.content_hash = "abc123"
    doc.doc_version = 1
    doc.doc_metadata = {}
    doc.indexed_at = datetime.now(tz=UTC)
    doc.tombstoned_at = None
    return doc


def _make_chunk(document_id: uuid.UUID) -> Chunk:
    chunk: Chunk = MagicMock(spec=Chunk)
    chunk.id = uuid.uuid4()
    chunk.document_id = document_id
    chunk.ord = 0
    chunk.text = "Some chunk text"
    chunk.symbol = None
    chunk.ingestion_run_id = None
    chunk.embedding_model = "all-minilm-l6-v2"
    chunk.embedding_provider = "sentence-transformers"
    chunk.parser_version = "1.0.0"
    chunk.chunker_strategy = "fixed_window"
    chunk.chunk_metadata = {}
    return chunk


@pytest.mark.asyncio
async def test_get_document_returns_document_and_chunks() -> None:
    """GET /api/v1/documents/{id} returns document + chunks."""
    tok, pt = _make_token(["search"], workspace_id=_WS_A)
    doc = _make_document()
    chunk = _make_chunk(doc.id)

    # Source mock whose tenant_id matches the token's workspace.
    from omniscience_core.db.models import Source as _Source

    src_mock: Any = MagicMock(spec=_Source)
    src_mock.id = doc.source_id
    src_mock.tenant_id = _WS_A
    src_mock.name = "test-source"
    src_mock.type = SourceType.git

    auth_session = _make_session(scalars=[tok])

    doc_session = AsyncMock()
    # First call: db.get(Document, id) → doc
    # Second call: db.get(Source, doc.source_id) → src_mock
    doc_session.get = AsyncMock(side_effect=[doc, src_mock])

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [chunk]
        return result

    doc_session.execute = _execute
    doc_session.__aenter__ = AsyncMock(return_value=doc_session)
    doc_session.__aexit__ = AsyncMock(return_value=False)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else doc_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.get(
            f"/api/v1/documents/{doc.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "document" in body
    assert "chunks" in body
    assert len(body["chunks"]) == 1


@pytest.mark.asyncio
async def test_get_document_404() -> None:
    """GET /api/v1/documents/{id} returns 404 when document not found."""
    tok, pt = _make_token(["search"])

    auth_session = _make_session(scalars=[tok])
    not_found_session = _make_session(get_result=None)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else not_found_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.get(
            f"/api/v1/documents/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "document_not_found"


# ---------------------------------------------------------------------------
# Ingestion runs
# ---------------------------------------------------------------------------


def _make_run(source_id: uuid.UUID | None = None) -> IngestionRun:
    run: IngestionRun = MagicMock(spec=IngestionRun)
    run.id = uuid.uuid4()
    run.source_id = source_id or uuid.uuid4()
    run.started_at = datetime.now(tz=UTC)
    run.finished_at = None
    run.status = IngestionRunStatus.ok
    run.docs_new = 5
    run.docs_updated = 2
    run.docs_removed = 0
    run.run_errors = {}
    return run


@pytest.mark.asyncio
async def test_list_ingestion_runs_returns_list() -> None:
    """GET /api/v1/ingestion-runs returns a list of runs."""
    tok, pt = _make_token(["sources:read"])
    run = _make_run()

    auth_session = _make_session(scalars=[tok])
    runs_session = _make_session(scalars=[run])

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else runs_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.get(
            "/api/v1/ingestion-runs",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_ingestion_run_404() -> None:
    """GET /api/v1/ingestion-runs/{id} returns 404 when not found."""
    tok, pt = _make_token(["sources:read"])

    auth_session = _make_session(scalars=[tok])
    not_found_session = _make_session(get_result=None)

    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else not_found_session

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = _factory

    async with await _client_for(app) as client:
        resp = await client.get(
            f"/api/v1/ingestion-runs/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "ingestion_run_not_found"


# ---------------------------------------------------------------------------
# Webhook signature verification (unit tests — no HTTP)
# ---------------------------------------------------------------------------


def test_github_valid_signature() -> None:
    """verify_webhook_signature accepts a valid GitHub HMAC-SHA256 signature."""
    from omniscience_server.rest.webhooks import verify_webhook_signature

    secret = "my-webhook-secret"
    payload = b'{"action": "push"}'
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    request = MagicMock()
    request.headers = {"X-Hub-Signature-256": sig}

    assert verify_webhook_signature("git", payload, secret, request) is True


def test_github_invalid_signature() -> None:
    """verify_webhook_signature rejects a tampered GitHub signature."""
    from omniscience_server.rest.webhooks import verify_webhook_signature

    secret = "my-webhook-secret"
    payload = b'{"action": "push"}'
    bad_sig = "sha256=deadbeef"

    request = MagicMock()
    request.headers = {"X-Hub-Signature-256": bad_sig}

    assert verify_webhook_signature("git", payload, secret, request) is False


def test_github_missing_signature() -> None:
    """verify_webhook_signature rejects when X-Hub-Signature-256 is absent."""
    from omniscience_server.rest.webhooks import verify_webhook_signature

    request = MagicMock()
    request.headers = {}

    assert verify_webhook_signature("git", b"payload", "secret", request) is False


def test_gitlab_valid_token() -> None:
    """verify_webhook_signature accepts a valid GitLab token."""
    from omniscience_server.rest.webhooks import verify_webhook_signature

    secret = "my-gitlab-secret"
    request = MagicMock()
    request.headers = {"X-Gitlab-Token": secret}

    assert verify_webhook_signature("gitlab", b"payload", secret, request) is True


def test_gitlab_invalid_token() -> None:
    """verify_webhook_signature rejects a wrong GitLab token."""
    from omniscience_server.rest.webhooks import verify_webhook_signature

    request = MagicMock()
    request.headers = {"X-Gitlab-Token": "wrong-token"}

    assert verify_webhook_signature("gitlab", b"payload", "correct-secret", request) is False


def test_confluence_valid_signature() -> None:
    """verify_webhook_signature accepts a valid Confluence HMAC-SHA256 signature."""
    from omniscience_server.rest.webhooks import verify_webhook_signature

    secret = "conf-secret"
    payload = b'{"event": "page_updated"}'
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    request = MagicMock()
    request.headers = {"X-Hub-Signature": sig}

    assert verify_webhook_signature("confluence", payload, secret, request) is True


def test_unknown_source_type_passes() -> None:
    """verify_webhook_signature returns True for unknown source types (no check)."""
    from omniscience_server.rest.webhooks import verify_webhook_signature

    request = MagicMock()
    request.headers = {}

    assert verify_webhook_signature("notion", b"payload", "secret", request) is True


@pytest.mark.asyncio
async def test_webhook_404_unknown_source() -> None:
    """POST /api/v1/ingest/webhook/{name} returns 404 for unknown source name."""
    # No auth token needed for webhook — but we need a DB factory

    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    # Session returns no source
    empty_session = AsyncMock()

    async def _execute_empty(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        return result

    empty_session.execute = _execute_empty
    empty_session.__aenter__ = AsyncMock(return_value=empty_session)
    empty_session.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = MagicMock(return_value=empty_session)

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/ingest/webhook/nonexistent-source",
            content=b'{"action": "push"}',
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_webhook_400_bad_signature() -> None:
    """POST /api/v1/ingest/webhook/{name} returns 400 when signature is invalid."""
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    src = _make_source()
    src.config = {"webhook_secret": "correct-secret"}
    src.type = SourceType.git

    source_session = AsyncMock()

    async def _execute_src(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = src
        return result

    source_session.execute = _execute_src
    source_session.__aenter__ = AsyncMock(return_value=source_session)
    source_session.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = MagicMock(return_value=source_session)

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/ingest/webhook/test-repo",
            content=b'{"action": "push"}',
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=baddeadbeef",
            },
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "bad_request"


@pytest.mark.asyncio
async def test_webhook_accepted_no_secret() -> None:
    """POST /api/v1/ingest/webhook/{name} returns 202 when no secret is configured."""
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)

    src = _make_source()
    src.config = {}  # No webhook_secret
    run_id = uuid.uuid4()

    wh_session = AsyncMock()

    async def _execute_src(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = src
        return result

    async def _refresh(obj: Any) -> None:
        obj.id = run_id
        obj.source_id = src.id

    wh_session.execute = _execute_src
    wh_session.add = MagicMock()
    wh_session.flush = AsyncMock()
    wh_session.refresh = AsyncMock(side_effect=_refresh)
    wh_session.commit = AsyncMock()
    wh_session.__aenter__ = AsyncMock(return_value=wh_session)
    wh_session.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = MagicMock(return_value=wh_session)

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/ingest/webhook/test-repo",
            content=b'{"action": "push"}',
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert "events_queued" in body


# ---------------------------------------------------------------------------
# OpenAPI spec endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openapi_schema_available(client: AsyncClient) -> None:
    """GET /api/v1/openapi.json returns 200 with a valid OpenAPI schema."""
    resp = await client.get("/api/v1/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "openapi" in schema
    assert "paths" in schema


@pytest.mark.asyncio
async def test_swagger_ui_in_test_env(client: AsyncClient) -> None:
    """GET /api/docs returns 200 in test environment (dev/test mode)."""
    resp = await client.get("/api/docs")
    # In test env docs_url is enabled — it redirects or returns HTML
    assert resp.status_code in (200, 307)
