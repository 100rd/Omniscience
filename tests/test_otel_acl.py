"""ACL invariant tests for the OTLP/HTTP trace receiver (#152).

Tenant identity for the OTel receiver is resolved EXCLUSIVELY from the
authenticated bearer token's ``workspace_id``.  Span attributes are
tenant-writable upstream content and MUST NEVER be promoted to workspace
identity.

These tests are the load-bearing security regression for the receiver:
the cross-workspace test verifies that a token for workspace A whose
payload claims ``resource.attributes['tenant.id'] = <workspace B>``
still lands in workspace A.  If anyone refactors the route and starts
reading span attributes for tenant identity, this test will fail loudly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_connectors.otel import CONTENT_TYPE_JSON
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_server.rest.otlp import router as otlp_router

# ---------------------------------------------------------------------------
# Shared payload builder — embeds malicious tenant-claim attributes so the
# cross-workspace assertion can prove they are NEVER honoured.
# ---------------------------------------------------------------------------


_TRACE_ID_A_HEX = "aaaa030405060708090a0b0c0d0e0f10"
_TRACE_ID_B_HEX = "bbbb030405060708090a0b0c0d0e0f10"
_SPAN_HEX = "1112131415161718"


def _build_payload_with_tenant_claim(
    *,
    trace_id_hex: str,
    claimed_workspace_id: uuid.UUID,
) -> bytes:
    """Build an OTLP/JSON body whose resource attributes claim a foreign tenant.

    These attributes are deliberately CRAFTED to look like the most likely
    tenancy fields a malicious or buggy client might use:

      * ``tenant.id``       — common k8s convention
      * ``service.namespace``
      * ``deployment.environment``
      * ``workspace_id``    — the literal column name on ApiToken

    The receiver MUST ignore all of them when assigning the tenant.  The
    only authoritative tenant signal is the bearer token's workspace_id.
    """
    document = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout"}},
                        {"key": "k8s.pod.name", "value": {"stringValue": "checkout-pod"}},
                        # Malicious tenant override claims.
                        {
                            "key": "tenant.id",
                            "value": {"stringValue": str(claimed_workspace_id)},
                        },
                        {
                            "key": "service.namespace",
                            "value": {"stringValue": str(claimed_workspace_id)},
                        },
                        {
                            "key": "deployment.environment",
                            "value": {"stringValue": str(claimed_workspace_id)},
                        },
                        {
                            "key": "workspace_id",
                            "value": {"stringValue": str(claimed_workspace_id)},
                        },
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id_hex,
                                "spanId": _SPAN_HEX,
                                "name": "GET /cart",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000000050000000",
                                "attributes": [],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    return json.dumps(document).encode("utf-8")


# ---------------------------------------------------------------------------
# Recording stub ingester — captures the workspace_id passed in by the route
# so the cross-workspace test can assert on it directly.
# ---------------------------------------------------------------------------


class _RecordingIngester:
    """Records ``ingest`` calls without doing any persistence."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID | None, list[str]]] = []

    async def ingest(self, *, workspace_id: uuid.UUID | None, parsed: Any) -> int:
        trace_ids = sorted(parsed.traces.keys())
        self.calls.append((workspace_id, trace_ids))
        return parsed.total_spans


# ---------------------------------------------------------------------------
# Multi-tenant test app: two tokens for two workspaces, both routed through
# the same db_session_factory mock that returns whichever token's prefix
# matches the request's bearer header.
# ---------------------------------------------------------------------------


def _make_token(
    *,
    plaintext: str,
    prefix: str,
    workspace_id: uuid.UUID | None,
    scopes: list[str],
    is_active: bool = True,
) -> ApiToken:
    hashed = hash_token(plaintext)
    token: ApiToken = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.name = f"token-{workspace_id}"
    token.token_prefix = prefix
    token.hashed_token = hashed
    token.scopes = scopes
    token.workspace_id = workspace_id
    token.expires_at = datetime.now(tz=UTC) + timedelta(hours=1)
    token.last_used_at = None
    token.is_active = is_active
    return token


