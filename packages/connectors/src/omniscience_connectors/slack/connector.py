"""Slack source connector.

Discovers messages from configured channels via the Slack Web API
(conversations.history) and assembles thread replies.  Supports the
Slack Events API for real-time message ingestion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
    WebhookPayload,
)

__all__ = ["SlackConfig", "SlackConnector"]

logger = logging.getLogger(__name__)

_SLACK_API_BASE = "https://slack.com/api"
_PAGE_SIZE = 200


class SlackConfig(BaseModel):
    """Public configuration for the Slack connector (no secrets)."""

    channel_ids: list[str] = Field(default_factory=list)
    """Slack channel IDs (C... format) to discover messages from."""

    include_threads: bool = True
    """Whether to fetch thread replies for each message."""

    max_age_days: int = 90
    """Ignore messages older than this many days."""


# ---------------------------------------------------------------------------
# Slack Events API webhook handler
# ---------------------------------------------------------------------------


class SlackWebhookHandler(WebhookHandler):
    """Handler for Slack Events API push events.

    Slack signs requests with HMAC-SHA256 using a signing secret.
    The signature is in the ``X-Slack-Signature`` header as ``v0=<hex>``,
    computed over ``v0:<timestamp>:<raw_body>``.
    """

    _SIG_HEADER = "x-slack-signature"
    _TS_HEADER = "x-slack-request-timestamp"
    # Reject requests older than 5 minutes to prevent replay attacks
    _MAX_AGE_SECONDS = 300

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        lower = {k.lower(): v for k, v in headers.items()}
        sig_header = lower.get(self._SIG_HEADER, "")
        ts_header = lower.get(self._TS_HEADER, "")

        if not sig_header or not ts_header:
            return False

        try:
            ts = int(ts_header)
        except ValueError:
            return False

        # Reject stale requests
        if abs(time.time() - ts) > self._MAX_AGE_SECONDS:
            return False

        base_str = f"v0:{ts}:{payload.decode('utf-8', errors='replace')}".encode()
        expected = "v0=" + hmac.new(secret.encode(), base_str, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig_header)

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        lower_headers = {k.lower(): v for k, v in headers.items()}

        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Slack webhook: invalid JSON: {exc}") from exc

        # Handle Slack URL verification challenge
        if data.get("type") == "url_verification":
            # The framework should return the challenge — we yield no refs.
            return WebhookPayload(
                source_name="slack",
                affected_refs=[],
                raw_headers=lower_headers,
            )

        affected: list[DocumentRef] = []
        event = data.get("event", {})
        if isinstance(event, dict):
            event_type = event.get("type", "")
            if event_type in ("message", "message.channels", "message.groups"):
                channel = str(event.get("channel", ""))
                ts = str(event.get("ts", ""))
                thread_ts = str(event.get("thread_ts", ts))
                if channel and ts:
                    # Use thread_ts as the document ID (threads are the unit)
                    # sha1 used only as a compact fingerprint, not for security
                    external_id = hashlib.sha1(  # noqa: S324
                        f"{channel}:{thread_ts}".encode()
                    ).hexdigest()
                    affected.append(
                        DocumentRef(
                            external_id=external_id,
                            uri=f"slack://channel/{channel}/thread/{thread_ts}",
                            metadata={
                                "channel_id": channel,
                                "thread_ts": thread_ts,
                                "event_type": event_type,
                            },
                        )
                    )

        return WebhookPayload(
            source_name="slack",
            affected_refs=affected,
            raw_headers=lower_headers,
        )


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


def _ts_to_datetime(ts: str) -> datetime | None:
    """Convert a Slack timestamp string (e.g. '1609459200.000000') to datetime."""
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (ValueError, OSError):
        return None


def _oldest_ts(max_age_days: int) -> str:
    """Return the Unix timestamp (as string) for max_age_days ago."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=max_age_days)
    return str(cutoff.timestamp())


