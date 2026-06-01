"""Coverage tests for rest/incidents_admin.py — target ≥ 80%.

Covers:
- GET /api/v1/admin/incidents/scoring — happy path (workspace override + default fallback)
- PUT /api/v1/admin/incidents/scoring — happy path, workspace not found (404)
- 401 (no token), 403 (wrong scope, unscoped token)
- 422 input validation (weights don't sum to 1, out-of-range field, missing)
- 503 when db_session_factory absent
- _default_response weights_source="default"
- _require_workspace raises 403 for global token (workspace_id is None)
- ValueError re-raise path for unknown errors
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_server.app import create_app
from omniscience_server.incidents_scoring import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_WEIGHTS,
    IncidentScoringConfig,
    IncidentScoringWeights,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(
    scopes: list[str], *, workspace_id: uuid.UUID | None = None
) -> tuple[ApiToken, str]:
    """Build a mock ApiToken with optional workspace binding."""
    pt, prefix = generate_token("test")
    hashed = hash_token(pt)

    tok: ApiToken = MagicMock(spec=ApiToken)
    tok.id = uuid.uuid4()
    tok.token_prefix = prefix
    tok.hashed_token = hashed
    tok.scopes = scopes
    tok.expires_at = None
    tok.is_active = True
    tok.last_used_at = None
    tok.workspace_id = workspace_id
    return tok, pt


def _make_session(token: ApiToken) -> AsyncMock:
    """Build a fake async DB session that returns *token* on auth lookups."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [token]
    result.scalars.return_value.first.return_value = token
    result.scalar_one.return_value = 1
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_app(token: ApiToken) -> FastAPI:
    """Return a FastAPI test app wired with the given token for auth."""
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    session = _make_session(token)
    app.state.db_session_factory = MagicMock(return_value=session)
    return app


async def _client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


_VALID_WEIGHTS: dict[str, Any] = {
    "recency": 0.25,
    "graph_proximity": 0.25,
    "evidence_count": 0.25,
    "cross_ref_strength": 0.25,
}

_SCORING_URL = "/api/v1/admin/incidents/scoring"