def _multi_token_session_factory(tokens: list[ApiToken]) -> Any:
    """Build a session-factory mock that routes lookups by token prefix.

    The auth middleware filters by ``token_prefix == prefix`` in SQL; we
    cannot inspect the SQLAlchemy expression in pure mock land, so we
    instead return all tokens and let the middleware's
    ``verify_token`` (real argon2 verification) pick the right one.
    """
    fake_session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = tokens
        return result

    fake_session.execute = _execute
    fake_session.flush = AsyncMock()
    fake_session.commit = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=fake_session)


def _build_app(tokens: list[ApiToken], ingester: _RecordingIngester) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = _multi_token_session_factory(tokens)
    app.state.otlp_ingester = ingester
    app.include_router(otlp_router, prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def two_workspace_setup() -> AsyncIterator[
    tuple[AsyncClient, str, uuid.UUID, str, uuid.UUID, _RecordingIngester]
]:
    """Yield (client, plaintext_A, ws_A, plaintext_B, ws_B, ingester).

    Two tokens for two distinct workspaces; both share the same ASGI
    client so cross-claim assertions are simple to express.
    """
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    plaintext_a, prefix_a = generate_token("development")
    plaintext_b, prefix_b = generate_token("development")

    token_a = _make_token(
        plaintext=plaintext_a,
        prefix=prefix_a,
        workspace_id=ws_a,
        scopes=[Scope.otel_write.value],
    )
    token_b = _make_token(
        plaintext=plaintext_b,
        prefix=prefix_b,
        workspace_id=ws_b,
        scopes=[Scope.otel_write.value],
    )

    ingester = _RecordingIngester()
    app = _build_app([token_a, token_b], ingester)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, plaintext_a, ws_a, plaintext_b, ws_b, ingester


# ---------------------------------------------------------------------------
# 401 / 403 — auth surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_token_rejected_401(two_workspace_setup: Any) -> None:
    """No Authorization header → 401, no spans ingested.

    ACL invariant: an unauthenticated client cannot inject into any
    workspace, regardless of what it claims in span attributes.
    """
    client, _pa, _wa, _pb, _wb, ingester = two_workspace_setup
    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=_build_payload_with_tenant_claim(
            trace_id_hex=_TRACE_ID_A_HEX, claimed_workspace_id=uuid.uuid4()
        ),
        headers={"Content-Type": CONTENT_TYPE_JSON},
    )
    assert response.status_code == 401
    assert ingester.calls == []


@pytest.mark.asyncio
async def test_invalid_token_rejected_401(two_workspace_setup: Any) -> None:
    """Garbage bearer token → 401, no spans ingested."""
    client, _pa, _wa, _pb, _wb, ingester = two_workspace_setup
    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=_build_payload_with_tenant_claim(
            trace_id_hex=_TRACE_ID_A_HEX, claimed_workspace_id=uuid.uuid4()
        ),
        headers={
            "Authorization": "Bearer sk_dev_garbage_does_not_match",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    assert response.status_code == 401
    assert ingester.calls == []


@pytest.mark.asyncio
async def test_token_without_otel_scope_rejected_403() -> None:
    """A token with non-otel scopes → 403, no spans ingested.

    ACL invariant: even an authenticated token cannot push traces unless
    it explicitly carries the ``otel:write`` scope (or admin).
    """
    workspace_id = uuid.uuid4()
    plaintext, prefix = generate_token("development")
    token = _make_token(
        plaintext=plaintext,
        prefix=prefix,
        workspace_id=workspace_id,
        scopes=[Scope.search.value],  # No otel:write, no admin.
    )
    ingester = _RecordingIngester()
    app = _build_app([token], ingester)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/otlp/v1/traces",
            content=_build_payload_with_tenant_claim(
                trace_id_hex=_TRACE_ID_A_HEX, claimed_workspace_id=uuid.uuid4()
            ),
            headers={
                "Authorization": f"Bearer {plaintext}",
                "Content-Type": CONTENT_TYPE_JSON,
            },
        )
    assert response.status_code == 403
    assert ingester.calls == []