class SlackConnector(Connector):
    """Source connector for Slack workspaces.

    Discovers messages from configured channels and optionally assembles
    thread replies.  File metadata is included in the document text.
    Supports the Slack Events API for push-based ingestion.
    """

    connector_type: ClassVar[str] = "slack"
    config_schema: ClassVar[type[BaseModel]] = SlackConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the bot token by calling auth.test."""
        headers = _auth_headers(secrets)
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await client.post(f"{_SLACK_API_BASE}/auth.test")
            data = resp.json()
            if not data.get("ok"):
                error = data.get("error", "unknown")
                raise PermissionError(f"Slack authentication failed ({error}) — check bot_token.")

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield DocumentRefs for Slack thread roots in configured channels."""
        cfg: SlackConfig = config  # type: ignore[assignment]
        headers = _auth_headers(secrets)
        oldest = _oldest_ts(cfg.max_age_days)

        channels = cfg.channel_ids if cfg.channel_ids else await self._list_channels(headers)

        for channel_id in channels:
            async for ref in self._discover_channel(channel_id, oldest, headers):
                yield ref

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch a Slack thread (root + replies) and return as Markdown."""
        cfg: SlackConfig = config  # type: ignore[assignment]
        headers = _auth_headers(secrets)

        channel_id = ref.metadata.get("channel_id", "")
        thread_ts = ref.metadata.get("thread_ts", "")

        if not channel_id or not thread_ts:
            raise ValueError(
                f"DocumentRef missing 'channel_id' or 'thread_ts' in metadata: {ref.uri!r}"
            )

        # Fetch root message
        root_messages = await self._history_page(channel_id, thread_ts, thread_ts, headers)
        root_text = ""
        if root_messages:
            root_text = _message_to_markdown(root_messages[0])

        # Fetch replies if configured
        reply_text = ""
        if cfg.include_threads:
            replies = await self._fetch_replies(channel_id, thread_ts, headers)
            reply_lines = [_message_to_markdown(m) for m in replies if m.get("ts") != thread_ts]
            if reply_lines:
                reply_text = "\n\n".join(reply_lines)

        # Assemble document
        channel_name = ref.metadata.get("channel_name", channel_id)
        parts = [f"## #{channel_name} — Thread {thread_ts}"]
        if root_text:
            parts.append(root_text)
        if reply_text:
            parts.append("### Replies\n\n" + reply_text)

        content = "\n\n".join(parts)
        return FetchedDocument(
            ref=ref,
            content_bytes=content.encode("utf-8"),
            content_type="text/markdown",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        return SlackWebhookHandler()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _list_channels(self, headers: dict[str, str]) -> list[str]:
        """Return IDs of all public channels the bot has access to."""
        channel_ids: list[str] = []
        cursor: str | None = None

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            while True:
                params: dict[str, Any] = {
                    "types": "public_channel",
                    "limit": 200,
                    "exclude_archived": "true",
                }
                if cursor:
                    params["cursor"] = cursor

                resp = await client.get(f"{_SLACK_API_BASE}/conversations.list", params=params)
                data = resp.json()
                if not data.get("ok"):
                    logger.warning(
                        "slack.list_channels.error",
                        extra={"error": data.get("error", "unknown")},
                    )
                    break

                for ch in data.get("channels", []):
                    ch_id = ch.get("id")
                    if isinstance(ch_id, str):
                        channel_ids.append(ch_id)

                cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not cursor:
                    break

        return channel_ids

    async def _discover_channel(
        self,
        channel_id: str,
        oldest: str,
        headers: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield DocumentRefs for thread roots in one channel."""
        # Get channel info for name
        channel_name = await self._channel_name(channel_id, headers)

        cursor: str | None = None
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            while True:
                params: dict[str, Any] = {
                    "channel": channel_id,
                    "oldest": oldest,
                    "limit": _PAGE_SIZE,
                }
                if cursor:
                    params["cursor"] = cursor

                try:
                    resp = await client.get(
                        f"{_SLACK_API_BASE}/conversations.history", params=params
                    )
                    data = resp.json()
                except httpx.HTTPError as exc:
                    logger.warning(
                        "slack.discover.http_error",
                        extra={"channel_id": channel_id, "error": str(exc)},
                    )
                    return

                if not data.get("ok"):
                    logger.warning(
                        "slack.discover.api_error",
                        extra={
                            "channel_id": channel_id,
                            "error": data.get("error", "unknown"),
                        },
                    )
                    return

                for msg in data.get("messages", []):
                    # Only yield thread roots (skip bot messages and system subtypes)
                    if msg.get("subtype"):
                        continue
                    ts = str(msg.get("ts", ""))
                    if not ts:
                        continue

                    updated_at = _ts_to_datetime(ts)
                    # sha1 used only as a compact fingerprint, not for security
                    external_id = hashlib.sha1(  # noqa: S324
                        f"{channel_id}:{ts}".encode()
                    ).hexdigest()

                    yield DocumentRef(
                        external_id=external_id,
                        uri=f"slack://channel/{channel_id}/thread/{ts}",
                        updated_at=updated_at,
                        metadata={
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "thread_ts": ts,
                            "reply_count": msg.get("reply_count", 0),
                        },
                    )

                cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not cursor or not data.get("has_more"):
                    break

    async def _history_page(
        self,
        channel_id: str,
        oldest: str,
        latest: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch a page of messages from a channel around a timestamp."""
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await client.get(
                f"{_SLACK_API_BASE}/conversations.history",
                params={
                    "channel": channel_id,
                    "oldest": oldest,
                    "latest": latest,
                    "inclusive": "true",
                    "limit": 1,
                },
            )
            data = resp.json()
            messages_raw: list[dict[str, Any]] = data.get("messages", [])
            return messages_raw

    async def _fetch_replies(
        self,
        channel_id: str,
        thread_ts: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch all replies for a thread."""
        messages: list[dict[str, Any]] = []
        cursor: str | None = None

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            while True:
                params: dict[str, Any] = {
                    "channel": channel_id,
                    "ts": thread_ts,
                    "limit": _PAGE_SIZE,
                }
                if cursor:
                    params["cursor"] = cursor

                resp = await client.get(f"{_SLACK_API_BASE}/conversations.replies", params=params)
                data = resp.json()
                if not data.get("ok"):
                    break

                messages.extend(data.get("messages", []))
                cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not cursor or not data.get("has_more"):
                    break

        return messages

    async def _channel_name(self, channel_id: str, headers: dict[str, str]) -> str:
        """Fetch the human-readable channel name."""
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            resp = await client.get(
                f"{_SLACK_API_BASE}/conversations.info",
                params={"channel": channel_id},
            )
            data = resp.json()
            if data.get("ok"):
                ch = data.get("channel", {})
                return str(ch.get("name", channel_id))
        return channel_id


def _auth_headers(secrets: dict[str, str]) -> dict[str, str]:
    token = secrets.get("bot_token", "")
    if not token:
        raise ValueError("Slack secrets must contain 'bot_token'.")
    if not token.startswith("xoxb-"):
        raise ValueError("Slack 'bot_token' must start with 'xoxb-'.")
    return {"Authorization": f"Bearer {token}"}


def _message_to_markdown(msg: dict[str, Any]) -> str:
    """Convert a Slack message dict to a simple Markdown string."""
    user = str(msg.get("user", msg.get("username", "unknown")))
    ts = str(msg.get("ts", ""))
    text = str(msg.get("text", ""))

    dt_str = ""
    dt = _ts_to_datetime(ts)
    if dt:
        dt_str = dt.strftime("%Y-%m-%d %H:%M UTC")

    header = f"**{user}** ({dt_str})" if dt_str else f"**{user}**"
    lines = [header, text]

    # Include file metadata
    files: list[dict[str, Any]] = msg.get("files", [])
    for f in files:
        name = f.get("name", "file")
        mimetype = f.get("mimetype", "")
        size = f.get("size", 0)
        lines.append(f"_Attachment: {name} ({mimetype}, {size} bytes)_")

    return "\n".join(line for line in lines if line)
