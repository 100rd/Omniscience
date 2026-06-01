"""Coverage tests for omniscience_server.routes.tokens (target ≥ 80%).

Exercises:
- POST /api/v1/tokens — create token, returns secret once
- GET  /api/v1/tokens — list active tokens
- DELETE /api/v1/tokens/{id} — deactivate token (204)
- DELETE /api/v1/tokens/{id} — not found (404)
- 503 when db_session_factory not set
- _get_env with / without settings on app.state
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenRead
from omniscience_server.routes.tokens import router as tokens_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token_obj(
    *,
    name: str = "test-token",
    scopes: list[str] | None = None,
    is_active: bool = True,
    token_id: uuid.UUID | None = None,
) -> ApiToken:
    """Build a MagicMock ApiToken with realistic fields."""
    plaintext, prefix = generate_token("test")
    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = token_id or uuid.uuid4()
    tok.name = name
    tok.token_prefix = prefix
    tok.hashed_token = hash_token(plaintext)
    tok.scopes = scopes or ["search"]
    tok.workspace_id = None
    tok.created_at = datetime.now(tz=UTC)
    tok.expires_at = None
    tok.last_used_at = None
    tok.is_active = is_active
    return tok


def _make_read_model(tok: ApiToken) -> ApiTokenRead:
    return ApiTokenRead(
        id=tok.id,
        name=tok.name,
        token_prefix=tok.token_prefix,
        scopes=tok.scopes,
        workspace_id=tok.workspace_id,
        created_at=tok.created_at,
        expires_at=tok.expires_at,
        last_used_at=tok.last_used_at,
        is_active=tok.is_active,
    )


def _make_session(
    *,
    get_result: Any = None,
    scalars: list[Any] | None = None,
) -> AsyncMock:
    session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        items = scalars or []
        result.scalars.return_value.all.return_value = items
        result.scalars.return_value.first.return_value = items[0] if items else None
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


def _build_app(
    session: AsyncMock | None = None,
    *,
    include_db: bool = True,
    include_settings: bool = False,
) -> FastAPI:
    app = FastAPI()
    if include_db:
        sess = session or _make_session()
        app.state.db_session_factory = MagicMock(return_value=sess)
    if include_settings:
        settings = MagicMock()
        settings.environment = "production"
        app.state.settings = settings
    app.include_router(tokens_router)
    return app


# ---------------------------------------------------------------------------
# POST /api/v1/tokens — create token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_token_returns_201_and_secret() -> None:
    tok = _make_token_obj(name="new-tok", scopes=["search"])
    read_model = _make_read_model(tok)

    session = _make_session()
    session.refresh = AsyncMock(side_effect=lambda obj: None)

    with (
        patch(
            "omniscience_server.routes.tokens.generate_token",
            return_value=("sk_test_plaintext", "sk_test_"),
        ),
        patch(
            "omniscience_server.routes.tokens.hash_token",
            return_value="hashed_value",
        ),
        patch(
            "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
            return_value=read_model,
        ),
        patch("omniscience_server.routes.tokens.audit_token_created"),
    ):
        app = _build_app(session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/tokens",
                json={"name": "new-tok", "scopes": ["search"]},
            )

    assert resp.status_code == 201
    body = resp.json()
    assert "token" in body
    assert "secret" in body
    assert body["secret"] == "sk_test_plaintext"


@pytest.mark.asyncio
async def test_create_token_with_expiry() -> None:
    tok = _make_token_obj()
    read_model = _make_read_model(tok)
    expires = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()

    session = _make_session()
    with (
        patch(
            "omniscience_server.routes.tokens.generate_token",
            return_value=("sk_t_plain", "sk_t_"),
        ),
        patch("omniscience_server.routes.tokens.hash_token", return_value="h"),
        patch(
            "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
            return_value=read_model,
        ),
        patch("omniscience_server.routes.tokens.audit_token_created"),
    ):
        app = _build_app(session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/tokens",
                json={"name": "expiring", "scopes": ["admin"], "expires_at": expires},
            )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_token_uses_env_from_settings() -> None:
    """When app.state.settings is present, generate_token receives its env value."""
    tok = _make_token_obj()
    read_model = _make_read_model(tok)

    session = _make_session()
    captured: list[str] = []

    def _fake_generate(env: str) -> tuple[str, str]:
        captured.append(env)
        return "sk_pro_plain", "sk_pro_"

    with (
        patch("omniscience_server.routes.tokens.generate_token", side_effect=_fake_generate),
        patch("omniscience_server.routes.tokens.hash_token", return_value="h"),
        patch(
            "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
            return_value=read_model,
        ),
        patch("omniscience_server.routes.tokens.audit_token_created"),
    ):
        app = _build_app(session, include_settings=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/tokens",
                json={"name": "prod-tok", "scopes": ["search"]},
            )

    assert resp.status_code == 201
    assert captured == ["production"]


@pytest.mark.asyncio
async def test_create_token_503_when_no_db() -> None:
    app = _build_app(include_db=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/tokens",
            json={"name": "x", "scopes": ["search"]},
        )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/tokens — list tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tokens_returns_active_tokens() -> None:
    tok1 = _make_token_obj(name="alpha", scopes=["search"])
    tok2 = _make_token_obj(name="beta", scopes=["admin"])

    read1 = _make_read_model(tok1)
    read2 = _make_read_model(tok2)

    session = _make_session(scalars=[tok1, tok2])
    with patch(
        "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
        side_effect=[read1, read2],
    ):
        app = _build_app(session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/tokens")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2


@pytest.mark.asyncio
async def test_list_tokens_empty() -> None:
    session = _make_session(scalars=[])
    app = _build_app(session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/tokens")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_tokens_503_when_no_db() -> None:
    app = _build_app(include_db=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/tokens")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# DELETE /api/v1/tokens/{id} — deactivate token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_token_returns_204() -> None:
    token_id = uuid.uuid4()
    tok = _make_token_obj(token_id=token_id)

    session = _make_session(get_result=tok)
    with (
        patch("omniscience_server.routes.tokens.delete_api_token", new_callable=AsyncMock),
        patch("omniscience_server.routes.tokens.audit_token_deleted"),
    ):
        app = _build_app(session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete(f"/api/v1/tokens/{token_id}")

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_token_not_found_returns_404() -> None:
    token_id = uuid.uuid4()
    # session.get returns None → token not found
    session = _make_session(get_result=None)

    app = _build_app(session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/v1/tokens/{token_id}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Token not found"


@pytest.mark.asyncio
async def test_delete_token_503_when_no_db() -> None:
    token_id = uuid.uuid4()
    app = _build_app(include_db=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/v1/tokens/{token_id}")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_delete_token_invalid_uuid_422() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/v1/tokens/not-a-uuid")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Validation: missing required fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_token_missing_name_422() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/tokens", json={"scopes": ["search"]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_token_missing_scopes_422() -> None:
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/tokens", json={"name": "tok"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# _get_env — fallback when no settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_env_fallback_to_development() -> None:
    """Without app.state.settings the env defaults to 'development'."""
    tok = _make_token_obj()
    read_model = _make_read_model(tok)
    session = _make_session()

    captured: list[str] = []

    def _fake_generate(env: str) -> tuple[str, str]:
        captured.append(env)
        return "sk_dev_plain", "sk_dev_"

    with (
        patch("omniscience_server.routes.tokens.generate_token", side_effect=_fake_generate),
        patch("omniscience_server.routes.tokens.hash_token", return_value="h"),
        patch(
            "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
            return_value=read_model,
        ),
        patch("omniscience_server.routes.tokens.audit_token_created"),
    ):
        # No settings on app.state → _get_env returns "development"
        app = _build_app(session, include_settings=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/tokens",
                json={"name": "dev-tok", "scopes": ["search"]},
            )

    assert resp.status_code == 201
    assert captured == ["development"]
