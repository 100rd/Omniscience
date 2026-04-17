"""Tests for the auth package: tokens, scopes, middleware, audit logging."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.audit import audit_token_created, audit_token_deleted
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope, check_scopes
from omniscience_core.auth.tokens import (
    create_api_token,
    delete_api_token,
    generate_token,
    hash_token,
    verify_token,
)
from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenRead

# Module-level Depends singletons — avoids ruff B008 (no function calls in defaults)
_current_token_dep = Depends(get_current_token)
_scope_search_dep = Depends(require_scope(Scope.search))
_scope_admin_dep = Depends(require_scope(Scope.admin))

# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


def test_generate_token_format() -> None:
    """Generated token matches the sk_{env}_{uuid}_{random} pattern."""
    plaintext, _prefix = generate_token("development")
    assert re.match(r"^sk_dev_[0-9a-f]{32}_", plaintext), f"Bad format: {plaintext}"


def test_generate_token_env_prefix() -> None:
    """Environment tag uses the first 3 chars of the env argument."""
    plaintext, _ = generate_token("staging")
    assert plaintext.startswith("sk_sta_")


def test_generate_token_prefix_length() -> None:
    """Prefix is exactly the first 8 chars of the full token."""
    plaintext, prefix = generate_token("production")
    assert prefix == plaintext[:8]
    assert len(prefix) == 8


def test_generate_token_unique() -> None:
    """Two calls produce different tokens."""
    t1, _ = generate_token("development")
    t2, _ = generate_token("development")
    assert t1 != t2


# ---------------------------------------------------------------------------
# Hash and verify
# ---------------------------------------------------------------------------


def test_hash_token_returns_string() -> None:
    """hash_token returns a non-empty string."""
    token, _ = generate_token("development")
    hashed = hash_token(token)
    assert isinstance(hashed, str)
    assert len(hashed) > 20


def test_verify_token_correct() -> None:
    """verify_token returns True for the correct plaintext."""
    token, _ = generate_token("development")
    hashed = hash_token(token)
    assert verify_token(token, hashed) is True


def test_verify_token_wrong() -> None:
    """verify_token returns False for a different plaintext."""
    token, _ = generate_token("development")
    hashed = hash_token(token)
    assert verify_token("wrong_token_value", hashed) is False


def test_verify_token_tampered_hash() -> None:
    """verify_token returns False when the hash is corrupted."""
    token, _ = generate_token("development")
    hashed = hash_token(token)
    tampered = hashed[:-4] + "XXXX"
    assert verify_token(token, tampered) is False


# ---------------------------------------------------------------------------
# Scope checking
# ---------------------------------------------------------------------------


def test_check_scopes_exact_match() -> None:
    """A token with exactly the required scope passes."""
    assert check_scopes({Scope.search}, {Scope.search}) is True


def test_check_scopes_admin_implies_all() -> None:
    """Admin scope satisfies every other scope."""
    for scope in Scope:
        assert check_scopes({scope}, {Scope.admin}) is True, f"admin should imply {scope}"


def test_check_scopes_insufficient() -> None:
    """search scope does not grant sources:write."""
    assert check_scopes({Scope.sources_write}, {Scope.search}) is False


def test_check_scopes_sources_write_not_implies_read() -> None:
    """sources:write does not implicitly grant sources:read."""
    assert check_scopes({Scope.sources_read}, {Scope.sources_write}) is False


def test_check_scopes_empty_required() -> None:
    """No required scopes always passes."""
    assert check_scopes(set(), {Scope.search}) is True


def test_check_scopes_multiple_granted() -> None:
    """Multiple granted scopes are all honoured."""
    assert (
        check_scopes({Scope.search, Scope.sources_read}, {Scope.search, Scope.sources_read})
        is True
    )


# ---------------------------------------------------------------------------
# create_api_token (mocked session)
# ---------------------------------------------------------------------------


def _make_mock_token(
    prefix: str = "sk_dev_x",
    scopes: list[str] | None = None,
) -> ApiToken:
    """Construct a minimal ApiToken-like mock."""
    scopes = scopes or ["search"]
    now = datetime.now(tz=UTC)
    token: ApiToken = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.name = "test-token"
    token.token_prefix = prefix
    token.scopes = scopes
    token.created_at = now
    token.expires_at = None
    token.last_used_at = None
    token.is_active = True
    token.hashed_token = "placeholder"
    return token


@pytest.mark.asyncio
async def test_create_api_token_persists_to_db() -> None:
    """create_api_token adds a token to the session and returns (ApiTokenRead, plaintext)."""
    mock_token = _make_mock_token()

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    async def _refresh(obj: Any) -> None:
        obj.id = mock_token.id
        obj.name = mock_token.name
        obj.token_prefix = mock_token.token_prefix
        obj.scopes = mock_token.scopes
        obj.created_at = mock_token.created_at
        obj.expires_at = mock_token.expires_at
        obj.last_used_at = mock_token.last_used_at
        obj.is_active = mock_token.is_active

    session.refresh.side_effect = _refresh

    read_model, plaintext = await create_api_token(session, "my-token", ["search"])

    session.add.assert_called_once()
    session.flush.assert_called_once()
    session.refresh.assert_called_once()

    assert isinstance(read_model, ApiTokenRead)
    assert isinstance(plaintext, str)
    assert plaintext.startswith("sk_")


@pytest.mark.asyncio
async def test_delete_api_token_marks_inactive() -> None:
    """delete_api_token sets is_active=False and flushes."""
    token_id = uuid.uuid4()
    mock_token = _make_mock_token()

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_token)
    session.flush = AsyncMock()

    await delete_api_token(session, token_id)

    assert mock_token.is_active is False
    session.flush.assert_called_once()


@pytest.mark.asyncio
async def test_delete_api_token_noop_when_missing() -> None:
    """delete_api_token is a no-op when token is not found."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()

    await delete_api_token(session, uuid.uuid4())

    session.flush.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers for building test apps
