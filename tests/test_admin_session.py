"""Tests for admin session endpoints and CSRF enforcement.

Coverage matrix:
  POST /admin/session
    - valid admin token -> 204, sets session + CSRF cookies
    - invalid/unknown token -> 401
    - token with non-admin scope -> 403
    - empty token body -> 422 (pydantic validation)

  DELETE /admin/session
    - always 204, clears both cookies

  CSRF enforcement (via get_current_token / cookie path)
    - GET with cookie, no CSRF header -> 200 (reads are exempt)
    - POST with cookie + matching X-CSRF-Token -> 200
    - POST with cookie + missing X-CSRF-Token -> 403
    - POST with cookie + wrong X-CSRF-Token -> 403
    - POST with Bearer header, no CSRF header -> 200 (bearer exempt)
    - Bearer takes priority over cookie
    - No auth -> 401
    - Cookie with inactive token -> 401
"""

from __future__ import annotations

import secrets
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.middleware import get_current_token
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken

# Module-level singleton — avoids ruff B008
_get_token_dep: Any = Depends(get_current_token)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_mock_token(
    plaintext: str,
    prefix: str,
    scopes: list[str],
    hashed: str,
) -> ApiToken:
    """Build a minimal ApiToken mock for test injection."""
    mock: ApiToken = MagicMock(spec=ApiToken)
    mock.token_prefix = prefix
    mock.hashed_token = hashed
    mock.scopes = scopes
    mock.expires_at = None
    mock.is_active = True
    mock.last_used_at = None
    return mock


def _make_fake_session(tokens: list[Any]) -> AsyncMock:
    """Async session context-manager returning *tokens* on execute."""
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = tokens
        return result

    session.execute = _execute
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_session_app(tokens: list[Any]) -> FastAPI:
    """Minimal app wired with the admin session router."""
    from omniscience_server.rest.admin_session import router as session_router

    app = FastAPI()
    fake_session = _make_fake_session(tokens)
    app.state.db_session_factory = MagicMock(return_value=fake_session)
    settings = MagicMock()
    settings.environment = "test"
    app.state.settings = settings
    app.include_router(session_router)
    return app


def _make_protected_app(tokens: list[Any]) -> FastAPI:
    """Minimal app with cookie-authenticated endpoints for CSRF tests."""
    app = FastAPI()
    fake_session = _make_fake_session(tokens)
    app.state.db_session_factory = MagicMock(return_value=fake_session)

    @app.get("/probe")
    async def _probe(token: ApiToken = _get_token_dep) -> dict[str, str]:  # type: ignore[assignment]
        return {"prefix": token.token_prefix}

    @app.post("/mutate")
    async def _mutate(token: ApiToken = _get_token_dep) -> dict[str, str]:  # type: ignore[assignment]
        return {"prefix": token.token_prefix}

    return app


# ---------------------------------------------------------------------------
# POST /admin/session — valid token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_sets_cookies_for_admin_token() -> None:
    """Valid admin token -> 204 + both cookies present."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_session_app([mock_token])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/session", json={"token": plaintext})

    assert response.status_code == 204
    assert "omniscience_admin_session" in response.cookies
    assert "csrf_token" in response.cookies


@pytest.mark.asyncio
async def test_create_session_session_cookie_is_httponly() -> None:
    """Session cookie must carry HttpOnly flag (not readable by JS)."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_session_app([mock_token])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/session", json={"token": plaintext})

    raw = "\n".join(response.headers.get_list("set-cookie")).lower()
    assert "httponly" in raw