@pytest.mark.asyncio
async def test_admin_scope_implies_otel_write() -> None:
    """An admin-scoped token is accepted (admin implies otel:write)."""
    workspace_id = uuid.uuid4()
    plaintext, prefix = generate_token("development")
    token = _make_token(
        plaintext=plaintext,
        prefix=prefix,
        workspace_id=workspace_id,
        scopes=[Scope.admin.value],
    )
    ingester = _RecordingIngester()
    app = _build_app([token], ingester)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/otlp/v1/traces",
            content=_build_payload_with_tenant_claim(
                trace_id_hex=_TRACE_ID_A_HEX, claimed_workspace_id=uuid.uuid4()
            ),
            headers={
                "Authorization": f"Bearer {plaintext}",
                "Content-Type": CONTENT_TYPE_JSON,
            },
        )
    assert response.status_code == 200
    assert ingester.calls == [(workspace_id, [_TRACE_ID_A_HEX])]


# ---------------------------------------------------------------------------
# Cross-workspace ACL invariant — the load-bearing test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_span_attribute_tenant_override_is_ignored(
    two_workspace_setup: Any,
) -> None:
    """Token for workspace A pushes a payload claiming workspace B → spans
    land in A.  The malicious tenant override has NO effect.

    This is THE security regression for #152.  If this test fails, the
    OTel receiver has been compromised: a tenant could inject traces
    into another tenant's graph by setting span attributes.
    """
    client, plaintext_a, ws_a, _plaintext_b, ws_b, ingester = two_workspace_setup

    # Token A pushes a payload that claims workspace B in every plausible
    # tenancy attribute (tenant.id, service.namespace,
    # deployment.environment, workspace_id).
    body = _build_payload_with_tenant_claim(
        trace_id_hex=_TRACE_ID_A_HEX,
        claimed_workspace_id=ws_b,
    )
    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=body,
        headers={
            "Authorization": f"Bearer {plaintext_a}",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    assert response.status_code == 200, response.text

    # The ingester was called with workspace_id == ws_a (token's workspace),
    # NOT ws_b (the malicious claim).  This is the core ACL invariant.
    assert ingester.calls == [(ws_a, [_TRACE_ID_A_HEX])]
    received_workspace_id = ingester.calls[0][0]
    assert received_workspace_id == ws_a
    assert received_workspace_id != ws_b


@pytest.mark.asyncio
async def test_each_workspace_isolated_when_both_make_cross_claims(
    two_workspace_setup: Any,
) -> None:
    """Both tenants post payloads claiming the OTHER workspace.

    Each tenant's spans MUST land under their own token's workspace_id;
    the cross-claims have no effect.  Symmetric proof of isolation.
    """
    client, plaintext_a, ws_a, plaintext_b, ws_b, ingester = two_workspace_setup

    body_a_claims_b = _build_payload_with_tenant_claim(
        trace_id_hex=_TRACE_ID_A_HEX, claimed_workspace_id=ws_b
    )
    body_b_claims_a = _build_payload_with_tenant_claim(
        trace_id_hex=_TRACE_ID_B_HEX, claimed_workspace_id=ws_a
    )

    response_a = await client.post(
        "/api/v1/otlp/v1/traces",
        content=body_a_claims_b,
        headers={
            "Authorization": f"Bearer {plaintext_a}",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    response_b = await client.post(
        "/api/v1/otlp/v1/traces",
        content=body_b_claims_a,
        headers={
            "Authorization": f"Bearer {plaintext_b}",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    assert response_a.status_code == 200
    assert response_b.status_code == 200

    # Each call landed under its own token's workspace, not the cross-claimed one.
    assert ingester.calls == [
        (ws_a, [_TRACE_ID_A_HEX]),
        (ws_b, [_TRACE_ID_B_HEX]),
    ]
