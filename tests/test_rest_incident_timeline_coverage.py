"""Coverage tests for rest/incident_timeline.py — target ≥ 80%.

Covers:
- GET /api/v1/incidents/{alert_id}/timeline happy paths:
  - JSON response (default format)
  - mermaid format (text/plain response)
  - with from/to window, entity_types, as_of, max_depth params
- 401 no token
- 403 wrong scope / no workspace
- 400 invalid_window (from > to)
- 400 invalid_timezone on from/to/as_of (naive datetime)
- 400 invalid_format (unknown format value)
- 400 invalid_alert_id ValueError
- 404 alert_not_found ValueError
- 400 generic ValueError fallback
- URL-encoded alert_id path (decoded correctly)
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
from omniscience_server.incidents import ALERT_NOT_FOUND_CODE, INVALID_ALERT_ID_CODE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(
    scopes: list[str], *, workspace_id: uuid.UUID | None = None
) -> tuple[ApiToken, str]:
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
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    app.state.db_session_factory = MagicMock(return_value=_make_session(token))
    return app


async def _client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


_WID = uuid.uuid4()
_ALERT_ID = "alert://pagerduty/INC-42"
_TIMELINE_URL = f"/api/v1/incidents/{_ALERT_ID}/timeline"

_EMPTY_TIMELINE_PAYLOAD: dict[str, Any] = {
    "alert_id": _ALERT_ID,
    "events": [],
    "event_count": 0,
    "as_of": "2026-04-01T12:00:00+00:00",
    "from": None,
    "to": None,
    "max_depth": 2,
    "entity_types": None,
}


# ---------------------------------------------------------------------------
# Auth: 401 no token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_401_no_token() -> None:
    tok, _ = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_TIMELINE_URL)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth: 403 wrong scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_403_wrong_scope() -> None:
    tok, pt = _make_token(["sources:read"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_TIMELINE_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Auth: 403 no workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_403_no_workspace() -> None:
    tok, pt = _make_token(["search"], workspace_id=None)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_TIMELINE_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# 400: invalid_window (from > to)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_400_invalid_window() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _TIMELINE_URL,
            params={"from": "2026-04-01T15:00:00Z", "to": "2026-04-01T12:00:00Z"},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_window"


# ---------------------------------------------------------------------------
# 400: invalid_timezone on "from" (naive datetime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_400_invalid_timezone_from() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _TIMELINE_URL,
            params={"from": "2026-04-01T12:00:00"},  # naive
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_timezone"


# ---------------------------------------------------------------------------
# 400: invalid_timezone on "to" (naive datetime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_400_invalid_timezone_to() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _TIMELINE_URL,
            params={"to": "2026-04-01T12:00:00"},  # naive
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_timezone"


# ---------------------------------------------------------------------------
# 400: invalid_timezone on as_of (naive datetime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_400_invalid_timezone_as_of() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _TIMELINE_URL,
            params={"as_of": "2026-04-01T12:00:00"},  # naive
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_timezone"


# ---------------------------------------------------------------------------
# 400: invalid_format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_400_invalid_format() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _TIMELINE_URL,
            params={"format": "csv"},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_format"


# ---------------------------------------------------------------------------
# 422: max_depth out of [1, 5] range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_422_max_depth_out_of_range() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _TIMELINE_URL,
            params={"max_depth": 10},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Happy path: JSON format (default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_happy_json() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(return_value=_EMPTY_TIMELINE_PAYLOAD),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _TIMELINE_URL,
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["alert_id"] == _ALERT_ID
    assert body["events"] == []


# ---------------------------------------------------------------------------
# Happy path: mermaid format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_happy_mermaid() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(return_value=_EMPTY_TIMELINE_PAYLOAD),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _TIMELINE_URL,
                params={"format": "mermaid"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "timeline" in resp.text.lower()


# ---------------------------------------------------------------------------
# Happy path: explicit json format param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_happy_explicit_json_format() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(return_value=_EMPTY_TIMELINE_PAYLOAD),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _TIMELINE_URL,
                params={"format": "json"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body


# ---------------------------------------------------------------------------
# Happy path: with from/to window and entity_types filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_with_window_and_entity_types() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(return_value=_EMPTY_TIMELINE_PAYLOAD),
    ) as mock_mcp:
        async with await _client(app) as c:
            resp = await c.get(
                _TIMELINE_URL,
                params={
                    "from": "2026-04-01T10:00:00Z",
                    "to": "2026-04-01T14:00:00Z",
                    "entity_types": ["k8s_pod", "pull_request"],
                    "max_depth": 3,
                },
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    mock_mcp.assert_awaited_once()
    call_kwargs = mock_mcp.call_args.kwargs
    assert call_kwargs["entity_types"] == ["k8s_pod", "pull_request"]
    assert call_kwargs["max_depth"] == 3


# ---------------------------------------------------------------------------
# Happy path: with as_of parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_with_as_of() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(return_value=_EMPTY_TIMELINE_PAYLOAD),
    ) as mock_mcp:
        async with await _client(app) as c:
            resp = await c.get(
                _TIMELINE_URL,
                params={"as_of": "2026-04-01T12:00:00Z"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    call_kwargs = mock_mcp.call_args.kwargs
    assert call_kwargs["as_of"] is not None


# ---------------------------------------------------------------------------
# URL-encoded alert_id is decoded correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_url_encoded_alert_id() -> None:
    """alert:// URI with slashes must be decoded before service call."""
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    raw_alert_id = "alert://pagerduty/INC-99"
    encoded = raw_alert_id.replace("://", "%3A%2F%2F").replace("/", "%2F")
    url = f"/api/v1/incidents/{encoded}/timeline"

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(return_value=_EMPTY_TIMELINE_PAYLOAD),
    ) as mock_mcp:
        async with await _client(app) as c:
            resp = await c.get(url, headers={"Authorization": f"Bearer {pt}"})

    assert resp.status_code == 200
    decoded = mock_mcp.call_args.kwargs["alert_id"]
    assert "%" not in decoded  # URL encoding stripped
    assert "alert://" in decoded


# ---------------------------------------------------------------------------
# 400: ValueError with invalid_alert_id prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_400_invalid_alert_id() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(
            side_effect=ValueError(f"{INVALID_ALERT_ID_CODE}: invalid URI format"),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                "/api/v1/incidents/not-a-valid-uri/timeline",
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == INVALID_ALERT_ID_CODE


# ---------------------------------------------------------------------------
# 404: alert_not_found ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_404_alert_not_found() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(
            side_effect=ValueError(f"{ALERT_NOT_FOUND_CODE}: alert not visible"),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _TIMELINE_URL,
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == ALERT_NOT_FOUND_CODE


# ---------------------------------------------------------------------------
# 400: generic ValueError fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_400_generic_value_error() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.incident_timeline.mcp_incident_timeline",
        new=AsyncMock(side_effect=ValueError("something unexpected")),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _TIMELINE_URL,
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "bad_request"