@pytest.mark.asyncio
async def test_create_session_samesite_strict() -> None:
    """Session and CSRF cookies must have SameSite=Strict."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_session_app([mock_token])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/session", json={"token": plaintext})

    raw = "\n".join(response.headers.get_list("set-cookie")).lower()
    assert "samesite=strict" in raw


# ---------------------------------------------------------------------------
# POST /admin/session — invalid / non-admin token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_rejects_unknown_token() -> None:
    """No matching token in DB -> 401."""
    app = _make_session_app([])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        plaintext, _ = generate_token("development")
        response = await client.post("/admin/session", json={"token": plaintext})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_session_rejects_non_admin_scope() -> None:
    """Token with search-only scope -> 403."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["search"], hashed)
    app = _make_session_app([mock_token])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/session", json={"token": plaintext})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_session_rejects_empty_token() -> None:
    """Empty/whitespace token string -> 422 (pydantic validation)."""
    app = _make_session_app([])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/session", json={"token": "   "})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /admin/session — logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_clears_cookies() -> None:
    """DELETE /admin/session returns 204 and expires both cookies."""
    app = _make_session_app([])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete("/admin/session")

    assert response.status_code == 204
    raw = "\n".join(response.headers.get_list("set-cookie")).lower()
    assert "max-age=0" in raw
    assert "omniscience_admin_session" in raw
    assert "csrf_token" in raw


# ---------------------------------------------------------------------------
# CSRF enforcement (cookie auth path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_request_via_cookie_exempt_from_csrf() -> None:
    """GET with session cookie but no CSRF header -> 200 (reads exempt)."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_protected_app([mock_token])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"omniscience_admin_session": plaintext},
    ) as client:
        response = await client.get("/probe")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_post_via_cookie_with_matching_csrf_succeeds() -> None:
    """POST with matching X-CSRF-Token header -> 200."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_protected_app([mock_token])

    csrf_value = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"omniscience_admin_session": plaintext, "csrf_token": csrf_value},
    ) as client:
        response = await client.post("/mutate", headers={"X-CSRF-Token": csrf_value})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_post_via_cookie_missing_csrf_header_rejected() -> None:
    """POST with cookie but no X-CSRF-Token header -> 403."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_protected_app([mock_token])

    csrf_value = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"omniscience_admin_session": plaintext, "csrf_token": csrf_value},
    ) as client:
        response = await client.post("/mutate")  # no X-CSRF-Token header

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "csrf_invalid"


@pytest.mark.asyncio
async def test_post_via_cookie_wrong_csrf_header_rejected() -> None:
    """POST with wrong X-CSRF-Token value -> 403."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_protected_app([mock_token])

    csrf_value = secrets.token_urlsafe(32)
    wrong_csrf = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"omniscience_admin_session": plaintext, "csrf_token": csrf_value},
    ) as client:
        response = await client.post("/mutate", headers={"X-CSRF-Token": wrong_csrf})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "csrf_invalid"


@pytest.mark.asyncio
async def test_bearer_post_exempt_from_csrf() -> None:
    """POST with Bearer Authorization header requires no X-CSRF-Token."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock_token = _make_mock_token(plaintext, prefix, ["admin"], hashed)
    app = _make_protected_app([mock_token])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/mutate", headers={"Authorization": f"Bearer {plaintext}"})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_bearer_takes_priority_over_cookie() -> None:
    """When both Bearer header and session cookie present, Bearer wins."""
    bearer_plaintext, bearer_prefix = generate_token("development")
    bearer_hashed = hash_token(bearer_plaintext)
    bearer_token = _make_mock_token(bearer_plaintext, bearer_prefix, ["admin"], bearer_hashed)

    cookie_plaintext, _ = generate_token("development")
    app = _make_protected_app([bearer_token])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"omniscience_admin_session": cookie_plaintext},
    ) as client:
        response = await client.get(
            "/probe", headers={"Authorization": f"Bearer {bearer_plaintext}"}
        )

    assert response.status_code == 200
    assert response.json()["prefix"] == bearer_prefix


@pytest.mark.asyncio
async def test_no_auth_returns_401() -> None:
    """No bearer header and no session cookie -> 401."""
    app = _make_protected_app([])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/probe")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_cookie_with_inactive_token_rejected() -> None:
    """Session cookie present but token is not in DB (deactivated) -> 401."""
    plaintext, _ = generate_token("development")
    app = _make_protected_app([])  # empty list simulates deactivated/missing token

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"omniscience_admin_session": plaintext},
    ) as client:
        response = await client.get("/probe")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Import sanity
# ---------------------------------------------------------------------------


def test_lookup_token_is_accessible() -> None:
    """_lookup_token must be importable from the middleware module."""
    from omniscience_core.auth.middleware import _lookup_token  # type: ignore[attr-defined]

    assert callable(_lookup_token)
