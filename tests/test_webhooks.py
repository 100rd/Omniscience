"""Tests for the enhanced webhook receiver (Issue #19).

Covers:
- Connector delegation (signature verification + payload parsing)
- Replay protection: duplicate delivery ID rejection
- Replay protection: expired payload timestamp rejection
- Per-source rate limiting (429)
- 202 response with events_queued count
- 404 for unknown source
- 400 for invalid signature
- Enqueues correct DocumentChangeEvent messages
- Graceful fallback when connector is not registered
- NATS not available -> events_queued=0 but still 202
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_connectors.base import DocumentRef, WebhookHandler, WebhookPayload
from omniscience_core.config import Settings
from omniscience_core.db.models import Source, SourceType
from omniscience_server.app import create_app
from omniscience_server.rest.delivery_tracker import DeliveryTracker
from omniscience_server.rest.webhooks import (
    WebhookAcceptedResponse,
    _check_source_rate_limit,
    _extract_delivery_id,
    _extract_payload_timestamp,
    clear_all_source_buckets,
    verify_webhook_signature,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )


def _make_source(
    name: str = "test-repo",
    source_type: SourceType = SourceType.git,
    webhook_secret: str | None = None,
) -> Source:
    src: Source = MagicMock(spec=Source)
    src.id = uuid.uuid4()
    src.type = source_type
    src.name = name
    cfg: dict[str, Any] = {}
    if webhook_secret is not None:
        cfg["webhook_secret"] = webhook_secret
    src.config = cfg
    return src


def _make_db_session(source: Source | None = None) -> AsyncMock:
    """Build a fake async DB session that returns *source* on first .first() call."""
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


def _make_app_with_source(source: Source | None = None) -> FastAPI:
    app = create_app(settings=_make_settings())
    sess = _make_db_session(source)
    app.state.db_session_factory = MagicMock(return_value=sess)
    return app


async def _client_for(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _github_sig(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_webhook_handler(
    *,
    valid: bool = True,
    refs: list[DocumentRef] | None = None,
) -> WebhookHandler:
    """Build a mock WebhookHandler."""
    handler = MagicMock(spec=WebhookHandler)
    handler.verify_signature = AsyncMock(return_value=valid)
    affected = refs or []
    wp = WebhookPayload(
        source_name="test-repo",
        affected_refs=affected,
        raw_headers={},
    )
    handler.parse_payload = AsyncMock(return_value=wp)
    return handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_source_buckets() -> None:
    """Clear per-source rate-limit state between tests."""
    clear_all_source_buckets()


# ---------------------------------------------------------------------------
# DeliveryTracker unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_tracker_not_duplicate_initially() -> None:
    """A fresh delivery ID is not a duplicate."""
    tracker = DeliveryTracker()
    assert await tracker.is_duplicate("abc-123") is False


@pytest.mark.asyncio
async def test_delivery_tracker_duplicate_after_record() -> None:
    """After recording, the same ID is a duplicate."""
    tracker = DeliveryTracker()
    await tracker.record("abc-123")
    assert await tracker.is_duplicate("abc-123") is True


@pytest.mark.asyncio
async def test_delivery_tracker_different_ids_not_duplicate() -> None:
    """Two distinct IDs do not collide."""
    tracker = DeliveryTracker()
    await tracker.record("id-1")
    assert await tracker.is_duplicate("id-2") is False


@pytest.mark.asyncio
async def test_delivery_tracker_expired_not_duplicate() -> None:
    """IDs older than the window are purged and no longer count as duplicates."""
    tracker = DeliveryTracker(window_seconds=0.05)  # 50 ms window
    await tracker.record("expiring-id")
    await asyncio.sleep(0.1)
    assert await tracker.is_duplicate("expiring-id") is False


@pytest.mark.asyncio
async def test_delivery_tracker_size() -> None:
    """size() returns the correct count of tracked IDs."""
    tracker = DeliveryTracker()
    assert await tracker.size() == 0
    await tracker.record("a")
    await tracker.record("b")
    assert await tracker.size() == 2


@pytest.mark.asyncio
async def test_delivery_tracker_concurrent_safe() -> None:
    """Concurrent record + is_duplicate calls do not raise or corrupt state."""
    tracker = DeliveryTracker()

    async def _write(i: int) -> None:
        await tracker.record(f"id-{i}")

    async def _read(i: int) -> bool:
        return await tracker.is_duplicate(f"id-{i}")

    tasks = [_write(i) for i in range(50)] + [_read(i) for i in range(50)]
    await asyncio.gather(*tasks)
    # Should not raise; count <= 50
    assert await tracker.size() <= 50


# ---------------------------------------------------------------------------
# Signature helper unit tests (legacy/fallback)
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal Request stub for verify_webhook_signature tests."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def test_verify_github_valid_signature() -> None:
    payload = b'{"action":"push"}'
    secret = "mysecret"
    sig = _github_sig(payload, secret)
    req = _FakeRequest({"X-Hub-Signature-256": sig})
    assert verify_webhook_signature("git", payload, secret, req) is True  # type: ignore[arg-type]


def test_verify_github_invalid_signature() -> None:
    payload = b'{"action":"push"}'
    req = _FakeRequest({"X-Hub-Signature-256": "sha256=deadbeef"})
    assert verify_webhook_signature("git", payload, "mysecret", req) is False  # type: ignore[arg-type]


def test_verify_github_missing_header() -> None:
    req = _FakeRequest({})
    assert verify_webhook_signature("git", b"payload", "secret", req) is False  # type: ignore[arg-type]


def test_verify_gitlab_valid_token() -> None:
    req = _FakeRequest({"X-Gitlab-Token": "secret123"})
    assert verify_webhook_signature("gitlab", b"payload", "secret123", req) is True  # type: ignore[arg-type]


def test_verify_gitlab_wrong_token() -> None:
    req = _FakeRequest({"X-Gitlab-Token": "wrong"})
    assert verify_webhook_signature("gitlab", b"payload", "correct", req) is False  # type: ignore[arg-type]


def test_verify_confluence_valid() -> None:
    payload = b"hello"
    secret = "sec"
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    req = _FakeRequest({"X-Hub-Signature": f"sha256={sig}"})
    assert verify_webhook_signature("confluence", payload, secret, req) is True  # type: ignore[arg-type]


def test_verify_unknown_source_type_passes() -> None:
    """Unknown source types bypass signature check (no secret defined for them)."""
    req = _FakeRequest({})
    assert verify_webhook_signature("notion", b"any", "secret", req) is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Delivery ID extraction unit tests
# ---------------------------------------------------------------------------


class _FakeRequestWithHeaders:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def test_extract_delivery_id_github() -> None:
    delivery = str(uuid.uuid4())
    req = _FakeRequestWithHeaders({"x-github-delivery": delivery})
    assert _extract_delivery_id(req) == delivery  # type: ignore[arg-type]


def test_extract_delivery_id_gitlab() -> None:
    event_uuid = str(uuid.uuid4())
    req = _FakeRequestWithHeaders({"x-gitlab-event-uuid": event_uuid})
    assert _extract_delivery_id(req) == event_uuid  # type: ignore[arg-type]


def test_extract_delivery_id_missing() -> None:
    req = _FakeRequestWithHeaders({})
    assert _extract_delivery_id(req) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Timestamp extraction unit tests
# ---------------------------------------------------------------------------


def test_extract_payload_timestamp_found() -> None:
    data = {"timestamp": 1_700_000_000}
    assert _extract_payload_timestamp(data) == 1_700_000_000.0


def test_extract_payload_timestamp_not_dict() -> None:
    assert _extract_payload_timestamp("not a dict") is None


def test_extract_payload_timestamp_missing() -> None:
    assert _extract_payload_timestamp({"action": "push"}) is None


# ---------------------------------------------------------------------------
# Per-source rate limit unit tests
# ---------------------------------------------------------------------------


def test_source_rate_limit_allows_first_request() -> None:
    allowed, _ = _check_source_rate_limit("src-abc", rpm=10)
    assert allowed is True


def test_source_rate_limit_exhausted() -> None:
    # Exhaust bucket (initial capacity = rpm - 1 tokens after first call)
    for _ in range(11):
        _check_source_rate_limit("src-xyz", rpm=10)
    allowed, retry_after = _check_source_rate_limit("src-xyz", rpm=10)
    assert allowed is False
    assert retry_after > 0


# ---------------------------------------------------------------------------
# Integration: 404 for unknown source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_404_unknown_source() -> None:
    """404 is returned when the source name is not in the database."""
    app = _make_app_with_source(source=None)

    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/ingest/webhook/nonexistent",
            content=b'{"action":"push"}',
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "source_not_found"


# ---------------------------------------------------------------------------
# Integration: 400 for invalid signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_400_invalid_signature_fallback() -> None:
    """400 is returned when the fallback verifier rejects the signature."""
    src = _make_source(webhook_secret="correct-secret")
    src.type = SourceType.git

    app = _make_app_with_source(source=src)

    # Patch _get_connector_handler to return None so we use the fallback path.
    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=None,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": "sha256=deadbeef",
                },
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "bad_request"
    assert "signature" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_webhook_400_invalid_signature_connector() -> None:
    """400 is returned when the connector's verify_signature returns False."""
    src = _make_source(webhook_secret="secret")
    src.type = SourceType.git

    app = _make_app_with_source(source=src)
    handler = _make_webhook_handler(valid=False)

    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=handler,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={"Content-Type": "application/json"},
            )

    assert resp.status_code == 400
    handler.verify_signature.assert_awaited_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Integration: connector delegation (valid signature + payload parsing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_delegates_to_connector_handler() -> None:
    """Connector's verify_signature and parse_payload are called."""
    src = _make_source(webhook_secret="secret")
    src.type = SourceType.git

    refs = [
        DocumentRef(external_id="sha-abc", uri="https://github.com/org/repo/blob/main/foo.py"),
        DocumentRef(external_id="sha-def", uri="https://github.com/org/repo/blob/main/bar.py"),
    ]
    handler = _make_webhook_handler(valid=True, refs=refs)
    app = _make_app_with_source(source=src)

    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=handler,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                },
            )

    assert resp.status_code == 202
    handler.verify_signature.assert_awaited_once()  # type: ignore[attr-defined]
    body = resp.json()
    assert body["accepted"] is True
    # 2 refs parsed but NATS unavailable in test → events_queued=0
    assert body["events_queued"] == 0


