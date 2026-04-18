"""Tests for Confluence, Notion, Slack, and Jira connectors (Issues #53-#56).

All HTTP calls are mocked via respx so no network access is required.
Covers validate, discover, fetch, and webhook handling for each connector,
plus content conversion helpers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest
import respx
from omniscience_connectors import (
    ConfluenceConnector,
    DocumentRef,
    FetchedDocument,
    JiraConnector,
    NotionConnector,
    SlackConnector,
    get_connector,
)
from omniscience_connectors.confluence.connector import (
    ConfluenceConfig,
    ConfluenceWebhookHandler,
    storage_to_markdown,
)
from omniscience_connectors.jira.connector import (
    JiraConfig,
    JiraWebhookHandler,
    _adf_to_markdown,
    _build_jql,
)
from omniscience_connectors.notion.connector import (
    NotionConfig,
    _block_to_markdown,
    _rich_text_to_str,
    blocks_to_markdown,
)
from omniscience_connectors.slack.connector import (
    SlackConfig,
    SlackWebhookHandler,
    _message_to_markdown,
    _ts_to_datetime,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFLUENCE_BASE = "https://example.atlassian.net/wiki"
_NOTION_TOKEN = "secret_abc123"
_SLACK_TOKEN = "xoxb-fake-token"
_JIRA_BASE = "https://example.atlassian.net"


def _b64_creds(email: str, token: str) -> str:
    return base64.b64encode(f"{email}:{token}".encode()).decode()


# ---------------------------------------------------------------------------
# Registry: all 4 connectors are auto-registered
# ---------------------------------------------------------------------------


def test_registry_has_confluence() -> None:
    c = get_connector("confluence")
    assert isinstance(c, ConfluenceConnector)


def test_registry_has_notion() -> None:
    c = get_connector("notion")
    assert isinstance(c, NotionConnector)


def test_registry_has_slack() -> None:
    c = get_connector("slack")
    assert isinstance(c, SlackConnector)


def test_registry_has_jira() -> None:
    c = get_connector("jira")
    assert isinstance(c, JiraConnector)


# ===========================================================================
# CONFLUENCE
# ===========================================================================


class TestConfluenceStorageToMarkdown:
    def test_paragraph(self) -> None:
        html = "<p>Hello world</p>"
        md = storage_to_markdown(html)
        assert "Hello world" in md

    def test_heading(self) -> None:
        md = storage_to_markdown("<h1>Title</h1>")
        assert md.startswith("# Title")

    def test_h2(self) -> None:
        md = storage_to_markdown("<h2>Section</h2>")
        assert "## Section" in md

    def test_code_block(self) -> None:
        md = storage_to_markdown("<pre>print('hello')</pre>")
        assert "```" in md
        assert "print" in md

    def test_list_items(self) -> None:
        html = "<ul><li>Item A</li><li>Item B</li></ul>"
        md = storage_to_markdown(html)
        assert "- Item A" in md
        assert "- Item B" in md

    def test_html_entity_amp(self) -> None:
        md = storage_to_markdown("<p>a &amp; b</p>")
        assert "a & b" in md

    def test_html_entity_nbsp(self) -> None:
        md = storage_to_markdown("<p>hello&nbsp;world</p>")
        assert "hello" in md
        assert "world" in md

    def test_strips_all_tags(self) -> None:
        md = storage_to_markdown("<ac:structured-macro>stuff</ac:structured-macro>")
        assert "<" not in md

    def test_horizontal_rule(self) -> None:
        md = storage_to_markdown("<hr/>")
        assert "---" in md

    def test_empty_input(self) -> None:
        md = storage_to_markdown("")
        assert md == ""

    def test_numeric_entity(self) -> None:
        md = storage_to_markdown("&#65;")
        assert "A" in md


class TestConfluenceValidate:
    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        respx.get(f"{_CONFLUENCE_BASE}/rest/api/user/current").mock(
            return_value=httpx.Response(200, json={"accountId": "abc123"})
        )
        connector = ConfluenceConnector()
        config = ConfluenceConfig(base_url=_CONFLUENCE_BASE, space_keys=["DOCS"])
        secrets = {"email": "user@example.com", "api_token": "mytoken"}
        await connector.validate(config, secrets)  # should not raise

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_unauthorized_raises(self) -> None:
        respx.get(f"{_CONFLUENCE_BASE}/rest/api/user/current").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        connector = ConfluenceConnector()
        config = ConfluenceConfig(base_url=_CONFLUENCE_BASE)
        secrets = {"email": "user@example.com", "api_token": "bad"}
        with pytest.raises(PermissionError):
            await connector.validate(config, secrets)

    @pytest.mark.asyncio
    async def test_validate_missing_secrets_raises(self) -> None:
        connector = ConfluenceConnector()
        config = ConfluenceConfig(base_url=_CONFLUENCE_BASE)
        with pytest.raises(ValueError):
            await connector.validate(config, {})

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_with_pat(self) -> None:
        respx.get(f"{_CONFLUENCE_BASE}/rest/api/user/current").mock(
            return_value=httpx.Response(200, json={"accountId": "abc"})
        )
        connector = ConfluenceConnector()
        config = ConfluenceConfig(base_url=_CONFLUENCE_BASE)
        await connector.validate(config, {"pat": "myPAT"})


class TestConfluenceDiscover:
    def _make_result(
        self, content_id: str = "123", title: str = "My Page", version: int = 1
    ) -> dict[str, Any]:
        return {
            "id": content_id,
            "title": title,
            "type": "page",
            "version": {"number": version, "when": "2024-01-15T10:00:00.000Z"},
            "metadata": {"labels": {"results": [{"name": "public"}]}},
            "space": {"key": "DOCS"},
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_yields_refs(self) -> None:
        page_response = {
            "results": [self._make_result()],
            "size": 1,
            "totalSize": 1,
        }
        respx.get(f"{_CONFLUENCE_BASE}/rest/api/content").mock(
            return_value=httpx.Response(200, json=page_response)
        )

        connector = ConfluenceConnector()
        config = ConfluenceConfig(
            base_url=_CONFLUENCE_BASE, space_keys=["DOCS"], content_types=["page"]
        )
        secrets = {"email": "u@x.com", "api_token": "tok"}

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, secrets):
            refs.append(ref)

        assert len(refs) == 1
        assert refs[0].metadata["content_id"] == "123"
        assert refs[0].metadata["title"] == "My Page"
        assert refs[0].metadata["space_key"] == "DOCS"
        assert refs[0].external_id  # non-empty

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_excludes_labels(self) -> None:
        result = self._make_result()
        result["metadata"]["labels"]["results"] = [{"name": "internal"}]
        page_response = {"results": [result], "size": 1, "totalSize": 1}
        respx.get(f"{_CONFLUENCE_BASE}/rest/api/content").mock(
            return_value=httpx.Response(200, json=page_response)
        )

        connector = ConfluenceConnector()
        config = ConfluenceConfig(
            base_url=_CONFLUENCE_BASE,
            space_keys=["DOCS"],
            exclude_labels=["internal"],
        )
        secrets = {"email": "u@x.com", "api_token": "tok"}

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, secrets):
            refs.append(ref)

        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_handles_api_error_gracefully(self) -> None:
        respx.get(f"{_CONFLUENCE_BASE}/rest/api/content").mock(
            return_value=httpx.Response(403)
        )
        connector = ConfluenceConnector()
        config = ConfluenceConfig(base_url=_CONFLUENCE_BASE, space_keys=["DOCS"])
        secrets = {"email": "u@x.com", "api_token": "tok"}

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, secrets):
            refs.append(ref)

        assert refs == []  # graceful — no crash


class TestConfluenceFetch:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_returns_markdown(self) -> None:
        content_response = {
            "id": "456",
            "title": "Test Page",
            "body": {
                "storage": {
                    "value": "<h1>Hello</h1><p>World content here.</p>",
                    "representation": "storage",
                }
            },
            "space": {"key": "DOCS"},
            "version": {"number": 2},
        }
        respx.get(f"{_CONFLUENCE_BASE}/rest/api/content/456").mock(
            return_value=httpx.Response(200, json=content_response)
        )

        connector = ConfluenceConnector()
        config = ConfluenceConfig(base_url=_CONFLUENCE_BASE)
        secrets = {"email": "u@x.com", "api_token": "tok"}
        ref = DocumentRef(
            external_id="sha1abc",
            uri=f"{_CONFLUENCE_BASE}/pages/viewpage.action?pageId=456",
            metadata={"content_id": "456", "title": "Test Page", "space_key": "DOCS"},
        )

        result = await connector.fetch(config, secrets, ref)

        assert isinstance(result, FetchedDocument)
        assert result.content_type == "text/markdown"
        decoded = result.content_bytes.decode()
        assert "# Test Page" in decoded
        assert "Hello" in decoded

    @pytest.mark.asyncio
    async def test_fetch_missing_content_id_raises(self) -> None:
        connector = ConfluenceConnector()
        config = ConfluenceConfig(base_url=_CONFLUENCE_BASE)
        secrets = {"email": "u@x.com", "api_token": "tok"}
        ref = DocumentRef(external_id="x", uri="http://example.com", metadata={})

        with pytest.raises(ValueError, match="content_id"):
            await connector.fetch(config, secrets, ref)


class TestConfluenceWebhook:
    async def test_verify_signature_valid(self) -> None:
        handler = ConfluenceWebhookHandler()
        secret = "mysecret"
        payload = b'{"webhookEvent": "page_created"}'
        sig = hmac.new(secret.encode(), payload, "sha256").hexdigest()
        headers = {"x-hub-signature": f"sha256={sig}"}
        result = await handler.verify_signature(payload, headers, secret)
        assert result is True

    async def test_verify_signature_invalid(self) -> None:
        handler = ConfluenceWebhookHandler()
        headers = {"x-hub-signature": "sha256=badhash"}
        result = await handler.verify_signature(b"payload", headers, "secret")
        assert result is False

    async def test_verify_signature_missing_header(self) -> None:
        handler = ConfluenceWebhookHandler()
        result = await handler.verify_signature(b"payload", {}, "secret")
        assert result is False

    async def test_parse_page_created_event(self) -> None:
        handler = ConfluenceWebhookHandler()
        payload = json.dumps({
            "webhookEvent": "page_created",
            "page": {
                "id": "789",
                "title": "New Page",
                "space": {"key": "TEAM"},
            },
        }).encode()
        result = await handler.parse_payload(payload, {})
        assert result.source_name == "confluence"
        assert len(result.affected_refs) == 1
        assert result.affected_refs[0].external_id == "789"

    async def test_parse_invalid_json_raises(self) -> None:
        handler = ConfluenceWebhookHandler()
        with pytest.raises(ValueError):
            await handler.parse_payload(b"not json", {})

    def test_connector_returns_handler(self) -> None:
        connector = ConfluenceConnector()
        handler = connector.webhook_handler()
        assert handler is not None


# ===========================================================================
# NOTION
# ===========================================================================


class TestNotionBlocksToMarkdown:
    def _rt(self, text: str) -> dict[str, Any]:
        return {"plain_text": text, "annotations": {}, "type": "text"}

    def test_paragraph(self) -> None:
        block = {
            "type": "paragraph",
            "paragraph": {"rich_text": [self._rt("Hello world")]},
        }
        md = _block_to_markdown(block)
        assert "Hello world" in md

    def test_heading_1(self) -> None:
        block = {
            "type": "heading_1",
            "heading_1": {"rich_text": [self._rt("Top Heading")]},
        }
        md = _block_to_markdown(block)
        assert md.startswith("# Top Heading")

    def test_heading_2(self) -> None:
        block = {
            "type": "heading_2",
            "heading_2": {"rich_text": [self._rt("Sub Heading")]},
        }
        md = _block_to_markdown(block)
        assert md.startswith("## Sub Heading")

    def test_bullet_list(self) -> None:
        block = {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [self._rt("List item")]},
        }
        md = _block_to_markdown(block)
        assert md.startswith("- List item")

    def test_numbered_list(self) -> None:
        block = {
            "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": [self._rt("First")]},
        }
        md = _block_to_markdown(block)
        assert md.startswith("1. First")

    def test_code_block(self) -> None:
        block = {
            "type": "code",
            "code": {
                "rich_text": [self._rt("print('hi')")],
                "language": "python",
            },
        }
        md = _block_to_markdown(block)
        assert "```python" in md
        assert "print('hi')" in md

    def test_divider(self) -> None:
        block = {"type": "divider", "divider": {}}
        md = _block_to_markdown(block)
        assert "---" in md

    def test_to_do_checked(self) -> None:
        block = {
            "type": "to_do",
            "to_do": {"rich_text": [self._rt("Done task")], "checked": True},
        }
        md = _block_to_markdown(block)
        assert "[x]" in md
        assert "Done task" in md

    def test_to_do_unchecked(self) -> None:
        block = {
            "type": "to_do",
            "to_do": {"rich_text": [self._rt("Pending task")], "checked": False},
        }
        md = _block_to_markdown(block)
        assert "[ ]" in md

    def test_rich_text_concatenation(self) -> None:
        rich_texts = [{"plain_text": "Hello"}, {"plain_text": " World"}]
        assert _rich_text_to_str(rich_texts) == "Hello World"

    def test_blocks_to_markdown_multiple_blocks(self) -> None:
        blocks: list[dict[str, Any]] = [
            {
                "type": "heading_1",
                "heading_1": {"rich_text": [self._rt("Title")]},
            },
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [self._rt("Body text")]},
            },
        ]
        md = blocks_to_markdown(blocks)
        assert "# Title" in md
        assert "Body text" in md


class TestNotionValidate:
    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        respx.get("https://api.notion.com/v1/users/me").mock(
            return_value=httpx.Response(200, json={"object": "user", "id": "uid123"})
        )
        connector = NotionConnector()
        config = NotionConfig()
        await connector.validate(config, {"integration_token": _NOTION_TOKEN})

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_unauthorized_raises(self) -> None:
        respx.get("https://api.notion.com/v1/users/me").mock(
            return_value=httpx.Response(401, json={"code": "unauthorized"})
        )
        connector = NotionConnector()
        config = NotionConfig()
        with pytest.raises(PermissionError):
            await connector.validate(config, {"integration_token": "bad"})

    @pytest.mark.asyncio
    async def test_validate_missing_token_raises(self) -> None:
        connector = NotionConnector()
        config = NotionConfig()
        with pytest.raises(ValueError, match="integration_token"):
            await connector.validate(config, {})


class TestNotionDiscover:
    def _make_page(self, page_id: str = "page-1") -> dict[str, Any]:
        return {
            "object": "page",
            "id": page_id,
            "url": f"https://notion.so/{page_id}",
            "created_time": "2024-01-01T00:00:00.000Z",
            "last_edited_time": "2024-06-01T12:00:00.000Z",
            "properties": {
                "Name": {
                    "type": "title",
                    "title": [{"plain_text": "Test Page"}],
                }
            },
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_from_database(self) -> None:
        db_id = "db-abc"
        respx.post(f"https://api.notion.com/v1/databases/{db_id}/query").mock(
            return_value=httpx.Response(
                200,
                json={"object": "list", "results": [self._make_page()], "has_more": False},
            )
        )
        connector = NotionConnector()
        config = NotionConfig(database_ids=[db_id])
        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {"integration_token": _NOTION_TOKEN}):
            refs.append(ref)

        assert len(refs) == 1
        assert refs[0].metadata["page_id"] == "page-1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_handles_api_error_gracefully(self) -> None:
        db_id = "db-bad"
        respx.post(f"https://api.notion.com/v1/databases/{db_id}/query").mock(
            return_value=httpx.Response(403)
        )
        connector = NotionConnector()
        config = NotionConfig(database_ids=[db_id])
        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {"integration_token": _NOTION_TOKEN}):
            refs.append(ref)
        assert refs == []


class TestNotionFetch:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_returns_markdown(self) -> None:
        page_id = "page-xyz"
        page_data = {
            "id": page_id,
            "url": f"https://notion.so/{page_id}",
            "properties": {
                "Name": {
                    "type": "title",
                    "title": [{"plain_text": "My Notion Page"}],
                }
            },
        }
        blocks_data = {
            "object": "list",
            "results": [
                {
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "Hello from Notion"}]},
                }
            ],
            "has_more": False,
        }

        respx.get(f"https://api.notion.com/v1/pages/{page_id}").mock(
            return_value=httpx.Response(200, json=page_data)
        )
        respx.get(f"https://api.notion.com/v1/blocks/{page_id}/children").mock(
            return_value=httpx.Response(200, json=blocks_data)
        )

        connector = NotionConnector()
        config = NotionConfig()
        secrets = {"integration_token": _NOTION_TOKEN}
        ref = DocumentRef(
            external_id="sha1abc",
            uri=f"https://notion.so/{page_id}",
            metadata={"page_id": page_id},
        )

        result = await connector.fetch(config, secrets, ref)

        assert result.content_type == "text/markdown"
        decoded = result.content_bytes.decode()
        assert "My Notion Page" in decoded
        assert "Hello from Notion" in decoded

    @pytest.mark.asyncio
    async def test_fetch_missing_page_id_raises(self) -> None:
        connector = NotionConnector()
        config = NotionConfig()
        ref = DocumentRef(external_id="x", uri="https://notion.so/x", metadata={})
        with pytest.raises(ValueError, match="page_id"):
            await connector.fetch(config, {"integration_token": _NOTION_TOKEN}, ref)

    def test_no_webhook_handler(self) -> None:
        connector = NotionConnector()
        assert connector.webhook_handler() is None


# ===========================================================================
# SLACK
# ===========================================================================


class TestSlackHelpers:
    def test_ts_to_datetime(self) -> None:
        dt = _ts_to_datetime("1609459200.000000")
        assert dt is not None
        assert dt.year == 2021

    def test_ts_to_datetime_invalid(self) -> None:
        assert _ts_to_datetime("not-a-ts") is None

    def test_message_to_markdown_basic(self) -> None:
        msg: dict[str, Any] = {
            "user": "U12345",
            "ts": "1609459200.000000",
            "text": "Hello team!",
        }
        md = _message_to_markdown(msg)
        assert "U12345" in md
        assert "Hello team!" in md

    def test_message_to_markdown_with_files(self) -> None:
        msg: dict[str, Any] = {
            "user": "U99",
            "ts": "1609459200.000000",
            "text": "See attached",
            "files": [{"name": "report.pdf", "mimetype": "application/pdf", "size": 1024}],
        }
        md = _message_to_markdown(msg)
        assert "report.pdf" in md
        assert "application/pdf" in md


class TestSlackValidate:
    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        respx.post("https://slack.com/api/auth.test").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "user": "bot", "team": "myteam"}
            )
        )
        connector = SlackConnector()
        config = SlackConfig(channel_ids=["C123"])
        await connector.validate(config, {"bot_token": _SLACK_TOKEN})

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_bad_token_raises(self) -> None:
        respx.post("https://slack.com/api/auth.test").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        )
        connector = SlackConnector()
        config = SlackConfig()
        with pytest.raises(PermissionError):
            await connector.validate(config, {"bot_token": _SLACK_TOKEN})

    @pytest.mark.asyncio
    async def test_validate_missing_token_raises(self) -> None:
        connector = SlackConnector()
        config = SlackConfig()
        with pytest.raises(ValueError, match="bot_token"):
            await connector.validate(config, {})

    @pytest.mark.asyncio
    async def test_validate_wrong_token_prefix_raises(self) -> None:
        connector = SlackConnector()
        config = SlackConfig()
        with pytest.raises(ValueError, match="xoxb-"):
            await connector.validate(config, {"bot_token": "xoxa-bad-token"})


class TestSlackDiscover:
    def _history_response(self, ts: str = "1609459200.000000") -> dict[str, Any]:
        return {
            "ok": True,
            "messages": [{"type": "message", "ts": ts, "text": "hi"}],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_yields_refs_for_channel(self) -> None:
        channel_id = "C123"
        respx.get("https://slack.com/api/conversations.history").mock(
            return_value=httpx.Response(200, json=self._history_response())
        )
        respx.get("https://slack.com/api/conversations.info").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "channel": {"name": "general"}}
            )
        )

        connector = SlackConnector()
        config = SlackConfig(channel_ids=[channel_id], max_age_days=365)
        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {"bot_token": _SLACK_TOKEN}):
            refs.append(ref)

        assert len(refs) == 1
        assert refs[0].metadata["channel_id"] == channel_id
        assert refs[0].metadata["thread_ts"] == "1609459200.000000"

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_skips_subtypes(self) -> None:
        channel_id = "C456"
        history = {
            "ok": True,
            "messages": [
                {"type": "message", "subtype": "bot_message", "ts": "1609459200.000000"},
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }
        respx.get("https://slack.com/api/conversations.history").mock(
            return_value=httpx.Response(200, json=history)
        )
        respx.get("https://slack.com/api/conversations.info").mock(
            return_value=httpx.Response(200, json={"ok": True, "channel": {"name": "dev"}})
        )

        connector = SlackConnector()
        config = SlackConfig(channel_ids=[channel_id], max_age_days=365)
        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {"bot_token": _SLACK_TOKEN}):
            refs.append(ref)

        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_handles_api_error_gracefully(self) -> None:
        respx.get("https://slack.com/api/conversations.history").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        )
        respx.get("https://slack.com/api/conversations.info").mock(
            return_value=httpx.Response(200, json={"ok": True, "channel": {"name": "gone"}})
        )

        connector = SlackConnector()
        config = SlackConfig(channel_ids=["C_GONE"], max_age_days=365)
        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {"bot_token": _SLACK_TOKEN}):
            refs.append(ref)
        assert refs == []


class TestSlackFetch:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_thread_with_replies(self) -> None:
        channel_id = "C789"
        thread_ts = "1609459200.000000"
        root_message = {
            "ok": True,
            "messages": [
                {"user": "U1", "ts": thread_ts, "text": "Root message"}
            ],
        }
        replies = {
            "ok": True,
            "messages": [
                {"user": "U1", "ts": thread_ts, "text": "Root message"},
                {"user": "U2", "ts": "1609459260.000000", "text": "Reply here"},
            ],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }

        respx.get("https://slack.com/api/conversations.history").mock(
            return_value=httpx.Response(200, json=root_message)
        )
        respx.get("https://slack.com/api/conversations.replies").mock(
            return_value=httpx.Response(200, json=replies)
        )

        connector = SlackConnector()
        config = SlackConfig(include_threads=True)
        secrets = {"bot_token": _SLACK_TOKEN}
        ref = DocumentRef(
            external_id="sha1xyz",
            uri=f"slack://channel/{channel_id}/thread/{thread_ts}",
            metadata={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "channel_name": "general",
            },
        )

        result = await connector.fetch(config, secrets, ref)

        assert result.content_type == "text/markdown"
        decoded = result.content_bytes.decode()
        assert "Root message" in decoded
        assert "Reply here" in decoded

    @pytest.mark.asyncio
    async def test_fetch_missing_channel_raises(self) -> None:
        connector = SlackConnector()
        config = SlackConfig()
        ref = DocumentRef(external_id="x", uri="slack://x", metadata={})
        with pytest.raises(ValueError, match="channel_id"):
            await connector.fetch(config, {"bot_token": _SLACK_TOKEN}, ref)


class TestSlackWebhook:
    def _make_sig(self, secret: str, ts: int, body: bytes) -> str:
        base = f"v0:{ts}:{body.decode()}".encode()
        return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()

    async def test_verify_signature_valid(self) -> None:
        handler = SlackWebhookHandler()
        secret = "slack-secret"
        ts = int(time.time())
        payload = b'{"type": "event_callback"}'
        sig = self._make_sig(secret, ts, payload)
        headers = {
            "x-slack-signature": sig,
            "x-slack-request-timestamp": str(ts),
        }
        result = await handler.verify_signature(payload, headers, secret)
        assert result is True

    async def test_verify_signature_stale_timestamp(self) -> None:
        handler = SlackWebhookHandler()
        secret = "slack-secret"
        ts = int(time.time()) - 600  # 10 minutes ago
        payload = b'{"type": "event_callback"}'
        sig = self._make_sig(secret, ts, payload)
        headers = {
            "x-slack-signature": sig,
            "x-slack-request-timestamp": str(ts),
        }
        result = await handler.verify_signature(payload, headers, secret)
        assert result is False

    async def test_verify_signature_bad_hash(self) -> None:
        handler = SlackWebhookHandler()
        headers = {
            "x-slack-signature": "v0=badhash",
            "x-slack-request-timestamp": str(int(time.time())),
        }
        result = await handler.verify_signature(b"payload", headers, "secret")
        assert result is False

    async def test_parse_message_event(self) -> None:
        handler = SlackWebhookHandler()
        payload = json.dumps({
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "ts": "1609459200.000000",
                "thread_ts": "1609459200.000000",
            },
        }).encode()
        result = await handler.parse_payload(payload, {})
        assert result.source_name == "slack"
        assert len(result.affected_refs) == 1
        assert "C123" in result.affected_refs[0].uri

    async def test_parse_url_verification(self) -> None:
        handler = SlackWebhookHandler()
        payload = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
        result = await handler.parse_payload(payload, {})
        assert result.affected_refs == []

    async def test_parse_invalid_json_raises(self) -> None:
        handler = SlackWebhookHandler()
        with pytest.raises(ValueError):
            await handler.parse_payload(b"bad json", {})

    def test_connector_returns_handler(self) -> None:
        connector = SlackConnector()
        assert connector.webhook_handler() is not None


# ===========================================================================
# JIRA
# ===========================================================================


class TestJiraJQL:
    def test_jql_with_projects(self) -> None:
        cfg = JiraConfig(base_url=_JIRA_BASE, project_keys=["PROJ", "API"])
        jql = _build_jql(cfg)
        assert 'project in ("PROJ", "API")' in jql

    def test_jql_with_issue_types(self) -> None:
        cfg = JiraConfig(base_url=_JIRA_BASE, issue_types=["Bug", "Story"])
        jql = _build_jql(cfg)
        assert 'issuetype in ("Bug", "Story")' in jql

    def test_jql_with_custom_filter(self) -> None:
        cfg = JiraConfig(base_url=_JIRA_BASE, jql_filter="priority = High")
        jql = _build_jql(cfg)
        assert "(priority = High)" in jql

    def test_jql_empty_config(self) -> None:
        cfg = JiraConfig(base_url=_JIRA_BASE)
        jql = _build_jql(cfg)
        assert "ORDER BY" in jql

    def test_jql_combined(self) -> None:
        cfg = JiraConfig(
            base_url=_JIRA_BASE,
            project_keys=["PROJ"],
            issue_types=["Bug"],
            jql_filter="status != Done",
        )
        jql = _build_jql(cfg)
        assert "project" in jql
        assert "issuetype" in jql
        assert "status != Done" in jql


class TestJiraAdfToMarkdown:
    def test_plain_text(self) -> None:
        node = {"type": "text", "text": "hello"}
        assert _adf_to_markdown(node) == "hello"

    def test_bold_text(self) -> None:
        node = {
            "type": "text",
            "text": "bold",
            "marks": [{"type": "strong"}],
        }
        result = _adf_to_markdown(node)
        assert "**bold**" in result

    def test_em_text(self) -> None:
        node = {
            "type": "text",
            "text": "italic",
            "marks": [{"type": "em"}],
        }
        result = _adf_to_markdown(node)
        assert "_italic_" in result

    def test_code_mark(self) -> None:
        node = {
            "type": "text",
            "text": "fn()",
            "marks": [{"type": "code"}],
        }
        result = _adf_to_markdown(node)
        assert "`fn()`" in result

    def test_link_mark(self) -> None:
        node = {
            "type": "text",
            "text": "Click",
            "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
        }
        result = _adf_to_markdown(node)
        assert "[Click](https://example.com)" in result

    def test_paragraph(self) -> None:
        node = {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Paragraph text"}],
        }
        result = _adf_to_markdown(node)
        assert "Paragraph text" in result

    def test_heading(self) -> None:
        node = {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "Heading 2"}],
        }
        result = _adf_to_markdown(node)
        assert "## Heading 2" in result

    def test_code_block(self) -> None:
        node = {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [{"type": "text", "text": "x = 1"}],
        }
        result = _adf_to_markdown(node)
        assert "```python" in result
        assert "x = 1" in result

    def test_rule(self) -> None:
        node = {"type": "rule"}
        assert "---" in _adf_to_markdown(node)


class TestJiraValidate:
    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        respx.get(f"{_JIRA_BASE}/rest/api/3/myself").mock(
            return_value=httpx.Response(200, json={"accountId": "uid"})
        )
        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE)
        await connector.validate(config, {"email": "u@x.com", "api_token": "tok"})

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_unauthorized_raises(self) -> None:
        respx.get(f"{_JIRA_BASE}/rest/api/3/myself").mock(
            return_value=httpx.Response(401)
        )
        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE)
        with pytest.raises(PermissionError):
            await connector.validate(config, {"email": "u@x.com", "api_token": "bad"})

    @pytest.mark.asyncio
    async def test_validate_missing_secrets_raises(self) -> None:
        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE)
        with pytest.raises(ValueError):
            await connector.validate(config, {})

    @respx.mock
    @pytest.mark.asyncio
    async def test_validate_with_pat(self) -> None:
        respx.get(f"{_JIRA_BASE}/rest/api/3/myself").mock(
            return_value=httpx.Response(200, json={"accountId": "uid"})
        )
        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE)
        await connector.validate(config, {"pat": "myPAT"})


class TestJiraDiscover:
    def _make_issue(self, issue_id: str = "10001", key: str = "PROJ-1") -> dict[str, Any]:
        return {
            "id": issue_id,
            "key": key,
            "fields": {
                "summary": "Fix the bug",
                "status": {"name": "Open"},
                "priority": {"name": "High"},
                "issuetype": {"name": "Bug"},
                "assignee": {"displayName": "Alice"},
                "reporter": {"displayName": "Bob"},
                "labels": ["backend"],
                "created": "2024-01-01T00:00:00.000+0000",
                "updated": "2024-06-01T12:00:00.000+0000",
            },
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_yields_issue_refs(self) -> None:
        respx.get(f"{_JIRA_BASE}/rest/api/3/search").mock(
            return_value=httpx.Response(
                200,
                json={"issues": [self._make_issue()], "total": 1, "startAt": 0},
            )
        )
        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE, project_keys=["PROJ"])
        secrets = {"email": "u@x.com", "api_token": "tok"}

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, secrets):
            refs.append(ref)

        assert len(refs) == 1
        assert refs[0].metadata["issue_key"] == "PROJ-1"
        assert refs[0].metadata["summary"] == "Fix the bug"
        assert refs[0].metadata["status"] == "Open"

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_handles_api_error_gracefully(self) -> None:
        respx.get(f"{_JIRA_BASE}/rest/api/3/search").mock(
            return_value=httpx.Response(400, json={"errorMessages": ["bad jql"]})
        )
        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE)
        secrets = {"email": "u@x.com", "api_token": "tok"}

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, secrets):
            refs.append(ref)
        assert refs == []


class TestJiraFetch:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_returns_markdown_with_comments(self) -> None:
        issue_key = "PROJ-42"
        issue_data = {
            "id": "10042",
            "key": issue_key,
            "fields": {
                "summary": "Critical bug in payment",
                "status": {"name": "In Progress"},
                "priority": {"name": "Critical"},
                "issuetype": {"name": "Bug"},
                "assignee": {"displayName": "Dev"},
                "reporter": {"displayName": "PM"},
                "labels": [],
                "created": "2024-02-01T09:00:00.000+0000",
                "updated": "2024-02-05T15:00:00.000+0000",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Payment fails on checkout."}],
                        }
                    ],
                },
            },
        }
        comments_data = {
            "comments": [
                {
                    "author": {"displayName": "Alice"},
                    "created": "2024-02-02T10:00:00.000+0000",
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Can reproduce."}],
                            }
                        ],
                    },
                }
            ]
        }

        respx.get(f"{_JIRA_BASE}/rest/api/3/issue/{issue_key}").mock(
            return_value=httpx.Response(200, json=issue_data)
        )
        respx.get(f"{_JIRA_BASE}/rest/api/3/issue/{issue_key}/comment").mock(
            return_value=httpx.Response(200, json=comments_data)
        )

        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE)
        secrets = {"email": "u@x.com", "api_token": "tok"}
        ref = DocumentRef(
            external_id="sha1abc",
            uri=f"{_JIRA_BASE}/browse/{issue_key}",
            metadata={"issue_key": issue_key},
        )

        result = await connector.fetch(config, secrets, ref)

        assert result.content_type == "text/markdown"
        decoded = result.content_bytes.decode()
        assert "PROJ-42" in decoded
        assert "Critical bug in payment" in decoded
        assert "Payment fails on checkout." in decoded
        assert "Can reproduce." in decoded

    @pytest.mark.asyncio
    async def test_fetch_missing_issue_key_raises(self) -> None:
        connector = JiraConnector()
        config = JiraConfig(base_url=_JIRA_BASE)
        ref = DocumentRef(external_id="x", uri="https://jira.example.com/browse/X", metadata={})
        with pytest.raises(ValueError, match="issue_key"):
            await connector.fetch(config, {"email": "u@x.com", "api_token": "tok"}, ref)


class TestJiraWebhook:
    async def test_verify_signature_valid(self) -> None:
        handler = JiraWebhookHandler()
        secret = "jira-secret"
        payload = b'{"webhookEvent": "jira:issue_created"}'
        sig = hmac.new(secret.encode(), payload, "sha256").hexdigest()
        headers = {"x-hub-signature": f"sha256={sig}"}
        result = await handler.verify_signature(payload, headers, secret)
        assert result is True

    async def test_verify_signature_no_header_no_secret(self) -> None:
        handler = JiraWebhookHandler()
        # No signature header and no configured secret = accept
        result = await handler.verify_signature(b"payload", {}, "")
        assert result is True

    async def test_verify_signature_bad_hash(self) -> None:
        handler = JiraWebhookHandler()
        headers = {"x-hub-signature": "sha256=badhash"}
        result = await handler.verify_signature(b"payload", headers, "secret")
        assert result is False

    async def test_parse_issue_created(self) -> None:
        handler = JiraWebhookHandler()
        payload = json.dumps({
            "webhookEvent": "jira:issue_created",
            "issue": {
                "id": "10001",
                "key": "PROJ-1",
                "fields": {"updated": "2024-01-01T00:00:00.000+0000"},
            },
        }).encode()
        result = await handler.parse_payload(payload, {})
        assert result.source_name == "jira"
        assert len(result.affected_refs) == 1
        assert result.affected_refs[0].metadata["issue_key"] == "PROJ-1"

    async def test_parse_invalid_json_raises(self) -> None:
        handler = JiraWebhookHandler()
        with pytest.raises(ValueError):
            await handler.parse_payload(b"not json", {})

    def test_connector_returns_handler(self) -> None:
        connector = JiraConnector()
        assert connector.webhook_handler() is not None
