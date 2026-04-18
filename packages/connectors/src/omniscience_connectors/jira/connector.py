"""Jira source connector.

Discovers Jira issues via JQL search and fetches issue body + comments.
Supports Jira Cloud (API token + email) and Jira Server/Data Center (PAT).
Implements webhooks for issue create/update/comment events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
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

__all__ = ["JiraConfig", "JiraConnector"]

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50

# Jira field names requested during discover (kept under line length limit)
_DISCOVER_FIELDS = (
    "summary,status,priority,issuetype,updated,created,"
    "assignee,reporter,labels"
)


class JiraConfig(BaseModel):
    """Public configuration for the Jira connector (no secrets)."""

    base_url: str
    """Base URL of the Jira instance (e.g. ``https://mycompany.atlassian.net``)."""

    project_keys: list[str] = Field(default_factory=list)
    """Jira project keys to sync (e.g. ``["PROJ", "BACKEND"]``).
    Empty = query all projects accessible to the credentials."""

    jql_filter: str = ""
    """Additional JQL clause appended to the base project filter.
    Example: ``"priority = High AND status != Done"``."""

    issue_types: list[str] = Field(default_factory=list)
    """Issue types to include (e.g. ``["Bug", "Story"]``).
    Empty = include all issue types."""

    webhook_secret: str | None = None
    """HMAC secret for verifying Jira webhook payloads."""


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


class JiraWebhookHandler(WebhookHandler):
    """Handler for Jira webhook events (issue create/update/comment).

    Jira Cloud signs webhooks with HMAC-SHA256 in the
    ``X-Hub-Signature`` header (``sha256=<hex>``).
    Jira Server/DC sends an optional secret query parameter — the
    framework passes the full raw body for verification.
    """

    _SIG_HEADER = "x-hub-signature"

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        lower = {k.lower(): v for k, v in headers.items()}
        sig_header = lower.get(self._SIG_HEADER, "")
        if not sig_header:
            # No signature header — accept if no secret is configured
            return not secret
        if not sig_header.startswith("sha256="):
            return False
        provided_hex = sig_header[len("sha256="):]
        expected_hex = hmac.new(secret.encode(), payload, "sha256").hexdigest()
        return hmac.compare_digest(expected_hex, provided_hex)

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        lower_headers = {k.lower(): v for k, v in headers.items()}
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Jira webhook: invalid JSON: {exc}") from exc

        affected: list[DocumentRef] = []
        event_type = str(data.get("webhookEvent", ""))
        issue_data = data.get("issue")

        if isinstance(issue_data, dict):
            issue_id = str(issue_data.get("id", ""))
            issue_key = str(issue_data.get("key", ""))
            if issue_id:
                fields = issue_data.get("fields", {})
                updated_raw = (
                    fields.get("updated", "") if isinstance(fields, dict) else ""
                )
                # sha1 used only as a compact fingerprint, not for security
                external_id = hashlib.sha1(  # noqa: S324
                    f"{issue_id}:{updated_raw}".encode()
                ).hexdigest()
                affected.append(
                    DocumentRef(
                        external_id=external_id,
                        uri=issue_key,
                        metadata={
                            "issue_id": issue_id,
                            "issue_key": issue_key,
                            "event_type": event_type,
                        },
                    )
                )

        return WebhookPayload(
            source_name="jira",
            affected_refs=affected,
            raw_headers=lower_headers,
        )


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _build_auth_headers(secrets: dict[str, str]) -> dict[str, str]:
    """Build HTTP auth headers for Jira Cloud or Server."""
    pat = secrets.get("pat", "")
    if pat:
        return {"Authorization": f"Bearer {pat}"}

    api_token = secrets.get("api_token", "")
    email = secrets.get("email", "")
    if api_token and email:
        import base64

        creds = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}

    raise ValueError(
        "Jira secrets must contain either 'pat' (Server) "
        "or both 'api_token' and 'email' (Cloud)."
    )


def _parse_jira_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    # Jira dates: "2023-11-15T10:30:00.000+0000"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Content conversion
# ---------------------------------------------------------------------------