@pytest.mark.asyncio
async def test_webhook_enqueues_correct_events() -> None:
    """DocumentChangeEvent is published per affected ref when NATS is available."""
    src = _make_source(webhook_secret="secret")
    src.type = SourceType.git

    refs = [
        DocumentRef(external_id="id-1", uri="https://example.com/file1.py"),
        DocumentRef(external_id="id-2", uri="https://example.com/file2.py"),
    ]
    handler = _make_webhook_handler(valid=True, refs=refs)
    app = _make_app_with_source(source=src)

    # Stub a fake NATS connection with a mock JetStream
    mock_js = AsyncMock()
    mock_js.publish = AsyncMock()
    mock_nats = MagicMock()
    mock_nats.jetstream = mock_js
    app.state.nats = mock_nats

    with (
        patch(
            "omniscience_server.rest.webhooks._get_connector_handler",
            return_value=handler,
        ),
        patch(
            "omniscience_server.rest.webhooks.QueueProducer.publish",
            new_callable=AsyncMock,
        ) as mock_publish,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                },
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert body["events_queued"] == 2
    assert mock_publish.call_count == 2

    # Verify the subjects and event shapes
    calls = mock_publish.call_args_list
    for call in calls:
        kwargs = call.kwargs
        assert kwargs["subject"] == f"ingest.changes.{src.type}"
        event = kwargs["payload"]
        assert event.source_id == src.id
        assert event.source_type == str(src.type)
        assert event.action == "updated"


