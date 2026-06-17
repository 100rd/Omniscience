"""Tests for the Terraform drift REST endpoint (Gap C, thin exposure stub).

POST /api/v1/drift/terraform — covers:
- 401 when no bearer token,
- 403 when the token lacks the 'search' scope,
- 200 with the engine's drift report for an authenticated, scoped caller,
  including a config-not-in-state case end-to-end through the real parsers.

The drift classification itself is unit-tested in ``tests/test_tf_drift.py``;
this file checks the auth gate and the parse -> engine -> JSON wiring.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_server.app import create_app

_WS = uuid.UUID("aaaaaaaa-1111-2222-3333-4444aaaa0099")


def _wire_session_factory(app: Any) -> None:
    """Attach a no-op AsyncSession factory so the auth middleware can run.

    The middleware reads ``app.state.db_session_factory`` to look up the
    bearer token; when ``None`` it 401s before the handler.  ``_lookup_token``
    is patched in the scoped tests, so the session itself is never used — but
    the factory must be present and callable.
    """
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = factory


def _make_rest_app() -> Any:
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    app = create_app(settings=settings)
    _wire_session_factory(app)
    return app


def _auth_token(scopes: list[str]) -> tuple[MagicMock, str]:
    plaintext, prefix = generate_token("drift")
    token: MagicMock = MagicMock(spec=ApiToken)
    token.hashed_token = hash_token(plaintext)
    token.token_prefix = prefix
    token.scopes = scopes
    token.expires_at = None
    token.is_active = True
    token.workspace_id = _WS
    return token, plaintext


_S3_CONFIG = 'resource "aws_sqs_queue" "jobs" {\n  name = "jobs"\n}\n'


@pytest.mark.asyncio
async def test_drift_requires_auth() -> None:
    app = _make_rest_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/drift/terraform", json={"config_documents": [_S3_CONFIG]}
        )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_drift_forbidden_without_search_scope() -> None:
    app = _make_rest_app()
    token, plaintext = _auth_token(scopes=["sources:read"])
    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=token,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/drift/terraform",
                json={"config_documents": [_S3_CONFIG]},
                headers={"Authorization": f"Bearer {plaintext}"},
            )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_drift_config_not_in_state() -> None:
    app = _make_rest_app()
    token, plaintext = _auth_token(scopes=["search"])
    with patch(
        "omniscience_core.auth.middleware._lookup_token",
        new_callable=AsyncMock,
        return_value=token,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/drift/terraform",
                json={"config_documents": [_S3_CONFIG]},  # config, no state
                headers={"Authorization": f"Bearer {plaintext}"},
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_drift"] is True
    assert body["config_not_in_state"] == ["resource.aws_sqs_queue.jobs"]
    assert body["state_not_in_config"] == []
