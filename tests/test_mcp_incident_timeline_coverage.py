"""Coverage tests for omniscience_server.mcp.incident_timeline (issue #235).

Covers previously-untested paths in incident_timeline.py:
- Happy path: workspace-scoped token, delegates to mcp_incident_timeline
- Unauthorized: missing token -> 'forbidden' from require_scope
- Forbidden scope: token with wrong scope
- No workspace: token without workspace_id -> 'forbidden:Incident timeline...'
- from_ts / to_ts / entity_types / as_of parameter plumbing
- max_depth clamping: above max (>5) clamps to 5, below min (<1) clamps to 1
- record_invocation is called with correct tool_name
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from omniscience_core.auth.scopes import Scope
from omniscience_core.db.models import ApiToken
from omniscience_server.mcp.incident_timeline import incident_timeline_tool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_ID = uuid.UUID("cccccccc-2222-3333-4444-555500000235")
_NOW = datetime(2026, 4, 17, 10, 0, 0, tzinfo=UTC)
_ALERT_ID = "alert://pagerduty/INC-235"


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_token(
    scopes: list[str] | None = None,
    workspace_id: uuid.UUID | None = _WS_ID,
) -> MagicMock:
    """Build an ApiToken mock."""
    token: MagicMock = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.scopes = scopes if scopes is not None else ["search"]
    token.workspace_id = workspace_id
    token.token_prefix = "sk_t"
    token.is_active = True
    token.expires_at = None
    return token


def _make_app() -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = MagicMock()
    return app


# ---------------------------------------------------------------------------
# Shared auth helper implementations (injected as parameters)
# ---------------------------------------------------------------------------


def _require_scope_impl(token: Any, scope: Scope) -> None:
    """Minimal _require_scope mirroring the production implementation."""
    if token is None:
        raise ValueError("unauthorized:Token missing or invalid")
    granted = {Scope(s) for s in token.scopes if s in Scope.__members__.values()}
    from omniscience_core.auth.scopes import check_scopes

    if not check_scopes({scope}, granted):
        raise ValueError(f"forbidden:Token lacks required scope '{scope}'")


def _parse_as_of_impl(raw: str | None) -> datetime | None:
    """Minimal _parse_as_of mirroring the production implementation."""
    from omniscience_server.mcp.server import _parse_as_of

    return _parse_as_of(raw)


# ---------------------------------------------------------------------------
# Test: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_happy_path() -> None:
    """incident_timeline_tool returns mcp_incident_timeline result on success."""
    token = _make_token()
    app = _make_app()
    ctx = MagicMock()

    expected: dict[str, Any] = {
        "alert_id": _ALERT_ID,
        "events": [],
        "event_count": 0,
    }

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()

    with patch(
        "omniscience_server.mcp.incident_timeline.mcp_incident_timeline",
        new_callable=AsyncMock,
        return_value=expected,
    ):
        result = await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=2,
        )

    assert result == expected
    record_invocation.assert_called_once_with(tool_name="incident_timeline", token=token)


# ---------------------------------------------------------------------------
# Test: unauthorized (token is None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_unauthorized() -> None:
    """incident_timeline_tool raises ValueError('unauthorized') when token is None."""
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=None)
    record_invocation = MagicMock()

    with pytest.raises(ValueError, match="unauthorized"):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=2,
        )


# ---------------------------------------------------------------------------
# Test: forbidden scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_forbidden_scope() -> None:
    """incident_timeline_tool raises ValueError('forbidden') for wrong scope."""
    token = _make_token(scopes=["sources:read"])  # no 'search'
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()

    with pytest.raises(ValueError, match="forbidden"):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=2,
        )


# ---------------------------------------------------------------------------
# Test: no workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_no_workspace_raises() -> None:
    """incident_timeline_tool raises 'forbidden' for token without workspace_id."""
    token = _make_token(workspace_id=None)
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()

    with pytest.raises(ValueError, match="forbidden"):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=2,
        )


# ---------------------------------------------------------------------------
# Test: max_depth clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_max_depth_above_max_clamped() -> None:
    """max_depth > 5 is clamped to 5 before calling mcp_incident_timeline."""
    token = _make_token()
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()
    captured_depth: list[int] = []

    async def capture_call(**kwargs: Any) -> dict[str, Any]:
        captured_depth.append(kwargs["max_depth"])
        return {"events": [], "event_count": 0}

    with patch(
        "omniscience_server.mcp.incident_timeline.mcp_incident_timeline",
        new=capture_call,
    ):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=999,
        )

    assert captured_depth == [5]


@pytest.mark.asyncio
async def test_incident_timeline_tool_max_depth_below_min_clamped() -> None:
    """max_depth < 1 is clamped to 1 before calling mcp_incident_timeline."""
    token = _make_token()
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()
    captured_depth: list[int] = []

    async def capture_call(**kwargs: Any) -> dict[str, Any]:
        captured_depth.append(kwargs["max_depth"])
        return {"events": [], "event_count": 0}

    with patch(
        "omniscience_server.mcp.incident_timeline.mcp_incident_timeline",
        new=capture_call,
    ):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=0,
        )

    assert captured_depth == [1]


@pytest.mark.asyncio
async def test_incident_timeline_tool_max_depth_within_range_unchanged() -> None:
    """max_depth within [1, 5] is passed unchanged."""
    token = _make_token()
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()
    captured_depth: list[int] = []

    async def capture_call(**kwargs: Any) -> dict[str, Any]:
        captured_depth.append(kwargs["max_depth"])
        return {"events": [], "event_count": 0}

    with patch(
        "omniscience_server.mcp.incident_timeline.mcp_incident_timeline",
        new=capture_call,
    ):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=3,
        )

    assert captured_depth == [3]


# ---------------------------------------------------------------------------
# Test: from_ts, to_ts, entity_types, as_of plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_passes_all_params() -> None:
    """Plumb from_ts, to_ts, entity_types, as_of through to mcp_incident_timeline."""
    token = _make_token()
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()
    captured_kwargs: dict[str, Any] = {}

    async def capture_call(**kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {"events": [], "event_count": 0}

    with patch(
        "omniscience_server.mcp.incident_timeline.mcp_incident_timeline",
        new=capture_call,
    ):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts="2026-04-12T08:00:00Z",
            to_ts="2026-04-12T20:00:00Z",
            entity_types=["service", "deployment"],
            as_of="2026-04-12T10:00:00Z",
            max_depth=2,
        )

    assert captured_kwargs["from_ts"] == datetime(2026, 4, 12, 8, 0, 0, tzinfo=UTC)
    assert captured_kwargs["to_ts"] == datetime(2026, 4, 12, 20, 0, 0, tzinfo=UTC)
    assert captured_kwargs["as_of"] == datetime(2026, 4, 12, 10, 0, 0, tzinfo=UTC)
    assert captured_kwargs["entity_types"] == ["service", "deployment"]
    assert captured_kwargs["workspace_id"] == _WS_ID
    assert captured_kwargs["alert_id"] == _ALERT_ID


# ---------------------------------------------------------------------------
# Test: workspace_id from token is forwarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_workspace_id_from_token() -> None:
    """incident_timeline_tool uses workspace_id from the resolved token."""
    custom_ws = uuid.UUID("dddddddd-3333-4444-5555-666600000235")
    token = _make_token(workspace_id=custom_ws)
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()
    captured_ws: list[uuid.UUID] = []

    async def capture_call(**kwargs: Any) -> dict[str, Any]:
        captured_ws.append(kwargs["workspace_id"])
        return {"events": []}

    with patch(
        "omniscience_server.mcp.incident_timeline.mcp_incident_timeline",
        new=capture_call,
    ):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=1,
        )

    assert captured_ws == [custom_ws]


# ---------------------------------------------------------------------------
# Test: null from_ts / to_ts / as_of passes None to backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_none_timestamps_pass_none() -> None:
    """None from_ts, to_ts, and as_of result in None passed to mcp_incident_timeline."""
    token = _make_token()
    app = _make_app()
    ctx = MagicMock()

    resolve_token = AsyncMock(return_value=token)
    record_invocation = MagicMock()
    captured: dict[str, Any] = {}

    async def capture_call(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"events": []}

    with patch(
        "omniscience_server.mcp.incident_timeline.mcp_incident_timeline",
        new=capture_call,
    ):
        await incident_timeline_tool(
            app=app,
            ctx=ctx,
            resolve_token=resolve_token,
            require_scope=_require_scope_impl,
            parse_as_of=_parse_as_of_impl,
            record_invocation=record_invocation,
            alert_id=_ALERT_ID,
            from_ts=None,
            to_ts=None,
            entity_types=None,
            as_of=None,
            max_depth=2,
        )

    assert captured["from_ts"] is None
    assert captured["to_ts"] is None
    assert captured["as_of"] is None
    assert captured["entity_types"] is None
