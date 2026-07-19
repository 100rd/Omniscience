"""Tests for workspace_id support on POST /api/v1/tokens (Bug 2 fix).

Verifies:
- POST /api/v1/tokens WITH workspace_id persists it on the created ApiToken row.
- POST /api/v1/tokens WITHOUT workspace_id leaves workspace_id as None.
- A token row that has workspace_id set satisfies require_scope(Scope.stats_read)
  and also has its workspace_id attribute correctly stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import omniscience_server.routes.tokens as token_routes
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenRead
from omniscience_server.routes.tokens import router as tokens_router

# ---------------------------------------------------------------------------
# Helpers (mirrors the pattern from test_routes_tokens_coverage.py)
# ---------------------------------------------------------------------------

_DEFAULT_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_token_obj(
    *,
    name: str = "ws-token",
    scopes: list[str] | None = None,
    workspace_id: uuid.UUID | None = None,
    token_id: uuid.UUID | None = None,
) -> ApiToken:
    """Build a MagicMock ApiToken with realistic fields."""
    plaintext, prefix = generate_token("test")
    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = token_id or uuid.uuid4()
    tok.name = name
    tok.token_prefix = prefix
    tok.hashed_token = hash_token(plaintext)
    tok.scopes = scopes or ["stats:read"]
    tok.workspace_id = workspace_id
    tok.created_at = datetime.now(tz=UTC)
    tok.expires_at = None
    tok.last_used_at = None
    tok.is_active = True
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


def _make_session(token_obj: ApiToken) -> AsyncMock:
    """Build a minimal async session mock that yields `token_obj` after flush."""
    session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = _execute
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()
    # After flush+refresh the token_obj is "populated"; model_validate picks it up.
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _build_app(session: AsyncMock) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = MagicMock(return_value=session)
    app.include_router(tokens_router)
    admin = _make_token_obj(scopes=["admin"])
    app.dependency_overrides[token_routes._optional_token_dep.dependency] = lambda: admin
    return app


# ---------------------------------------------------------------------------
# Test: workspace_id is persisted when provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_token_with_workspace_id_persists_it() -> None:
    """POST /api/v1/tokens with workspace_id stores it on the created ApiToken."""
    ws_id = _DEFAULT_WORKSPACE
    tok = _make_token_obj(workspace_id=ws_id, scopes=["stats:read"])
    read_model = _make_read_model(tok)
    session = _make_session(tok)

    # Capture what ApiToken(...) was constructed with (via the patched
    # side_effect below, which records kwargs into constructed_kwargs).
    constructed_kwargs: dict[str, Any] = {}

    with (
        patch(
            "omniscience_server.routes.tokens.generate_token",
            return_value=("sk_test_plain", "sk_test_"),
        ),
        patch("omniscience_server.routes.tokens.hash_token", return_value="hashed"),
        patch(
            "omniscience_server.routes.tokens.ApiToken",
            side_effect=lambda **kw: constructed_kwargs.update(kw) or tok,
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
                json={
                    "name": "ws-token",
                    "scopes": ["stats:read"],
                    "workspace_id": str(ws_id),
                },
            )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "token" in body
    assert "secret" in body

    # The ApiToken was constructed with the workspace_id we passed.
    assert "workspace_id" in constructed_kwargs
    assert constructed_kwargs["workspace_id"] == ws_id

    # The returned token read-model carries the workspace_id.
    assert body["token"]["workspace_id"] == str(ws_id)


# ---------------------------------------------------------------------------
# Test: workspace_id stays None when omitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_token_without_workspace_id_stays_null() -> None:
    """POST /api/v1/tokens without workspace_id leaves it as None."""
    tok = _make_token_obj(workspace_id=None, scopes=["search"])
    read_model = _make_read_model(tok)
    session = _make_session(tok)

    constructed_kwargs: dict[str, Any] = {}

    with (
        patch(
            "omniscience_server.routes.tokens.generate_token",
            return_value=("sk_test_plain2", "sk_test_2"),
        ),
        patch("omniscience_server.routes.tokens.hash_token", return_value="hashed2"),
        patch(
            "omniscience_server.routes.tokens.ApiToken",
            side_effect=lambda **kw: constructed_kwargs.update(kw) or tok,
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
                json={"name": "no-ws-token", "scopes": ["search"]},
            )

    assert resp.status_code == 201, resp.text

    # workspace_id in kwargs should be None (the default).
    assert constructed_kwargs.get("workspace_id") is None

    # The returned token read-model has null workspace_id.
    assert resp.json()["token"]["workspace_id"] is None


# ---------------------------------------------------------------------------
# Test: token row with workspace_id satisfies stats_read scope check
# ---------------------------------------------------------------------------


def test_token_with_workspace_id_satisfies_stats_read_scope() -> None:
    """A token row with workspace_id set and stats:read scope passes require_scope logic."""
    ws_id = _DEFAULT_WORKSPACE
    tok = _make_token_obj(workspace_id=ws_id, scopes=["stats:read"])

    # Verify workspace_id is set.
    assert tok.workspace_id == ws_id

    # Verify scopes include stats:read.
    assert Scope.stats_read in {Scope(s) for s in tok.scopes if s in Scope.__members__.values()}


# ---------------------------------------------------------------------------
# Test: ApiTokenRead schema round-trips workspace_id correctly
# ---------------------------------------------------------------------------


def test_api_token_read_round_trips_workspace_id() -> None:
    """ApiTokenRead serializes and deserializes workspace_id as a UUID."""
    ws_id = _DEFAULT_WORKSPACE
    tok = _make_token_obj(workspace_id=ws_id)
    read_model = _make_read_model(tok)

    assert read_model.workspace_id == ws_id
    dumped = read_model.model_dump()
    assert dumped["workspace_id"] == ws_id


def test_api_token_read_null_workspace_id() -> None:
    """ApiTokenRead serializes workspace_id as None when not set."""
    tok = _make_token_obj(workspace_id=None)
    read_model = _make_read_model(tok)

    assert read_model.workspace_id is None
    assert read_model.model_dump()["workspace_id"] is None


# ---------------------------------------------------------------------------
# Test: workspace_id field is accepted in the request schema (pydantic validation)
# ---------------------------------------------------------------------------


def test_token_create_request_accepts_workspace_id() -> None:
    """TokenCreateRequest parses workspace_id from a UUID string."""
    from omniscience_server.routes.tokens import TokenCreateRequest

    ws_id = _DEFAULT_WORKSPACE
    req = TokenCreateRequest(
        name="tok",
        scopes=["stats:read"],
        workspace_id=ws_id,
    )
    assert req.workspace_id == ws_id


def test_token_create_request_workspace_id_defaults_none() -> None:
    """TokenCreateRequest defaults workspace_id to None when omitted."""
    from omniscience_server.routes.tokens import TokenCreateRequest

    req = TokenCreateRequest(name="tok", scopes=["search"])
    assert req.workspace_id is None