@pytest.mark.asyncio
async def test_webhook_generic_event_when_no_refs() -> None:
    """A single generic event is emitted when the connector returns no refs."""
    src = _make_source(webhook_secret="secret")
    src.type = SourceType.git

    # Handler returns empty refs list
    handler = _make_webhook_handler(valid=True, refs=[])
    app = _make_app_with_source(source=src)

    mock_js = AsyncMock()
    mock_nats = MagicMock()
    mock_nats.jetstream = mock_js
    app.state.nats = mock_nats

    with (
        patch(
            "omniscience_server.rest.webhooks._get_connector_handler",
            return_value=handler,
        ),
        patch(
            "omniscience_server.rest.webhooks.QueueProducer.publish",
            new_callable=AsyncMock,
        ) as mock_publish,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                },
            )

    assert resp.status_code == 202
    assert mock_publish.call_count == 1
    event = mock_publish.call_args.kwargs["payload"]
    assert event.external_id == "*"


# ---------------------------------------------------------------------------
# Integration: 202 with no secret (no signature check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_202_no_secret() -> None:
    """202 is returned when no webhook_secret is configured (no sig check)."""
    src = _make_source(webhook_secret=None)
    src.type = SourceType.git
    app = _make_app_with_source(source=src)

    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=None,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={"Content-Type": "application/json"},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True


