"""Coverage tests for rest/postmortem.py — target ≥ 80%.

Covers:
- GET /api/v1/incidents/{incident_id}/postmortem happy paths:
  - markdown response (default format, default template)
  - html response
  - json response
- 401 (no token)
- 403 wrong scope / no workspace
- 400 invalid_template
- 400 invalid_format
- 400 invalid_incident_id
- 400 invalid_window (start > end)
- 400 invalid_timezone (naive datetime in incident_start/incident_end)
- 404 incident_not_found
- ValueError with invalid_alert_id: prefix => 400 invalid_alert_id
- PostmortemError with unknown code => 400 (fallback)
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
from omniscience_server.postmortem import (
    INCIDENT_NOT_FOUND_CODE,
    INVALID_FORMAT_CODE,
    INVALID_INCIDENT_ID_CODE,
    INVALID_TEMPLATE_CODE,
    Citation,
    FollowUp,
    PostmortemError,
    PostmortemReport,
    PostmortemTimelineRow,
    get_template,
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
_INCIDENT_ID = "INC-99"
_ALERT_ID = "alert://pagerduty/INC-99"
_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)

_PM_URL = f"/api/v1/incidents/{_INCIDENT_ID}/postmortem"


def _make_report(template_id: str = "blameless") -> PostmortemReport:
    row = PostmortemTimelineRow(
        ts=_NOW,
        entity_id="pod/api",
        entity_type="k8s_pod",
        change_kind="created",
        before_state_summary=None,
        after_state_summary="API pod restarted",
        citation=Citation(
            entity_id="pod/api",
            entity_type="k8s_pod",
            as_of=_NOW,
            source="src-1",
        ),
    )
    followup = FollowUp(
        name=f"followup://{_INCIDENT_ID.lower()}/schedule-review",
        incident_id=_INCIDENT_ID,
        alert_id=_ALERT_ID,
        title="Schedule post-incident review meeting",
        priority="P3",
        source_entity_id=None,
        source_as_of=None,
        created_at=_NOW,
    )
    return PostmortemReport(
        incident_id=_INCIDENT_ID,
        alert_id=_ALERT_ID,
        template_id=template_id,  # type: ignore[arg-type]
        template_display_name=get_template(template_id).display_name,
        incident_start=_NOW,
        incident_end=_NOW,
        generated_at=_NOW,
        effective_as_of=_NOW,
        timeline=[row],
        runbook_steps_count=0,
        followups=[followup],
    )


# ---------------------------------------------------------------------------
# Auth: 401 without token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_401_no_token() -> None:
    tok, _ = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(_PM_URL, params={"alert_id": _ALERT_ID})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth: 403 wrong scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_403_wrong_scope() -> None:
    tok, pt = _make_token(["sources:read"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _PM_URL,
            params={"alert_id": _ALERT_ID},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 403: no workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_403_no_workspace() -> None:
    tok, pt = _make_token(["search"], workspace_id=None)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _PM_URL,
            params={"alert_id": _ALERT_ID},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# 422: alert_id missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_422_missing_alert_id() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _PM_URL,
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 422: alert_id empty string (min_length=1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_422_empty_alert_id() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _PM_URL,
            params={"alert_id": ""},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 400: invalid_window (start > end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_400_invalid_window() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _PM_URL,
            params={
                "alert_id": _ALERT_ID,
                "incident_start": "2026-04-01T15:00:00Z",
                "incident_end": "2026-04-01T12:00:00Z",
            },
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_window"


# ---------------------------------------------------------------------------
# 400: invalid_timezone (naive datetime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_400_invalid_timezone_start() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _PM_URL,
            params={
                "alert_id": _ALERT_ID,
                "incident_start": "2026-04-01T12:00:00",  # naive — no tz
            },
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_timezone"


@pytest.mark.asyncio
async def test_postmortem_400_invalid_timezone_end() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    async with await _client(app) as c:
        resp = await c.get(
            _PM_URL,
            params={
                "alert_id": _ALERT_ID,
                "incident_end": "2026-04-01T12:00:00",  # naive
            },
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Happy path: markdown (default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_happy_path_markdown() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    report = _make_report()

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(return_value=report),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "Post-mortem" in resp.text


# ---------------------------------------------------------------------------
# Happy path: html format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_happy_path_html() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    report = _make_report()

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(return_value=report),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID, "format": "html"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<html" in resp.text.lower() or "<h1" in resp.text.lower()


# ---------------------------------------------------------------------------
# Happy path: json format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_happy_path_json() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    report = _make_report()

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(return_value=report),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID, "format": "json"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["incident_id"] == _INCIDENT_ID
    assert body["template_id"] == "blameless"


# ---------------------------------------------------------------------------
# Happy path: five_whys template
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_five_whys_template() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    report = _make_report(template_id="five_whys")

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(return_value=report),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID, "template": "five_whys"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 400: invalid_template from PostmortemError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_400_invalid_template() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(
            side_effect=PostmortemError(INVALID_TEMPLATE_CODE, "unknown_tmpl"),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID, "template": "unknown_tmpl"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == INVALID_TEMPLATE_CODE


# ---------------------------------------------------------------------------
# 400: invalid_format from PostmortemError (render path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_400_invalid_format_from_render() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)
    report = _make_report()

    with (
        patch(
            "omniscience_server.rest.postmortem.generate_postmortem",
            new=AsyncMock(return_value=report),
        ),
        patch(
            "omniscience_server.rest.postmortem.render",
            side_effect=PostmortemError(INVALID_FORMAT_CODE, "pdf"),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID, "format": "pdf"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == INVALID_FORMAT_CODE


# ---------------------------------------------------------------------------
# 400: invalid_incident_id from PostmortemError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_400_invalid_incident_id() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(
            side_effect=PostmortemError(INVALID_INCIDENT_ID_CODE, "bad-id"),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == INVALID_INCIDENT_ID_CODE


# ---------------------------------------------------------------------------
# 404: incident_not_found from PostmortemError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_404_incident_not_found() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(
            side_effect=PostmortemError(INCIDENT_NOT_FOUND_CODE, _ALERT_ID),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == INCIDENT_NOT_FOUND_CODE


# ---------------------------------------------------------------------------
# 400: ValueError with invalid_alert_id: prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_400_invalid_alert_id_value_error() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(
            side_effect=ValueError("invalid_alert_id: bad URI format"),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": "not-a-valid-alert-uri"},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_alert_id"


# ---------------------------------------------------------------------------
# 400: PostmortemError with unmapped code => fallback 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postmortem_400_unknown_postmortem_error_code() -> None:
    tok, pt = _make_token(["search"], workspace_id=_WID)
    app = _make_app(tok)

    with patch(
        "omniscience_server.rest.postmortem.generate_postmortem",
        new=AsyncMock(
            side_effect=PostmortemError("some_unknown_code", "details"),
        ),
    ):
        async with await _client(app) as c:
            resp = await c.get(
                _PM_URL,
                params={"alert_id": _ALERT_ID},
                headers={"Authorization": f"Bearer {pt}"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "some_unknown_code"
