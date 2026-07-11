"""Tenant isolation tests for REST endpoints.

Verifies that bearer tokens scoped to workspace A cannot read, modify, or
delete resources that belong to workspace B.

Endpoints under test:
  GET    /api/v1/sources            — list_sources
  POST   /api/v1/sources            — create_source
  GET    /api/v1/sources/{id}       — get_source
  PATCH  /api/v1/sources/{id}       — update_source
  DELETE /api/v1/sources/{id}       — delete_source
  GET    /api/v1/sources/{id}/stats — source_stats
  GET    /api/v1/documents/{id}     — get_document

Security invariants tested:
  1. Token for workspace A sees only workspace-A sources in list.
  2. Token for workspace A gets 404 (not 403) for a workspace-B source by id.
  3. Token for workspace A cannot PATCH a workspace-B source (404).
  4. Token for workspace A cannot DELETE a workspace-B source (404).
  5. Token for workspace A gets 404 for stats of a workspace-B source.
  6. Token for workspace A gets 404 for a document whose parent source is in workspace B.
  7. Legacy token (workspace_id=None) is rejected fail-closed with 403 on
     all read and write source endpoints, and on the document endpoint.
  8. Source creation derives tenant_id from the token and ignores caller claims.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.config import Settings
from omniscience_core.db.models import (
    ApiToken,
    Document,
    Source,
    SourceStatus,
    SourceType,
)
from omniscience_server.app import create_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_WS_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(
    *,
    workspace_id: uuid.UUID | None,
    scopes: list[str],
) -> tuple[ApiToken, str]:
    """Return an (ApiToken mock, plaintext) pair."""
    plaintext, prefix = generate_token("test")
    hashed = hash_token(plaintext)

    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = uuid.uuid4()
    tok.name = f"token-ws-{workspace_id}"
    tok.token_prefix = prefix
    tok.hashed_token = hashed
    tok.scopes = scopes
    tok.workspace_id = workspace_id
    tok.expires_at = None
    tok.is_active = True
    tok.last_used_at = None
    return tok, plaintext


def _make_source(
    *,
    tenant_id: uuid.UUID | None,
    name: str = "src",
    source_id: uuid.UUID | None = None,
) -> Source:
    """Minimal Source mock."""
    src: Source = MagicMock(spec=Source)
    src.id = source_id or uuid.uuid4()
    src.type = SourceType.git
    src.name = name
    src.config = {}
    src.secrets_ref = None
    src.status = SourceStatus.active
    src.last_sync_at = None
    src.last_error = None
    src.last_error_at = None
    src.freshness_sla_seconds = None
    src.tenant_id = tenant_id
    src.created_at = datetime.now(tz=UTC)
    src.updated_at = datetime.now(tz=UTC)
    return src


def _make_document(
    *,
    source_id: uuid.UUID,
    doc_id: uuid.UUID | None = None,
) -> Document:
    """Minimal Document mock."""
    doc: Document = MagicMock(spec=Document)
    doc.id = doc_id or uuid.uuid4()
    doc.source_id = source_id
    doc.external_id = "ext-1"
    doc.uri = "file:///a.txt"
    doc.title = "A"
    doc.content_hash = "abc123"
    doc.doc_version = 1
    doc.indexed_at = datetime.now(tz=UTC)
    doc.tombstoned_at = None
    doc.doc_metadata = {}
    return doc


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )


def _make_auth_session(token: ApiToken) -> AsyncMock:
    """Session that returns ``token`` for auth middleware queries."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        res.scalars.return_value.all.return_value = [token]
        return res

    session.execute = _execute
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_data_session(
    *,
    get_result: Any = None,
    scalars: list[Any] | None = None,
) -> AsyncMock:
    """Session used for business-logic queries."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        items = scalars or []
        res.scalars.return_value.all.return_value = items
        res.scalars.return_value.first.return_value = items[0] if items else None
        res.scalar_one.return_value = 0
        res.scalar_one_or_none.return_value = None
        return res

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


def _make_create_session() -> AsyncMock:
    """Return a data session that simulates database-generated source fields."""
    session = _make_data_session()

    async def _refresh(source: Source) -> None:
        source.id = source.id or uuid.uuid4()
        source.last_sync_at = None
        source.last_error = None
        source.last_error_at = None
        source.created_at = datetime.now(tz=UTC)
        source.updated_at = datetime.now(tz=UTC)

    session.refresh = AsyncMock(side_effect=_refresh)
    return session


def _build_app(
    token: ApiToken,
    data_session: AsyncMock,
) -> FastAPI:
    """Create the full app with auth and data sessions."""
    app = create_app(settings=_make_settings())
    auth_session = _make_auth_session(token)
    call_count: list[int] = [0]

    def _factory() -> AsyncMock:
        call_count[0] += 1
        return auth_session if call_count[0] == 1 else data_session

    app.state.db_session_factory = _factory
    return app


async def _get_client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# create_source — server-derived workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_source_uses_token_workspace_not_caller_tenant() -> None:
    """POST /sources ignores a foreign tenant_id claimed by the request body."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:write"])
    data_session = _make_create_session()
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.post(
            "/api/v1/sources",
            json={"type": "git", "name": "owned-source", "tenant_id": str(_WS_B)},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 201
    created = data_session.add.call_args.args[0]
    assert created.tenant_id == _WS_A
    assert resp.json()["tenant_id"] == str(_WS_A)


@pytest.mark.asyncio
async def test_create_source_legacy_token_rejected_before_write() -> None:
    """POST /sources rejects a token without workspace_id before persistence."""
    tok, pt = _make_token(workspace_id=None, scopes=["sources:write"])
    data_session = _make_create_session()
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.post(
            "/api/v1/sources",
            json={"type": "git", "name": "unscoped-source"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    data_session.add.assert_not_called()


# ---------------------------------------------------------------------------
# list_sources — workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sources_returns_only_own_workspace_sources() -> None:
    """GET /sources returns only sources with tenant_id matching the token's workspace."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:read"])
    # Data session returns a source that belongs to workspace A
    src_a = _make_source(tenant_id=_WS_A, name="ws-a-source")
    data_session = _make_data_session(scalars=[src_a])
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            "/api/v1/sources",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["name"] == "ws-a-source"


@pytest.mark.asyncio
async def test_list_sources_cross_tenant_excluded() -> None:
    """GET /sources does not return sources from workspace B when token is workspace A."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:read"])
    # Data session returns no rows (because the SQL filter by tenant_id would exclude WS_B rows)
    data_session = _make_data_session(scalars=[])
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            "/api/v1/sources",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_sources_legacy_token_rejected_403() -> None:
    """GET /sources with a legacy token (workspace_id=None) returns 403 fail-closed."""
    tok, pt = _make_token(workspace_id=None, scopes=["sources:read"])
    data_session = _make_data_session(scalars=[])
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            "/api/v1/sources",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# get_source — cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_source_cross_tenant_returns_404() -> None:
    """GET /sources/{id} returns 404 when the source belongs to workspace B."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:read"])
    # The source exists but belongs to WS_B
    src_b = _make_source(tenant_id=_WS_B, name="ws-b-source")
    data_session = _make_data_session(get_result=src_b)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/sources/{src_b.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_get_source_legacy_token_rejected_403() -> None:
    """GET /sources/{id} with legacy token (workspace_id=None) returns 403."""
    tok, pt = _make_token(workspace_id=None, scopes=["sources:read"])
    src = _make_source(tenant_id=_WS_A)
    data_session = _make_data_session(get_result=src)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/sources/{src.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_source_own_workspace_succeeds() -> None:
    """GET /sources/{id} succeeds when source belongs to the token's workspace."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:read"])
    src = _make_source(tenant_id=_WS_A, name="my-source")
    data_session = _make_data_session(get_result=src)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/sources/{src.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    assert resp.json()["name"] == "my-source"


# ---------------------------------------------------------------------------
# update_source — cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_source_cross_tenant_returns_404() -> None:
    """PATCH /sources/{id} returns 404 when source belongs to workspace B."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:write"])
    src_b = _make_source(tenant_id=_WS_B)
    data_session = _make_data_session(get_result=src_b)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.patch(
            f"/api/v1/sources/{src_b.id}",
            json={"name": "hacked"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_update_source_legacy_token_rejected_403() -> None:
    """PATCH /sources/{id} with legacy token returns 403 fail-closed."""
    tok, pt = _make_token(workspace_id=None, scopes=["sources:write"])
    src = _make_source(tenant_id=_WS_A)
    data_session = _make_data_session(get_result=src)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.patch(
            f"/api/v1/sources/{src.id}",
            json={"name": "attempt"},
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# delete_source — cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_source_cross_tenant_returns_404() -> None:
    """DELETE /sources/{id} returns 404 when source belongs to workspace B."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:write"])
    src_b = _make_source(tenant_id=_WS_B)
    data_session = _make_data_session(get_result=src_b)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.delete(
            f"/api/v1/sources/{src_b.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_delete_source_legacy_token_rejected_403() -> None:
    """DELETE /sources/{id} with legacy token returns 403 fail-closed."""
    tok, pt = _make_token(workspace_id=None, scopes=["sources:write"])
    src = _make_source(tenant_id=_WS_A)
    data_session = _make_data_session(get_result=src)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.delete(
            f"/api/v1/sources/{src.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# source_stats — cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_stats_cross_tenant_returns_404() -> None:
    """GET /sources/{id}/stats returns 404 when source belongs to workspace B."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["sources:read"])
    src_b = _make_source(tenant_id=_WS_B)
    data_session = _make_data_session(get_result=src_b)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/sources/{src_b.id}/stats",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_source_stats_legacy_token_rejected_403() -> None:
    """GET /sources/{id}/stats with legacy token returns 403 fail-closed."""
    tok, pt = _make_token(workspace_id=None, scopes=["sources:read"])
    src = _make_source(tenant_id=_WS_A)
    data_session = _make_data_session(get_result=src)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/sources/{src.id}/stats",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# get_document — cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_document_cross_tenant_returns_404() -> None:
    """GET /documents/{id} returns 404 when document's source is in workspace B."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["search"])

    src_b = _make_source(tenant_id=_WS_B)
    doc = _make_document(source_id=src_b.id)

    # We need a session that returns the doc from .get(Document, ...) on first
    # call and the source from .get(Source, ...) on the second call.
    session = AsyncMock()
    call_count: list[int] = [0]

    async def _get(model: Any, pk: Any) -> Any:
        call_count[0] += 1
        if call_count[0] == 1:
            return doc  # Document lookup
        return src_b  # Source lookup

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res

    session.get = _get
    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    tok_obj = tok  # ApiToken object
    auth_session = _make_auth_session(tok_obj)

    app = create_app(settings=_make_settings())
    req_count: list[int] = [0]

    def _factory() -> Any:
        req_count[0] += 1
        return auth_session if req_count[0] == 1 else session

    app.state.db_session_factory = _factory

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/documents/{doc.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "document_not_found"


@pytest.mark.asyncio
async def test_get_document_own_workspace_succeeds() -> None:
    """GET /documents/{id} succeeds when document's source is in the same workspace."""
    tok, pt = _make_token(workspace_id=_WS_A, scopes=["search"])

    src_a = _make_source(tenant_id=_WS_A)
    doc = _make_document(source_id=src_a.id)

    session = AsyncMock()
    call_count: list[int] = [0]

    async def _get(model: Any, pk: Any) -> Any:
        call_count[0] += 1
        if call_count[0] == 1:
            return doc
        return src_a

    async def _execute(stmt: Any) -> Any:
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        return res

    session.get = _get
    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    auth_session = _make_auth_session(tok)
    app = create_app(settings=_make_settings())
    req_count: list[int] = [0]

    def _factory() -> Any:
        req_count[0] += 1
        return auth_session if req_count[0] == 1 else session

    app.state.db_session_factory = _factory

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/documents/{doc.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "document" in data
    assert "chunks" in data


@pytest.mark.asyncio
async def test_get_document_legacy_token_rejected_403() -> None:
    """GET /documents/{id} with legacy token (workspace_id=None) returns 403."""
    tok, pt = _make_token(workspace_id=None, scopes=["search"])
    src = _make_source(tenant_id=_WS_A)
    doc = _make_document(source_id=src.id)

    data_session = _make_data_session(get_result=doc)
    app = _build_app(tok, data_session)

    async with await _get_client(app) as client:
        resp = await client.get(
            f"/api/v1/documents/{doc.id}",
            headers={"Authorization": f"Bearer {pt}"},
        )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