# ---------------------------------------------------------------------------


def _make_fake_session(tokens: list[Any]) -> AsyncMock:
    """Build a reusable fake async session context manager."""
    fake_session = AsyncMock()

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = tokens
        return result

    fake_session.execute = _fake_execute
    fake_session.flush = AsyncMock()
    fake_session.commit = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    return fake_session


def _make_protected_app(mock_token: Any) -> FastAPI:
    """Build a minimal FastAPI app with /protected using get_current_token."""
    app = FastAPI()
    fake_session = _make_fake_session([mock_token])
    app.state.db_session_factory = MagicMock(return_value=fake_session)

    @app.get("/protected")
    async def _protected(token: ApiToken = _current_token_dep) -> dict[str, str]:  # type: ignore[assignment]
        return {"prefix": token.token_prefix}

    return app


# ---------------------------------------------------------------------------
# Middleware — bearer extraction and validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_extracts_bearer_token() -> None:
    """Middleware returns 200 when a valid Bearer token is provided."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)

    now = datetime.now(tz=UTC)
    mock_token: ApiToken = MagicMock(spec=ApiToken)
    mock_token.token_prefix = prefix
    mock_token.hashed_token = hashed
    mock_token.scopes = ["search"]
    mock_token.expires_at = None
    mock_token.is_active = True
    mock_token.last_used_at = now

    app = _make_protected_app(mock_token)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/protected", headers={"Authorization": f"Bearer {plaintext}"}
        )

    assert response.status_code == 200
    assert response.json()["prefix"] == prefix


@pytest.mark.asyncio
async def test_middleware_rejects_missing_token() -> None:
    """Middleware returns 401 when no Authorization header is present."""
    app = FastAPI()

    @app.get("/protected")
    async def _protected(token: ApiToken = _current_token_dep) -> dict[str, str]:  # type: ignore[assignment]
        return {"prefix": token.token_prefix}

    # No db_session_factory — 401 before DB is reached
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/protected")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_middleware_rejects_invalid_token() -> None:
    """Middleware returns 401 when hash verification fails."""
    _, prefix = generate_token("development")
    hashed = hash_token("different_token_than_sent")

    mock_token: ApiToken = MagicMock(spec=ApiToken)
    mock_token.token_prefix = prefix
    mock_token.hashed_token = hashed
    mock_token.scopes = ["search"]
    mock_token.expires_at = None
    mock_token.is_active = True

    app = _make_protected_app(mock_token)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/protected", headers={"Authorization": "Bearer sk_dev_wrongtoken_abc123"}
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_middleware_rejects_expired_token() -> None:
    """Middleware returns 401 when the token has passed its expiry datetime."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    expired_at = datetime.now(tz=UTC) - timedelta(hours=1)

    mock_token: ApiToken = MagicMock(spec=ApiToken)
    mock_token.token_prefix = prefix
    mock_token.hashed_token = hashed
    mock_token.scopes = ["search"]
    mock_token.expires_at = expired_at
    mock_token.is_active = True

    app = _make_protected_app(mock_token)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/protected", headers={"Authorization": f"Bearer {plaintext}"}
        )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# require_scope dependency
