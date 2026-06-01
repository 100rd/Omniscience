"""Coverage tests for rest/replay.py — target ≥ 80%.

Covers:
POST /api/v1/replay:
- 401 no token
- 403 wrong scope / no workspace
- 400 both audit_log_id AND at_time/query supplied
- 400 neither audit_log_id nor at_time+query supplied
- 400 only at_time supplied (query missing)
- 422 invalid at_time (naive datetime → ReplayRequestBody validator)
- 422 unknown tool_name in ReplayQueryBody
- 422 extra fields (extra="forbid")
- happy path: audit_log_id replay
- happy path: inline replay (at_time + query)
- 404 AuditLogNotFoundError
- 400 ValueError unknown_replay_tool
- 422 ValueError invalid_audit_arguments
- 400 ValueError invalid_timezone / invalid_at_time
- 400 generic ValueError fallback
- 503 db_session_factory absent

GET /api/v1/replay/audit/{audit_log_id}:
- 401 no token
- 403 wrong scope / no workspace
- happy path
- 404 AuditLogNotFoundError
- 503 db_session_factory absent
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_server.app import create_app
from omniscience_server.as_of import INVALID_TIMEZONE_CODE
from omniscience_server.replay import (
    INVALID_AT_TIME_CODE,
    INVALID_AUDIT_ARGS_CODE,
    SUPPORTED_REPLAY_TOOLS,
    UNKNOWN_TOOL_CODE,
    AuditLogNotFoundError,
    ReplayResult,
)

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
_AUDIT_ID = uuid.uuid4()
_AT_TIME = "2026-04-01T12:00:00Z"
_TOOL = SUPPORTED_REPLAY_TOOLS[0]  # "search"

_POST_URL = "/api/v1/replay"
_GET_URL = f"/api/v1/replay/audit/{_AUDIT_ID}"


def _make_replay_result() -> ReplayResult:
    return ReplayResult(
        response={"results": []},
        state_fingerprint="abc123",
        fingerprint_algorithm="sha256",
        original_state_fingerprint=None,
        fingerprint_match=None,
        at_time=datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC),
        tool_name=_TOOL,
        audit_log_id=None,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/replay — 401 no token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_401_no_token() -> None:
    tok, _ = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(_POST_URL, json={"audit_log_id": str(_AUDIT_ID)})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST — 403 wrong scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_403_wrong_scope() -> None:
    tok, pt = _make_token(["sources:read"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={"audit_log_id": str(_AUDIT_ID)},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST — 403 no workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_403_no_workspace() -> None:
    tok, pt = _make_token(["search"], workspace_id=None)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={"audit_log_id": str(_AUDIT_ID)},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# POST — 503 no db factory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_503_no_db() -> None:
    from fastapi import HTTPException as _HTTPException

    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay._session_factory_or_503",
        side_effect=_HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "DB unavailable"},
        ),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={"audit_log_id": str(_AUDIT_ID)},
                headers={"Authorization": f"Bearer {pt}"},
            )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST — 400 both audit_log_id AND at_time supplied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_400_both_audit_and_inline() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={
                "audit_log_id": str(_AUDIT_ID),
                "at_time": _AT_TIME,
                "query": {"tool_name": _TOOL, "arguments": {}},
            },
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------
# POST — 400 neither audit_log_id nor at_time+query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_400_nothing_supplied() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------
# POST — 400 only at_time (query missing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_400_at_time_without_query() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={"at_time": _AT_TIME},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST — 422 invalid at_time (naive datetime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_422_naive_at_time() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={
                "at_time": "2026-04-01T12:00:00",  # naive
                "query": {"tool_name": _TOOL, "arguments": {}},
            },
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST — 422 unknown tool_name in query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_422_unknown_tool_name() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={
                "at_time": _AT_TIME,
                "query": {"tool_name": "definitely_not_a_tool", "arguments": {}},
            },
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST — 422 extra field in body (extra="forbid")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_422_extra_field() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.post(
            _POST_URL,
            json={"audit_log_id": str(_AUDIT_ID), "extra_field": "bad"},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST — happy path: replay by audit_log_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_happy_audit_id() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    result = _make_replay_result()

    with patch(
        "omniscience_server.rest.replay.replay_by_audit_id",
        new=AsyncMock(return_value=result),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={"audit_log_id": str(_AUDIT_ID)},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "state_fingerprint" in body
    assert body["tool_name"] == _TOOL


# ---------------------------------------------------------------------------
# POST — happy path: inline replay (at_time + query)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_happy_inline() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    result = _make_replay_result()

    with patch(
        "omniscience_server.rest.replay.replay_context",
        new=AsyncMock(return_value=result),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={
                    "at_time": _AT_TIME,
                    "query": {"tool_name": _TOOL, "arguments": {"query": "outage"}},
                },
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_name"] == _TOOL


# ---------------------------------------------------------------------------
# POST — 404 AuditLogNotFoundError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_404_audit_not_found() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay.replay_by_audit_id",
        new=AsyncMock(side_effect=AuditLogNotFoundError(str(_AUDIT_ID))),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={"audit_log_id": str(_AUDIT_ID)},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "audit_log_not_found"


# ---------------------------------------------------------------------------
# POST — 400 ValueError unknown_replay_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_400_unknown_tool_value_error() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay.replay_context",
        new=AsyncMock(side_effect=ValueError(f"{UNKNOWN_TOOL_CODE}:bad_tool")),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={
                    "at_time": _AT_TIME,
                    "query": {"tool_name": _TOOL, "arguments": {}},
                },
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == UNKNOWN_TOOL_CODE


# ---------------------------------------------------------------------------
# POST — 422 ValueError invalid_audit_arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_422_invalid_audit_args() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay.replay_by_audit_id",
        new=AsyncMock(side_effect=ValueError(f"{INVALID_AUDIT_ARGS_CODE}:bad json")),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={"audit_log_id": str(_AUDIT_ID)},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == INVALID_AUDIT_ARGS_CODE


# ---------------------------------------------------------------------------
# POST — 400 ValueError invalid_at_time prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_400_invalid_at_time_value_error() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay.replay_context",
        new=AsyncMock(side_effect=ValueError(f"{INVALID_AT_TIME_CODE}:parse error")),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={
                    "at_time": _AT_TIME,
                    "query": {"tool_name": _TOOL, "arguments": {}},
                },
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == INVALID_TIMEZONE_CODE


# ---------------------------------------------------------------------------
# POST — 400 ValueError with invalid_timezone code (exact string match)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_400_invalid_timezone_code_exact() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay.replay_context",
        new=AsyncMock(side_effect=ValueError(INVALID_TIMEZONE_CODE)),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={
                    "at_time": _AT_TIME,
                    "query": {"tool_name": _TOOL, "arguments": {}},
                },
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == INVALID_TIMEZONE_CODE


# ---------------------------------------------------------------------------
# POST — 400 generic ValueError fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_replay_400_generic_value_error() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay.replay_context",
        new=AsyncMock(side_effect=ValueError("something went wrong")),
    ):
        async with await _client(app) as c:
            resp = await c.post(
                _POST_URL,
                json={
                    "at_time": _AT_TIME,
                    "query": {"tool_name": _TOOL, "arguments": {}},
                },
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------
# GET /api/v1/replay/audit/{audit_log_id} — 401 no token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_replay_by_audit_id_401_no_token() -> None:
    tok, _ = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_GET_URL)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET — 403 wrong scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_replay_by_audit_id_403_wrong_scope() -> None:
    tok, pt = _make_token(["sources:read"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_GET_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET — 403 no workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_replay_by_audit_id_403_no_workspace() -> None:
    tok, pt = _make_token(["search"], workspace_id=None)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_GET_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# GET — 503 no db
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_replay_by_audit_id_503_no_db() -> None:
    from fastapi import HTTPException as _HTTPException

    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay._session_factory_or_503",
        side_effect=_HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "DB unavailable"},
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(_GET_URL, headers={"Authorization": f"Bearer {pt}"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_replay_by_audit_id_happy() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    result = _make_replay_result()

    with patch(
        "omniscience_server.rest.replay.replay_by_audit_id",
        new=AsyncMock(return_value=result),
    ):
        async with await _client(app) as c:
            resp = await c.get(_GET_URL, headers={"Authorization": f"Bearer {pt}"})

    assert resp.status_code == 200
    body = resp.json()
    assert "state_fingerprint" in body


# ---------------------------------------------------------------------------
# GET — 404 AuditLogNotFoundError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_replay_by_audit_id_404() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.replay.replay_by_audit_id",
        new=AsyncMock(side_effect=AuditLogNotFoundError(str(_AUDIT_ID))),
    ):
        async with await _client(app) as c:
            resp = await c.get(_GET_URL, headers={"Authorization": f"Bearer {pt}"})

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "audit_log_not_found"