def _adf_to_markdown(node: dict[str, Any], depth: int = 0) -> str:
    """Recursively convert Atlassian Document Format (ADF) nodes to Markdown.

    ADF is used by Jira Cloud for rich text fields.
    """
    node_type = node.get("type", "")
    content: list[dict[str, Any]] = node.get("content", [])
    text: str = node.get("text", "")
    attrs: dict[str, Any] = node.get("attrs", {})

    if node_type == "text":
        marks: list[dict[str, Any]] = node.get("marks", [])
        result = text
        for mark in marks:
            mt = mark.get("type", "")
            if mt == "strong":
                result = f"**{result}**"
            elif mt == "em":
                result = f"_{result}_"
            elif mt == "code":
                result = f"`{result}`"
            elif mt == "strike":
                result = f"~~{result}~~"
            elif mt == "link":
                href = mark.get("attrs", {}).get("href", "")
                result = f"[{result}]({href})"
        return result

    if node_type == "doc":
        return "\n\n".join(_adf_to_markdown(c, depth) for c in content)

    if node_type == "paragraph":
        inner = "".join(_adf_to_markdown(c, depth) for c in content)
        return f"{inner}\n\n"

    if node_type == "heading":
        level = min(attrs.get("level", 1), 6)
        prefix = "#" * level
        inner = "".join(_adf_to_markdown(c, depth) for c in content)
        return f"{prefix} {inner}\n\n"

    if node_type == "bulletList":
        items = [_adf_to_markdown(c, depth + 1) for c in content]
        return "".join(items)

    if node_type == "orderedList":
        lines: list[str] = []
        for i, item in enumerate(content, start=1):
            inner = "".join(
                _adf_to_markdown(c, depth + 1) for c in item.get("content", [])
            )
            lines.append(f"{i}. {inner.strip()}\n")
        return "".join(lines) + "\n"

    if node_type == "listItem":
        indent = "  " * (depth - 1)
        inner = "".join(_adf_to_markdown(c, depth) for c in content)
        return f"{indent}- {inner.strip()}\n"

    if node_type == "codeBlock":
        lang = attrs.get("language", "")
        inner = "".join(_adf_to_markdown(c, depth) for c in content)
        return f"```{lang}\n{inner}\n```\n\n"

    if node_type == "blockquote":
        inner = "".join(_adf_to_markdown(c, depth) for c in content)
        quoted = "\n".join(f"> {line}" for line in inner.splitlines())
        return f"{quoted}\n\n"

    if node_type == "rule":
        return "---\n\n"

    if node_type == "hardBreak":
        return "\n"

    if node_type == "inlineCard":
        url = attrs.get("url", "")
        return f"[{url}]({url})"

    if node_type == "mention":
        display = attrs.get("text", attrs.get("id", ""))
        return f"@{display}"

    # Tables (simplified)
    if node_type == "table":
        rows = [_adf_to_markdown(c, depth) for c in content]
        return "".join(rows) + "\n"

    if node_type in ("tableRow", "tableHeader", "tableCell"):
        cells = [_adf_to_markdown(c, depth) for c in content]
        return "| " + " | ".join(c.strip() for c in cells) + " |\n"

    # Fallback: recurse into children
    return "".join(_adf_to_markdown(c, depth) for c in content)


