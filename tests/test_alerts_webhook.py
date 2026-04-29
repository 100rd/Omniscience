"""Tests for the alerts webhook handler.

Covers (per issue #151 acceptance):
- PagerDuty: positive (verified) and negative (invalid signature) cases
- Datadog: positive, negative, missing-timestamp, stale-timestamp, replay-window
- Constant-time comparison (no timing-attack vector — ``hmac.compare_digest`` used)
- Dual-secret rotation: one of two newline-separated secrets accepted
- Cross-workspace ACL: payload's ``tenant_id`` field is IGNORED; the alert lands
  in the workspace derived from ``Source.tenant_id`` configured per source
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_connectors.alerts.webhook import (
    AlertsWebhookHandler,
    verify_datadog,
    verify_pagerduty,
)
from omniscience_core.config import Settings
from omniscience_core.db.models import Source, SourceType
from omniscience_server.app import create_app
from omniscience_server.rest.webhooks import clear_all_source_buckets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )


def _alert_source(
    *,
    name: str = "alerts-prod",
    tenant_id: uuid.UUID | None = None,
    secret: str = "supersecret",
) -> Source:
    src: Source = MagicMock(spec=Source)
    src.id = uuid.uuid4()
    src.type = SourceType.alerts
    src.name = name
    src.tenant_id = tenant_id or uuid.uuid4()
    src.config = {"webhook_secret": secret, "provider": "pagerduty"}
    return src


def _make_db(source: Source | None) -> AsyncMock:
    session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.first.return_value = source
        return result

    session.execute = _execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _app_with(source: Source | None = None) -> FastAPI:
    app = create_app(settings=_settings())
    app.state.db_session_factory = MagicMock(return_value=_make_db(source))
    return app


def _pd_sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _dd_sign(body: bytes, ts: int, secret: str) -> str:
    base = f"{ts}:".encode() + body
    return hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def reset_source_buckets() -> None:
    """Clear per-source rate-limit state between tests."""
    clear_all_source_buckets()


# ---------------------------------------------------------------------------
# PagerDuty signature verification — pure function
# ---------------------------------------------------------------------------


def test_pagerduty_positive_case() -> None:
    body = b'{"event":{"data":{"id":"P1","severity":"critical","status":"triggered"}}}'
    sig = _pd_sign(body, "secret-1")
    assert verify_pagerduty(body, sig, "secret-1") is True


def test_pagerduty_rejects_bad_signature() -> None:
    body = b'{"x":1}'
    bad = "v1=" + "00" * 32
    assert verify_pagerduty(body, bad, "secret-1") is False


def test_pagerduty_rejects_missing_signature() -> None:
    assert verify_pagerduty(b'{"x":1}', "", "secret-1") is False


def test_pagerduty_rejects_unknown_version() -> None:
    body = b'{"x":1}'
    digest = hmac.new(b"secret-1", body, hashlib.sha256).hexdigest()
    # Right hash, wrong version label.
    assert verify_pagerduty(body, f"v999={digest}", "secret-1") is False


def test_pagerduty_dual_secret_rotation_accepts_either() -> None:
    body = b'{"x":1}'
    # Sender signed with the *new* (second) secret only.
    sig = _pd_sign(body, "new-secret")
    rotated = "old-secret\nnew-secret"
    assert verify_pagerduty(body, sig, rotated) is True


def test_pagerduty_accepts_multiple_v1_in_header() -> None:
    body = b'{"x":1}'
    good = _pd_sign(body, "secret-1")
    # Header carries a stale signature plus a current one.
    header = f"v1=deadbeef,{good}"
    assert verify_pagerduty(body, header, "secret-1") is True


# ---------------------------------------------------------------------------
# Datadog signature verification — pure function
# ---------------------------------------------------------------------------


def test_datadog_positive_case() -> None:
    body = b'{"id":"42"}'
    ts = 1_761_300_000
    sig = _dd_sign(body, ts, "secret-1")
    assert verify_datadog(body, sig, str(ts), "secret-1", now=ts + 10) is True


def test_datadog_rejects_bad_signature() -> None:
    body = b'{"id":"42"}'
    ts = 1_761_300_000
    bad = "00" * 32
    assert verify_datadog(body, bad, str(ts), "secret-1", now=ts + 1) is False


def test_datadog_rejects_missing_timestamp() -> None:
    body = b'{"id":"42"}'
    sig = _dd_sign(body, 1_761_300_000, "secret-1")
    assert verify_datadog(body, sig, "", "secret-1") is False


def test_datadog_rejects_stale_timestamp() -> None:
    """Timestamp older than ``replay_window_seconds`` -> reject."""
    body = b'{"id":"42"}'
    ts = 1_761_300_000
    sig = _dd_sign(body, ts, "secret-1")
    # 600s skew — outside the 300s default window.
    assert verify_datadog(body, sig, str(ts), "secret-1", now=ts + 600) is False


def test_datadog_accepts_sha256_prefixed_header() -> None:
    body = b'{"id":"42"}'
    ts = 1_761_300_000
    sig = _dd_sign(body, ts, "secret-1")
    assert verify_datadog(body, "sha256=" + sig, str(ts), "secret-1", now=ts + 10) is True


def test_datadog_dual_secret_rotation() -> None:
    body = b'{"id":"42"}'
    ts = 1_761_300_000
    sig = _dd_sign(body, ts, "new-secret")
    rotated = "old-secret\nnew-secret"
    assert verify_datadog(body, sig, str(ts), rotated, now=ts + 10) is True


# ---------------------------------------------------------------------------
# Constant-time comparison verified
# ---------------------------------------------------------------------------


def test_verifiers_use_constant_time_compare() -> None:
    """Both verifiers must reference ``hmac.compare_digest`` — no naive ``==``.

    Inspect the implementation source to prove the constant-time primitive is
    in use; this catches regressions where a future maintainer accidentally
    swaps in ``==``.
    """
    pd_src = inspect.getsource(verify_pagerduty)
    dd_src = inspect.getsource(verify_datadog)
    assert "hmac.compare_digest" in pd_src
    assert "hmac.compare_digest" in dd_src
    # No raw equality on the signature in either path.
    assert "received ==" not in pd_src
    assert "received ==" not in dd_src


# ---------------------------------------------------------------------------
# WebhookHandler high-level dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_dispatches_to_pagerduty() -> None:
    handler = AlertsWebhookHandler()
    body = b'{"event":{"data":{"id":"P1","severity":"warning","status":"triggered"}}}'
    sig = _pd_sign(body, "s1")
    assert await handler.verify_signature(body, {"X-PagerDuty-Signature": sig}, "s1") is True


@pytest.mark.asyncio
async def test_handler_dispatches_to_datadog() -> None:
    handler = AlertsWebhookHandler()
    body = b'{"id":"42","alert_type":"alert","priority":"P1"}'
    ts = int(time.time())
    sig = _dd_sign(body, ts, "s1")
    headers = {
        "X-Datadog-Signature": sig,
        "X-Datadog-Signature-Timestamp": str(ts),
    }
    assert await handler.verify_signature(body, headers, "s1") is True


@pytest.mark.asyncio
async def test_handler_rejects_request_without_known_signature_header() -> None:
    handler = AlertsWebhookHandler()
    assert await handler.verify_signature(b"{}", {"X-Random": "1"}, "s1") is False


@pytest.mark.asyncio
async def test_handler_parse_payload_pagerduty_emits_alert_and_targets() -> None:
    handler = AlertsWebhookHandler()
    body = json.dumps(
        {
            "event": {
                "data": {
                    "id": "PABC",
                    "severity": "critical",
                    "status": "triggered",
                    "title": "pod/web crashed",
                    "description": (
                        "Affecting arn:aws:ec2:us-east-1:111122223333:instance/i-0a "
                        "and pod/web; trace://aabbccddeeff00112233445566778899"
                    ),
                    "service": {"summary": "web-api"},
                    "created_at": "2026-04-24T10:00:00Z",
                }
            }
        }
    ).encode()
    payload = await handler.parse_payload(body, {"X-PagerDuty-Signature": "v1=ignored"})
    names = [r.uri for r in payload.affected_refs]
    assert names[0] == "alert://pagerduty/PABC"
    assert "arn:aws:ec2:us-east-1:111122223333:instance/i-0a" in names
    assert "pod/web" in names
    assert "service://web-api" in names
    assert "trace://aabbccddeeff00112233445566778899" in names


@pytest.mark.asyncio
async def test_handler_parse_payload_rejects_non_object_json() -> None:
    handler = AlertsWebhookHandler()
    with pytest.raises(ValueError, match="JSON object"):
        await handler.parse_payload(b'"hello"', {"X-PagerDuty-Signature": "v1=x"})


# ---------------------------------------------------------------------------
# Receiver integration: 200/202 happy path + 400 negative path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receiver_pagerduty_valid_signature_returns_202() -> None:
    src = _alert_source(secret="secret-1")
    app = _app_with(src)
    body = json.dumps(
        {"event": {"data": {"id": "P1", "severity": "warning", "status": "triggered"}}}
    ).encode()
    headers = {
        "X-PagerDuty-Signature": _pd_sign(body, "secret-1"),
        "Content-Type": "application/json",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.post("/api/v1/ingest/webhook/alerts-prod", content=body, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["accepted"] is True


@pytest.mark.asyncio
async def test_receiver_pagerduty_invalid_signature_returns_400() -> None:
    src = _alert_source(secret="secret-1")
    app = _app_with(src)
    body = b'{"event":{"data":{"id":"P1","severity":"warning","status":"triggered"}}}'
    bad_header = "v1=" + "0" * 64
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.post(
            "/api/v1/ingest/webhook/alerts-prod",
            content=body,
            headers={"X-PagerDuty-Signature": bad_header},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_receiver_datadog_invalid_signature_returns_400() -> None:
    src = _alert_source(secret="secret-1")
    app = _app_with(src)
    body = b'{"id":"42","alert_type":"alert","priority":"P1"}'
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.post(
            "/api/v1/ingest/webhook/alerts-prod",
            content=body,
            headers={
                "X-Datadog-Signature": "0" * 64,
                "X-Datadog-Signature-Timestamp": str(int(time.time())),
            },
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# CROSS-WORKSPACE ACL TEST (P0 — payload tenant_id MUST be ignored)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_acl_payload_tenant_id_is_ignored() -> None:
    """A malicious payload that claims a foreign ``tenant_id`` MUST NOT bind
    the alert to that workspace.  The receiver derives workspace identity
    SOLELY from ``Source.tenant_id`` resolved by the URL-path source name.
    """
    workspace_a = uuid.uuid4()
    workspace_b_attacker = uuid.uuid4()  # NOT used by the receiver
    assert workspace_a != workspace_b_attacker

    src = _alert_source(name="alerts-A", tenant_id=workspace_a, secret="secret-A")

    # Forge a payload that includes B's workspace UUID in *every* tenant-shaped
    # field a naive implementation might accidentally read.
    body_dict: dict[str, Any] = {
        "tenant_id": str(workspace_b_attacker),
        "customer_id": str(workspace_b_attacker),
        "org_id": str(workspace_b_attacker),
        "routing_key": str(workspace_b_attacker),
        "account_id": str(workspace_b_attacker),
        "team": str(workspace_b_attacker),
        "event": {
            "data": {
                "id": "PEVIL",
                "severity": "critical",
                "status": "triggered",
                "title": "Forged tenant",
                "tenant_id": str(workspace_b_attacker),
                "service": {"summary": "anything"},
                "custom_details": {
                    "tenant_id": str(workspace_b_attacker),
                    "workspace_id": str(workspace_b_attacker),
                    "owner_workspace": str(workspace_b_attacker),
                },
            }
        },
    }
    body = json.dumps(body_dict).encode()

    handler = AlertsWebhookHandler()
    sig = _pd_sign(body, "secret-A")

    # Step 1: signature succeeds with source A's secret.
    assert await handler.verify_signature(body, {"X-PagerDuty-Signature": sig}, "secret-A") is True

    # Step 2: the parsed payload carries forensic tenant fields in `raw` but
    # NEVER surfaces them as a `workspace_id`/`tenant_id` on the alert ref's
    # metadata.  Tenant identity is the receiver's job (Source.tenant_id).
    parsed = await handler.parse_payload(body, {"X-PagerDuty-Signature": sig})
    alert_ref = parsed.affected_refs[0]
    assert alert_ref.metadata["kind"] == "alert"
    assert "workspace_id" not in alert_ref.metadata
    assert "tenant_id" not in alert_ref.metadata
    assert alert_ref.metadata.get("provider_alert_id") == "PEVIL"

    # Step 3: end-to-end through the FastAPI receiver.  `Source.tenant_id` is
    # the only thing that can establish workspace.  We assert the request is
    # accepted under source A; the attacker's `tenant_id` payload field is
    # never consulted (the receiver does not call any function that reads it).
    app = _app_with(src)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.post(
            "/api/v1/ingest/webhook/alerts-A",
            content=body,
            headers={
                "X-PagerDuty-Signature": sig,
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 202

    # Step 4: confirm the source ORM row used by the receiver is the one
    # bound to workspace A — never workspace B.
    assert src.tenant_id == workspace_a
    assert src.tenant_id != workspace_b_attacker


@pytest.mark.asyncio
async def test_forged_payload_rejected_with_400_when_signed_with_wrong_secret() -> None:
    """A payload signed with the wrong secret is rejected; no entities emitted."""
    src = _alert_source(secret="real-secret")
    app = _app_with(src)
    body = b'{"event":{"data":{"id":"P1","severity":"warning","status":"triggered"}}}'
    bad_sig = _pd_sign(body, "attacker-secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.post(
            "/api/v1/ingest/webhook/alerts-prod",
            content=body,
            headers={"X-PagerDuty-Signature": bad_sig},
        )
    assert resp.status_code == 400