# ---------------------------------------------------------------------------
# Auth: 401 without token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_401_no_token() -> None:
    wid = uuid.uuid4()
    tok, _ = _make_token(["stats:read"], workspace_id=wid)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_SCORING_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_scoring_401_no_token() -> None:
    wid = uuid.uuid4()
    tok, _ = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.put(_SCORING_URL, json={"weights": _VALID_WEIGHTS})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth: 403 wrong scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_403_wrong_scope() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["sources:read"], workspace_id=wid)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_SCORING_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_scoring_403_wrong_scope() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=wid)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.put(
            _SCORING_URL,
            json={"weights": _VALID_WEIGHTS, "confidence_threshold": 0.6},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 403: unscoped / global token (workspace_id is None)
# Responses wrapped in {"error": {...}} by the app-level error handler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_403_no_workspace() -> None:
    """Token has correct scope but no workspace_id => _require_workspace raises 403."""
    tok, pt = _make_token(["stats:read"], workspace_id=None)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_SCORING_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_put_scoring_403_no_workspace() -> None:
    tok, pt = _make_token(["incidents:write"], workspace_id=None)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.put(
            _SCORING_URL,
            json={"weights": _VALID_WEIGHTS, "confidence_threshold": 0.6},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# 503: _get_db_factory raises when factory absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_503_no_db() -> None:
    """When _get_db_factory raises 503 the endpoint propagates it."""
    from fastapi import HTTPException as _HTTPException

    wid = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=wid)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incidents_admin._get_db_factory",
        side_effect=_HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(_SCORING_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "service_unavailable"


@pytest.mark.asyncio
async def test_put_scoring_503_no_db() -> None:
    from fastapi import HTTPException as _HTTPException

    wid = uuid.uuid4()
    tok, pt = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incidents_admin._get_db_factory",
        side_effect=_HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        ),
    ):
        async with await _client(app) as c:
            resp = await c.put(
                _SCORING_URL,
                json={"weights": _VALID_WEIGHTS, "confidence_threshold": 0.6},
                headers={"Authorization": f"Bearer {pt}"},
            )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET happy path: no workspace config => default response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_default_when_no_config() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=wid)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incidents_admin.load_workspace_config",
        new=AsyncMock(return_value=None),
    ):
        async with await _client(app) as c:
            resp = await c.get(_SCORING_URL, headers={"Authorization": f"Bearer {pt}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["weights_source"] == "default"
    assert body["confidence_threshold"] == DEFAULT_CONFIDENCE_THRESHOLD
    assert body["weights"]["recency"] == DEFAULT_WEIGHTS["recency"]


# ---------------------------------------------------------------------------
# GET happy path: workspace has an override config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_workspace_override() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=wid)
    app = _make_app(tok)

    custom_weights = IncidentScoringWeights(
        recency=0.4,
        graph_proximity=0.3,
        evidence_count=0.2,
        cross_ref_strength=0.1,
    )
    mock_config = IncidentScoringConfig(weights=custom_weights, confidence_threshold=0.75)

    with patch(
        "omniscience_server.rest.incidents_admin.load_workspace_config",
        new=AsyncMock(return_value=mock_config),
    ):
        async with await _client(app) as c:
            resp = await c.get(_SCORING_URL, headers={"Authorization": f"Bearer {pt}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["weights_source"] == "workspace"
    assert body["confidence_threshold"] == 0.75
    assert body["weights"]["recency"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# GET: workspace config has weights=None => falls back to DEFAULT_WEIGHTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_config_with_no_weights_uses_defaults() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["stats:read"], workspace_id=wid)
    app = _make_app(tok)

    # Config present but weights=None (only threshold customised)
    mock_config = IncidentScoringConfig(weights=None, confidence_threshold=0.8)

    with patch(
        "omniscience_server.rest.incidents_admin.load_workspace_config",
        new=AsyncMock(return_value=mock_config),
    ):
        async with await _client(app) as c:
            resp = await c.get(_SCORING_URL, headers={"Authorization": f"Bearer {pt}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["weights_source"] == "workspace"
    assert body["confidence_threshold"] == pytest.approx(0.8)
    assert body["weights"]["recency"] == DEFAULT_WEIGHTS["recency"]


# ---------------------------------------------------------------------------
# GET: stats:read scope accepted; admin also works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scoring_admin_scope_accepted() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["admin"], workspace_id=wid)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incidents_admin.load_workspace_config",
        new=AsyncMock(return_value=None),
    ):
        async with await _client(app) as c:
            resp = await c.get(_SCORING_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PUT happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_scoring_success() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incidents_admin.save_workspace_config",
        new=AsyncMock(return_value=None),
    ):
        async with await _client(app) as c:
            resp = await c.put(
                _SCORING_URL,
                json={"weights": _VALID_WEIGHTS, "confidence_threshold": 0.7},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["weights_source"] == "workspace"
    assert body["confidence_threshold"] == pytest.approx(0.7)
    assert body["weights"]["recency"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# PUT: admin scope also accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_scoring_admin_scope() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["admin"], workspace_id=wid)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incidents_admin.save_workspace_config",
        new=AsyncMock(return_value=None),
    ):
        async with await _client(app) as c:
            resp = await c.put(
                _SCORING_URL,
                json={"weights": _VALID_WEIGHTS, "confidence_threshold": 0.5},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PUT: 404 workspace not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_scoring_404_workspace_not_found() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incidents_admin.save_workspace_config",
        new=AsyncMock(side_effect=ValueError(f"workspace_not_found:{wid}")),
    ):
        async with await _client(app) as c:
            resp = await c.put(
                _SCORING_URL,
                json={"weights": _VALID_WEIGHTS, "confidence_threshold": 0.6},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "workspace_not_found"


# ---------------------------------------------------------------------------
# PUT: 422 validation — weights don't sum to 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_scoring_422_weights_not_sum_to_one() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)
    bad_weights = {
        "recency": 0.5,
        "graph_proximity": 0.5,
        "evidence_count": 0.5,
        "cross_ref_strength": 0.5,
    }
    async with await _client(app) as c:
        resp = await c.put(
            _SCORING_URL,
            json={"weights": bad_weights, "confidence_threshold": 0.6},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PUT: 422 validation — weight field out of [0, 1] range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_scoring_422_weight_out_of_range() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)
    bad_weights = {
        "recency": -0.1,
        "graph_proximity": 0.4,
        "evidence_count": 0.4,
        "cross_ref_strength": 0.3,
    }
    async with await _client(app) as c:
        resp = await c.put(
            _SCORING_URL,
            json={"weights": bad_weights, "confidence_threshold": 0.6},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PUT: 422 validation — missing required weights field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_scoring_422_missing_weights() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.put(
            _SCORING_URL,
            json={"confidence_threshold": 0.6},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PUT: ValueError re-raised for unknown errors (results in 500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_scoring_reraises_unknown_value_error() -> None:
    wid = uuid.uuid4()
    tok, pt = _make_token(["incidents:write"], workspace_id=wid)
    app = _make_app(tok)

    # The production code does `raise` for any ValueError not starting with
    # "workspace_not_found:".  FastAPI converts unhandled exceptions to 500.
    with patch(
        "omniscience_server.rest.incidents_admin.save_workspace_config",
        new=AsyncMock(side_effect=ValueError("some_other_error:details")),
    ):
        async with await _client(app) as c:
            with pytest.raises(Exception):
                await c.put(
                    _SCORING_URL,
                    json={"weights": _VALID_WEIGHTS, "confidence_threshold": 0.6},
                    headers={"Authorization": f"Bearer {pt}"},
                )