def _field_to_text(value: Any) -> str:
    """Convert a Jira field value to a plain string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # ADF document
        if value.get("type") == "doc":
            return _adf_to_markdown(value).strip()
        # Simple value objects (status, priority, issuetype, user, etc.)
        for key in ("displayName", "name", "value", "key"):
            if key in value:
                return str(value[key])
        return str(value)
    if isinstance(value, list):
        return ", ".join(_field_to_text(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class JiraConnector(Connector):
    """Source connector for Atlassian Jira (Cloud and Server/DC).

    Discovers issues via JQL search and fetches issue body + comments.
    """

    connector_type: ClassVar[str] = "jira"
    config_schema: ClassVar[type[BaseModel]] = JiraConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify connectivity by fetching the current user."""
        cfg: JiraConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.base_url.rstrip("/")

        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await client.get(f"{base}/rest/api/3/myself")
            if resp.status_code == 401:
                raise PermissionError(
                    "Jira authentication failed — check api_token/email or pat."
                )
            resp.raise_for_status()

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield DocumentRefs for all matching Jira issues via JQL."""
        cfg: JiraConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.base_url.rstrip("/")

        jql = _build_jql(cfg)
        start_at = 0

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            while True:
                params: dict[str, Any] = {
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": _PAGE_SIZE,
                    "fields": _DISCOVER_FIELDS,
                }
                try:
                    resp = await client.get(
                        f"{base}/rest/api/3/search", params=params
                    )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "jira.discover.api_error",
                        extra={"status": exc.response.status_code, "jql": jql},
                    )
                    break

                data = resp.json()
                issues: list[dict[str, Any]] = data.get("issues", [])

                for issue in issues:
                    ref = _issue_to_ref(issue, base)
                    if ref is not None:
                        yield ref

                total = data.get("total", 0)
                start_at += len(issues)
                if start_at >= total or not issues:
                    break

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch a Jira issue body + comments as Markdown."""
        cfg: JiraConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.base_url.rstrip("/")

        issue_key = ref.metadata.get("issue_key", "")
        if not issue_key:
            raise ValueError(
                f"DocumentRef missing 'issue_key' in metadata: {ref.uri!r}"
            )

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            # Fetch full issue with all fields
            issue_resp = await client.get(
                f"{base}/rest/api/3/issue/{issue_key}",
                params={"expand": "renderedFields,names"},
            )
            issue_resp.raise_for_status()
            issue_data = issue_resp.json()

            # Fetch comments
            comments_resp = await client.get(
                f"{base}/rest/api/3/issue/{issue_key}/comment",
                params={"maxResults": 500},
            )
            comments_resp.raise_for_status()
            comments_data = comments_resp.json()

        markdown = _issue_to_markdown(issue_data, comments_data, base)
        return FetchedDocument(
            ref=ref,
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        return JiraWebhookHandler()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_jql(cfg: JiraConfig) -> str:
    """Build a JQL query from the connector configuration."""
    clauses: list[str] = []

    if cfg.project_keys:
        keys = ", ".join(f'"{k}"' for k in cfg.project_keys)
        clauses.append(f"project in ({keys})")

    if cfg.issue_types:
        types = ", ".join(f'"{t}"' for t in cfg.issue_types)
        clauses.append(f"issuetype in ({types})")

    if cfg.jql_filter:
        clauses.append(f"({cfg.jql_filter})")

    if not clauses:
        return "ORDER BY updated DESC"

    return " AND ".join(clauses) + " ORDER BY updated DESC"


def _issue_to_ref(issue: dict[str, Any], base_url: str) -> DocumentRef | None:
    """Convert a Jira issue search result to a DocumentRef."""
    issue_id = str(issue.get("id", ""))
    issue_key = str(issue.get("key", ""))
    if not issue_id or not issue_key:
        return None

    fields: dict[str, Any] = issue.get("fields", {}) or {}
    updated_raw = str(fields.get("updated", ""))
    updated_at = _parse_jira_date(updated_raw)

    # sha1 used only as a compact fingerprint, not for security
    external_id = hashlib.sha1(  # noqa: S324
        f"{issue_id}:{updated_raw}".encode()
    ).hexdigest()
    uri = f"{base_url.rstrip('/')}/browse/{issue_key}"

    return DocumentRef(
        external_id=external_id,
        uri=uri,
        updated_at=updated_at,
        metadata={
            "issue_id": issue_id,
            "issue_key": issue_key,
            "summary": _field_to_text(fields.get("summary")),
            "status": _field_to_text(fields.get("status")),
            "issue_type": _field_to_text(fields.get("issuetype")),
            "priority": _field_to_text(fields.get("priority")),
            "labels": fields.get("labels", []),
        },
    )


def _issue_to_markdown(
    issue: dict[str, Any],
    comments_data: dict[str, Any],
    base_url: str,
) -> str:
    """Format a full Jira issue + comments as Markdown."""
    key = str(issue.get("key", ""))
    fields: dict[str, Any] = issue.get("fields", {}) or {}

    summary = _field_to_text(fields.get("summary"))
    status = _field_to_text(fields.get("status"))
    priority = _field_to_text(fields.get("priority"))
    issue_type = _field_to_text(fields.get("issuetype"))
    assignee = _field_to_text(fields.get("assignee"))
    reporter = _field_to_text(fields.get("reporter"))
    labels: list[str] = fields.get("labels", [])
    created = str(fields.get("created", ""))
    updated = str(fields.get("updated", ""))

    # Description — can be ADF (dict) or plain string
    description_raw = fields.get("description") or (
        issue.get("renderedFields", {}) or {}
    ).get("description", "")
    if isinstance(description_raw, dict):
        description = _adf_to_markdown(description_raw).strip()
    else:
        description = str(description_raw).strip()

    url = f"{base_url.rstrip('/')}/browse/{key}"

    parts = [
        f"# [{key}] {summary}",
        f"**URL**: {url}",
        f"**Type**: {issue_type} | **Status**: {status} | **Priority**: {priority}",
        f"**Assignee**: {assignee} | **Reporter**: {reporter}",
        f"**Created**: {created} | **Updated**: {updated}",
    ]
    if labels:
        parts.append(f"**Labels**: {', '.join(labels)}")

    if description:
        parts.append("\n## Description\n")
        parts.append(description)

    # Comments
    comments: list[dict[str, Any]] = comments_data.get("comments", [])
    if comments:
        parts.append("\n## Comments\n")
        for comment in comments:
            author = _field_to_text(comment.get("author"))
            created_at = str(comment.get("created", ""))
            body_raw = comment.get("body")
            if isinstance(body_raw, dict):
                body = _adf_to_markdown(body_raw).strip()
            else:
                body = str(body_raw or "").strip()
            parts.append(f"**{author}** ({created_at}):\n{body}\n")

    return "\n\n".join(parts)