# ---------------------------------------------------------------------------
# Integration: replay protection — duplicate delivery ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_400_duplicate_delivery_id() -> None:
    """Second request with the same delivery ID is rejected with 400."""
    src = _make_source(webhook_secret=None)
    src.type = SourceType.git
    app = _make_app_with_source(source=src)
    delivery_id = str(uuid.uuid4())

    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=None,
    ):
        async with await _client_for(app) as client:
            # First delivery — should succeed
            resp1 = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": delivery_id,
                },
            )
            assert resp1.status_code == 202

            # Second delivery with same ID — should be rejected
            resp2 = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": delivery_id,
                },
            )

    assert resp2.status_code == 400
    body = resp2.json()
    assert body["error"]["code"] == "bad_request"
    assert "duplicate" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Integration: replay protection — expired timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_400_expired_timestamp() -> None:
    """Payload with a timestamp older than the replay window is rejected."""
    src = _make_source(webhook_secret=None)
    src.type = SourceType.git
    app = _make_app_with_source(source=src)

    # Timestamp well outside the 5-minute window
    old_ts = int(time.time()) - 600
    payload = json.dumps({"timestamp": old_ts}).encode()

    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=None,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=payload,
                headers={"Content-Type": "application/json"},
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "bad_request"
    assert "old" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_webhook_202_fresh_timestamp() -> None:
    """Payload with a current timestamp passes the age check."""
    src = _make_source(webhook_secret=None)
    src.type = SourceType.git
    app = _make_app_with_source(source=src)

    current_ts = int(time.time())
    payload = json.dumps({"timestamp": current_ts}).encode()

    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=None,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                },
            )

    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Integration: per-source rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_429_rate_limited() -> None:
    """429 is returned when the per-source rate limit is exceeded."""
    src = _make_source(webhook_secret=None)
    src.type = SourceType.git

    # Each call uses a new session instance from the factory.
    # We need an independent session for each request.
    app = create_app(settings=_make_settings())
    session = _make_db_session(source=src)
    app.state.db_session_factory = MagicMock(return_value=session)

    # Patch to use rpm=1 so the second request exceeds the limit.
    with (
        patch(
            "omniscience_server.rest.webhooks._get_connector_handler",
            return_value=None,
        ),
        patch(
            "omniscience_server.rest.webhooks._DEFAULT_SOURCE_RPM",
            1,
        ),
    ):
        async with await _client_for(app) as client:
            # Exhaust the single token.
            r1 = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={"Content-Type": "application/json"},
            )
            # Second request — bucket empty.
            r2 = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={"Content-Type": "application/json"},
            )

    assert r1.status_code == 202
    assert r2.status_code == 429
    body = r2.json()
    assert body["error"]["code"] == "rate_limited"
    assert "Retry-After" in r2.headers


# ---------------------------------------------------------------------------
# Integration: graceful fallback when connector not registered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_202_connector_not_registered() -> None:
    """202 is returned even when the connector type is not in the registry."""
    src = _make_source(webhook_secret=None)
    src.type = SourceType.git
    app = _make_app_with_source(source=src)

    # Do NOT patch _get_connector_handler — let the real registry be used.
    # Since "git" may not be registered, _get_connector_handler returns None gracefully.
    async with await _client_for(app) as client:
        resp = await client.post(
            "/api/v1/ingest/webhook/test-repo",
            content=b'{"action":"push"}',
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True


# ---------------------------------------------------------------------------
# Integration: NATS unavailable — events_queued=0 but still 202
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_202_nats_unavailable() -> None:
    """202 is returned and events_queued=0 when NATS is not connected."""
    src = _make_source(webhook_secret=None)
    src.type = SourceType.git
    app = _make_app_with_source(source=src)
    # Explicitly unset nats on app state
    app.state.nats = None

    refs = [DocumentRef(external_id="id-1", uri="https://example.com/f.py")]
    handler = _make_webhook_handler(valid=True, refs=refs)

    with patch(
        "omniscience_server.rest.webhooks._get_connector_handler",
        return_value=handler,
    ):
        async with await _client_for(app) as client:
            resp = await client.post(
                "/api/v1/ingest/webhook/test-repo",
                content=b'{"action":"push"}',
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                },
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    assert body["events_queued"] == 0


# ---------------------------------------------------------------------------
# Response model unit test
# ---------------------------------------------------------------------------


def test_webhook_accepted_response_model() -> None:
    r = WebhookAcceptedResponse(accepted=True, events_queued=3)
    assert r.accepted is True
    assert r.events_queued == 3
    data = r.model_dump()
    assert data == {"accepted": True, "events_queued": 3}