# ---------------------------------------------------------------------------


def _make_token_with_scopes(scopes: list[str]) -> ApiToken:
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)
    mock: ApiToken = MagicMock(spec=ApiToken)
    mock.token_prefix = prefix
    mock.hashed_token = hashed
    mock.scopes = scopes
    mock.expires_at = None
    mock.is_active = True
    mock.last_used_at = None
    return mock


@pytest.mark.asyncio
async def test_require_scope_allows_matching_scope() -> None:
    """require_scope passes when the token has the required scope."""
    token = _make_token_with_scopes(["search"])
    fake_session = _make_fake_session([token])

    app = FastAPI()
    app.state.db_session_factory = MagicMock(return_value=fake_session)

    @app.get("/search-only")
    async def _endpoint(tok: ApiToken = _scope_search_dep) -> dict[str, bool]:  # type: ignore[assignment]
        return {"ok": True}

    plaintext, _ = generate_token("development")
    with patch("omniscience_core.auth.middleware.verify_token", return_value=True):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/search-only", headers={"Authorization": f"Bearer {plaintext}"}
            )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_require_scope_rejects_insufficient_scope() -> None:
    """require_scope returns 403 when the token lacks the required scope."""
    token = _make_token_with_scopes(["search"])
    fake_session = _make_fake_session([token])

    app = FastAPI()
    app.state.db_session_factory = MagicMock(return_value=fake_session)

    @app.get("/admin-only")
    async def _endpoint(tok: ApiToken = _scope_admin_dep) -> dict[str, bool]:  # type: ignore[assignment]
        return {"ok": True}

    plaintext, _ = generate_token("development")
    with patch("omniscience_core.auth.middleware.verify_token", return_value=True):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/admin-only", headers={"Authorization": f"Bearer {plaintext}"}
            )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# last_used_at update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_used_at_update_fires() -> None:
    """get_current_token triggers a last_used_at update on the token."""
    plaintext, prefix = generate_token("development")
    hashed = hash_token(plaintext)

    mock_token: ApiToken = MagicMock(spec=ApiToken)
    mock_token.token_prefix = prefix
    mock_token.hashed_token = hashed
    mock_token.scopes = ["search"]
    mock_token.expires_at = None
    mock_token.is_active = True
    mock_token.last_used_at = None

    flush_call_count: list[int] = [0]
    fake_session = AsyncMock()

    async def _fake_execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [mock_token]
        return result

    async def _fake_flush() -> None:
        flush_call_count[0] += 1

    fake_session.execute = _fake_execute
    fake_session.flush = _fake_flush
    fake_session.commit = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    app = FastAPI()
    app.state.db_session_factory = MagicMock(return_value=fake_session)

    @app.get("/check")
    async def _endpoint(token: ApiToken = _current_token_dep) -> dict[str, str]:  # type: ignore[assignment]
        return {"prefix": token.token_prefix}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/check", headers={"Authorization": f"Bearer {plaintext}"})

    # flush was called at least once (last_used_at update)
    assert flush_call_count[0] >= 1


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


def test_audit_token_created_logs() -> None:
    """audit_token_created emits a structured log entry."""
    with patch("omniscience_core.auth.audit.log") as mock_log:
        audit_token_created("sk_dev_ab", ["search", "admin"])
        mock_log.info.assert_called_once()
        call_kwargs = mock_log.info.call_args
        assert call_kwargs[0][0] == "audit.token.created"
        assert call_kwargs[1]["event_type"] == "token_created"
        assert call_kwargs[1]["token_prefix"] == "sk_dev_ab"
        assert call_kwargs[1]["scopes"] == ["search", "admin"]


def test_audit_token_deleted_logs() -> None:
    """audit_token_deleted emits a structured log entry."""
    with patch("omniscience_core.auth.audit.log") as mock_log:
        audit_token_deleted("sk_dev_ab")
        mock_log.info.assert_called_once()
        call_kwargs = mock_log.info.call_args
        assert call_kwargs[0][0] == "audit.token.deleted"
        assert call_kwargs[1]["event_type"] == "token_deleted"
        assert call_kwargs[1]["token_prefix"] == "sk_dev_ab"
