"""Confluence source connector.

Supports Confluence Cloud (API token + email) and Confluence Server/Data Center
(personal access token).  Discovers pages and blog posts via CQL and converts
Confluence storage format (XHTML) to plain Markdown.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
    WebhookPayload,
)

__all__ = ["ConfluenceConfig", "ConfluenceConnector"]

logger = logging.getLogger(__name__)

# Confluence content types supported
ContentType = Literal["page", "blogpost"]

# Max results per page for REST API pagination
_PAGE_SIZE = 50

# Macro regex broken out to stay within line length limit
_MACRO_PATTERN = re.compile(
    r'<ac:structured-macro[^>]*ac:name="code"[^>]*>.*?</ac:structured-macro>',
    re.DOTALL,
)

# Patterns for converting block-level elements
_TAG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Headings
    (re.compile(r"<h1[^>]*>"), "# "),
    (re.compile(r"<h2[^>]*>"), "## "),
    (re.compile(r"<h3[^>]*>"), "### "),
    (re.compile(r"<h4[^>]*>"), "#### "),
    (re.compile(r"<h5[^>]*>"), "##### "),
    (re.compile(r"<h6[^>]*>"), "###### "),
    (re.compile(r"</h\d>"), "\n"),
    # Paragraphs and divs
    (re.compile(r"<p[^>]*>"), ""),
    (re.compile(r"</p>"), "\n\n"),
    (re.compile(r"<div[^>]*>"), ""),
    (re.compile(r"</div>"), "\n"),
    # Line breaks
    (re.compile(r"<br\s*/?>"), "\n"),
    # List items
    (re.compile(r"<li[^>]*>"), "- "),
    (re.compile(r"</li>"), "\n"),
    # Code blocks (Confluence macro or pre)
    (_MACRO_PATTERN, "```\n```"),
    (re.compile(r"<pre[^>]*>"), "```\n"),
    (re.compile(r"</pre>"), "\n```\n"),
    (re.compile(r"<code[^>]*>"), "`"),
    (re.compile(r"</code>"), "`"),
    # Blockquote
    (re.compile(r"<blockquote[^>]*>"), "> "),
    (re.compile(r"</blockquote>"), "\n"),
    # Horizontal rule
    (re.compile(r"<hr\s*/?>"), "\n---\n"),
    # Tables — crude extraction (strips table markup)
    (re.compile(r"<th[^>]*>"), "| "),
    (re.compile(r"</th>"), " |"),
    (re.compile(r"<td[^>]*>"), "| "),
    (re.compile(r"</td>"), " |"),
    (re.compile(r"<tr[^>]*>"), ""),
    (re.compile(r"</tr>"), " |\n"),
    (re.compile(r"<t(?:head|body|foot)[^>]*>|</t(?:head|body|foot)>"), ""),
    (re.compile(r"<table[^>]*>|</table>"), "\n"),
]

# Strip remaining XML/HTML tags (including Confluence macros)
_STRIP_TAGS = re.compile(r"<[^>]+>")

# Collapse excessive blank lines
_EXCESS_BLANK = re.compile(r"\n{3,}")

# HTML entity map (common ones; full decoding done via stdlib)
_HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
    "&mdash;": "\u2014",
    # Use the unicode escape rather than the literal EN DASH character
    "&ndash;": "\u2013",
    "&hellip;": "\u2026",
}


class ConfluenceConfig(BaseModel):
    """Public configuration for the Confluence connector (no secrets)."""

    base_url: str
    """Base URL of the Confluence instance (e.g. ``https://mycompany.atlassian.net/wiki``)."""

    space_keys: list[str] = Field(default_factory=list)
    """Confluence space keys to sync.  Empty list discovers all accessible spaces."""

    content_types: list[ContentType] = Field(default=["page"])
    """Content types to discover: ``page`` and/or ``blogpost``."""

    include_labels: list[str] = Field(default_factory=list)
    """Only include content tagged with at least one of these labels.  Empty = no filter."""

    exclude_labels: list[str] = Field(default_factory=list)
    """Exclude content tagged with any of these labels."""

    webhook_secret: str | None = None
    """HMAC secret for verifying Confluence webhook payloads."""


def storage_to_markdown(storage_html: str) -> str:
    """Convert Confluence storage format (XHTML) to approximate Markdown.

    This is intentionally a best-effort conversion.  Full fidelity would
    require a full XHTML parser; the goal here is to produce human-readable
    text that preserves the document's informational content.
    """
    text = storage_html

    # Apply block-level tag conversions
    for pattern, replacement in _TAG_PATTERNS:
        text = pattern.sub(replacement, text)

    # Strip all remaining tags
    text = _STRIP_TAGS.sub("", text)

    # Decode HTML entities
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)

    # Decode numeric entities
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)

    # Normalise whitespace
    text = _EXCESS_BLANK.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _build_auth_headers(secrets: dict[str, str]) -> dict[str, str]:
    """Build HTTP Authorization headers from secrets.

    Supports:
    - Cloud: ``api_token`` + ``email`` -> HTTP Basic with token as password
    - Server: ``pat`` (personal access token) -> Bearer token
    """
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
        "Confluence secrets must contain either 'pat' (Server) "
        "or both 'api_token' and 'email' (Cloud)."
    )


