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

import omniscience_server.routes.tokens as token_routes
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import MCP_READ_PROFILE_ID, generate_token, hash_token
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

    async def _execute(_stmt: Any, _params: Any = None) -> Any:
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
    authenticated: bool = True,
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
    if authenticated:
        admin = _make_token_obj(scopes=["admin"])
        app.dependency_overrides[token_routes._admin_dep.dependency] = lambda: admin
        app.dependency_overrides[token_routes._optional_token_dep.dependency] = lambda: admin
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
async def test_admin_bootstrap_never_reopens_after_any_token_row() -> None:
    existing_admin = _make_token_obj(scopes=["admin"], is_active=False)
    session = _make_session(scalars=[existing_admin])
    app = _build_app(session, authenticated=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/tokens",
            json={"name": "second-admin", "scopes": ["admin"]},
        )

    assert response.status_code == 401
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_empty_database_bootstrap_allows_only_exact_admin() -> None:
    session = _make_session(scalars=[])
    admin = _make_token_obj(scopes=["admin"])
    with (
        patch(
            "omniscience_server.routes.tokens.generate_token", return_value=("secret", "sk_boot_")
        ),
        patch("omniscience_server.routes.tokens.hash_token", return_value="hash"),
        patch(
            "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
            return_value=_make_read_model(admin),
        ),
    ):
        app = _build_app(session, authenticated=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            rejected = await client.post(
                "/api/v1/tokens", json={"name": "search", "scopes": ["search"]}
            )
            accepted = await client.post(
                "/api/v1/tokens", json={"name": "admin", "scopes": ["admin"]}
            )

    assert rejected.status_code == 403
    assert accepted.status_code == 201


@pytest.mark.asyncio
async def test_bootstrap_acquires_advisory_lock_before_emptiness_check() -> None:
    """The bootstrap path takes the advisory xact-lock before the emptiness SELECT.

    A genuine concurrent-request race is not reproducible on this single
    mocked session / one ASGI harness (see FIX 1 report note), so this test
    proves the code path taken rather than observed concurrent behavior:
    ``pg_advisory_xact_lock`` is the first statement executed on the
    bootstrap path, using the named constant lock key.
    """
    session = _make_session(scalars=[])
    executed: list[tuple[Any, Any]] = []
    original_execute = session.execute

    async def _spy_execute(stmt: Any, params: Any = None) -> Any:
        executed.append((stmt, params))
        return await original_execute(stmt, params)

    session.execute = _spy_execute
    admin = _make_token_obj(scopes=["admin"])

    with (
        patch(
            "omniscience_server.routes.tokens.generate_token",
            return_value=("secret", "sk_boot_"),
        ),
        patch("omniscience_server.routes.tokens.hash_token", return_value="hash"),
        patch(
            "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
            return_value=_make_read_model(admin),
        ),
    ):
        app = _build_app(session, authenticated=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/tokens", json={"name": "boot", "scopes": ["admin"]}
            )

    assert resp.status_code == 201
    assert len(executed) >= 2
    first_stmt, first_params = executed[0]
    assert "pg_advisory_xact_lock" in str(first_stmt)
    assert first_params == {"k": token_routes._BOOTSTRAP_ADVISORY_LOCK_KEY}


@pytest.mark.asyncio
async def test_bootstrap_closes_immediately_after_first_created_token() -> None:
    """A second sequential bootstrap attempt is rejected once a row exists.

    Exercises the lock-guarded bootstrap path end-to-end for the invariant
    FIX 1 protects: after any row appears in ``api_tokens``, bootstrap never
    reopens. True inter-request concurrency can't be reproduced on one
    mocked session (see module docstring / FIX 1 report note); this proves
    the sequential-request invariant instead.
    """
    created_admin = _make_token_obj(scopes=["admin"])
    first_session = _make_session(scalars=[])
    second_session = _make_session(scalars=[created_admin])

    app = FastAPI()
    app.state.db_session_factory = MagicMock(side_effect=[first_session, second_session])
    app.include_router(tokens_router)

    with (
        patch(
            "omniscience_server.routes.tokens.generate_token",
            return_value=("secret", "sk_boot_"),
        ),
        patch("omniscience_server.routes.tokens.hash_token", return_value="hash"),
        patch(
            "omniscience_server.routes.tokens.ApiTokenRead.model_validate",
            return_value=_make_read_model(created_admin),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/api/v1/tokens", json={"name": "boot-1", "scopes": ["admin"]}
            )
            second = await client.post(
                "/api/v1/tokens", json={"name": "boot-2", "scopes": ["admin"]}
            )

    assert first.status_code == 201
    assert second.status_code == 401


@pytest.mark.asyncio
async def test_create_token_with_invalid_bearer_is_401_not_bootstrap() -> None:
    """A supplied-but-invalid Bearer token 401s and never falls through to bootstrap.

    ``get_optional_current_token`` must raise on a malformed/unrecognized
    credential rather than treat it as "no credential supplied" — otherwise
    an attacker holding a garbage token could still hit the bootstrap path.
    """
    session = _make_session(scalars=[])
    app = _build_app(session, authenticated=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/tokens",
            json={"name": "attempt", "scopes": ["admin"]},
            headers={"Authorization": "Bearer sk_bad_0000000000000000000000"},
        )

    assert resp.status_code == 401
    # The route body (which would mint a bootstrap admin token) must never run.
    session.add.assert_not_called()


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
async def test_delete_token_with_mcp_read_profile_id_returns_409() -> None:
    """DELETE on a token with the MCP read profile_id is rejected with 409.

    Such tokens must be revoked via the dedicated admin-authenticated
    profile-revoke endpoint (DELETE .../profiles/omniscience-mcp-read-v1/{id}),
    not the generic token-delete route.
    """
    token_id = uuid.uuid4()
    tok = _make_token_obj(token_id=token_id)
    tok.profile_id = MCP_READ_PROFILE_ID

    session = _make_session(get_result=tok)
    app = _build_app(session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/v1/tokens/{token_id}")

    assert resp.status_code == 409


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