def _content_url(base_url: str, content_id: str) -> str:
    """Return the human-readable URL for a Confluence content item."""
    base = base_url.rstrip("/")
    return f"{base}/pages/viewpage.action?pageId={content_id}"


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------


class ConfluenceWebhookHandler(WebhookHandler):
    """Handler for Confluence webhook events (page create/update).

    Confluence sends an HMAC-SHA256 signature in the ``X-Hub-Signature`` header
    using the format ``sha256=<hex>``.
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
        if not sig_header.startswith("sha256="):
            return False
        provided_hex = sig_header[len("sha256=") :]
        expected_hex = hmac.new(secret.encode(), payload, "sha256").hexdigest()
        return hmac.compare_digest(expected_hex, provided_hex)

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Confluence webhook: invalid JSON: {exc}") from exc

        lower_headers = {k.lower(): v for k, v in headers.items()}
        affected: list[DocumentRef] = []

        page = data.get("page") or data.get("content")
        if isinstance(page, dict):
            page_id = str(page.get("id", ""))
            page_title = str(page.get("title", ""))
            space_key = ""
            space = page.get("space")
            if isinstance(space, dict):
                space_key = str(space.get("key", ""))
            if page_id:
                affected.append(
                    DocumentRef(
                        external_id=page_id,
                        uri=_content_url("", page_id),
                        metadata={
                            "title": page_title,
                            "space_key": space_key,
                            "event": data.get("webhookEvent", ""),
                        },
                    )
                )

        return WebhookPayload(
            source_name="confluence",
            affected_refs=affected,
            raw_headers=lower_headers,
        )


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class ConfluenceConnector(Connector):
    """Source connector for Atlassian Confluence (Cloud and Server/DC).

    Discovers pages and blog posts via the Confluence REST API and converts
    storage format to Markdown for downstream ingestion.
    """

    connector_type: ClassVar[str] = "confluence"
    config_schema: ClassVar[type[BaseModel]] = ConfluenceConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify connectivity by fetching current user info."""
        cfg: ConfluenceConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.base_url.rstrip("/")

        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await client.get(f"{base}/rest/api/user/current")
            if resp.status_code == 401:
                raise PermissionError(
                    "Confluence authentication failed — check api_token/email or pat."
                )
            resp.raise_for_status()

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield DocumentRefs for all matching Confluence content."""
        cfg: ConfluenceConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.base_url.rstrip("/")
        url = f"{base}/rest/api/content"

        spaces_to_query = (
            cfg.space_keys if cfg.space_keys else await self._list_space_keys(base, headers)
        )

        for space_key in spaces_to_query:
            for content_type in cfg.content_types:
                cql = f'type="{content_type}" AND space="{space_key}"'
                if cfg.include_labels:
                    label_clause = " OR ".join(f'label="{lbl}"' for lbl in cfg.include_labels)
                    cql += f" AND ({label_clause})"

                start = 0
                async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
                    while True:
                        params: dict[str, Any] = {
                            "cql": cql,
                            "start": start,
                            "limit": _PAGE_SIZE,
                            "expand": "version,metadata.labels,space",
                        }
                        try:
                            resp = await client.get(url, params=params)
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            logger.warning(
                                "confluence.discover.api_error",
                                extra={
                                    "space": space_key,
                                    "status": exc.response.status_code,
                                },
                            )
                            break

                        data = resp.json()
                        results: list[dict[str, Any]] = data.get("results", [])

                        for item in results:
                            item_labels: list[str] = [
                                lbl["name"]
                                for lbl in (
                                    item.get("metadata", {}).get("labels", {}).get("results", [])
                                )
                            ]
                            # Apply exclude label filter
                            if cfg.exclude_labels and any(
                                lbl in cfg.exclude_labels for lbl in item_labels
                            ):
                                continue

                            content_id = str(item.get("id", ""))
                            title = str(item.get("title", ""))
                            version_num = item.get("version", {}).get("number", 0)
                            updated_raw = item.get("version", {}).get("when")
                            updated_at: datetime | None = None
                            if updated_raw:
                                with contextlib.suppress(ValueError):
                                    updated_at = datetime.fromisoformat(
                                        updated_raw.replace("Z", "+00:00")
                                    )

                            # Use content_id + version for stable external_id
                            # sha1 is used only as a compact fingerprint, not for security
                            external_id = hashlib.sha1(  # noqa: S324
                                f"{content_id}:{version_num}".encode()
                            ).hexdigest()

                            yield DocumentRef(
                                external_id=external_id,
                                uri=_content_url(base, content_id),
                                updated_at=updated_at,
                                metadata={
                                    "content_id": content_id,
                                    "title": title,
                                    "content_type": content_type,
                                    "space_key": space_key,
                                    "labels": item_labels,
                                    "version": version_num,
                                },
                            )

                        # Pagination
                        size = data.get("size", 0)
                        total = data.get("totalSize", size)
                        start += size
                        if start >= total or size == 0:
                            break

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch a Confluence page and return its content as Markdown."""
        cfg: ConfluenceConfig = config  # type: ignore[assignment]
        headers = _build_auth_headers(secrets)
        base = cfg.base_url.rstrip("/")

        content_id = ref.metadata.get("content_id", "")
        if not content_id:
            raise ValueError(f"DocumentRef is missing 'content_id' in metadata: {ref.uri!r}")

        url = f"{base}/rest/api/content/{content_id}"
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            resp = await client.get(url, params={"expand": "body.storage,version,space"})
            resp.raise_for_status()

        data = resp.json()
        storage_html: str = data.get("body", {}).get("storage", {}).get("value", "")
        title: str = data.get("title", "")

        markdown = f"# {title}\n\n{storage_to_markdown(storage_html)}"
        content_bytes = markdown.encode("utf-8")

        return FetchedDocument(
            ref=ref,
            content_bytes=content_bytes,
            content_type="text/markdown",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        return ConfluenceWebhookHandler()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _list_space_keys(self, base: str, headers: dict[str, str]) -> list[str]:
        """Return all accessible space keys from the Confluence instance."""
        keys: list[str] = []
        start = 0
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            while True:
                resp = await client.get(
                    f"{base}/rest/api/space",
                    params={"start": start, "limit": _PAGE_SIZE, "type": "global"},
                )
                resp.raise_for_status()
                data = resp.json()
                results: list[dict[str, Any]] = data.get("results", [])
                for space in results:
                    key = space.get("key")
                    if isinstance(key, str):
                        keys.append(key)
                size = data.get("size", 0)
                total = data.get("totalSize", size)
                start += size
                if start >= total or size == 0:
                    break
        return keys
