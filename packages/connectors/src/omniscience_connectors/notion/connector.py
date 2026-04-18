"""Notion source connector.

Discovers pages and database entries via the Notion API and converts
Notion block trees to Markdown.  Uses polling only (no webhook support).
"""

from __future__ import annotations

import hashlib
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
)

__all__ = ["NotionConfig", "NotionConnector"]

logger = logging.getLogger(__name__)

_NOTION_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_PAGE_SIZE = 100


class NotionConfig(BaseModel):
    """Public configuration for the Notion connector (no secrets)."""

    database_ids: list[str] = Field(default_factory=list)
    """Notion database IDs to query.  UUIDs with or without dashes."""

    page_ids: list[str] = Field(default_factory=list)
    """Individual Notion page IDs to include."""

    include_properties: list[str] = Field(default_factory=list)
    """Database property names to include in metadata.  Empty = include all."""


# ---------------------------------------------------------------------------
# Notion blocks -> Markdown
# ---------------------------------------------------------------------------


def _rich_text_to_str(rich_texts: list[dict[str, Any]]) -> str:
    """Concatenate plain_text values from a Notion rich_text array."""
    return "".join(rt.get("plain_text", "") for rt in rich_texts)


def _block_to_markdown(block: dict[str, Any], depth: int = 0) -> str:
    """Convert a single Notion block dict to a Markdown string.

    *depth* is used for nested lists (children are handled by the caller).
    """
    block_type: str = block.get("type", "")
    data: dict[str, Any] = block.get(block_type, {})
    rich_texts: list[dict[str, Any]] = data.get("rich_text", [])
    text = _rich_text_to_str(rich_texts)
    indent = "  " * depth

    if block_type == "paragraph":
        return f"{text}\n\n" if text else ""

    if block_type in ("heading_1", "heading_2", "heading_3"):
        level = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}[block_type]
        return f"{level} {text}\n\n"

    if block_type == "bulleted_list_item":
        return f"{indent}- {text}\n"

    if block_type == "numbered_list_item":
        return f"{indent}1. {text}\n"

    if block_type == "to_do":
        checked = "x" if data.get("checked") else " "
        return f"{indent}- [{checked}] {text}\n"

    if block_type == "toggle":
        return f"{indent}> {text}\n"

    if block_type == "quote":
        return f"> {text}\n\n"

    if block_type == "callout":
        icon = ""
        emoji_data = data.get("icon")
        if isinstance(emoji_data, dict) and emoji_data.get("type") == "emoji":
            icon = emoji_data.get("emoji", "") + " "
        return f"> {icon}{text}\n\n"

    if block_type == "code":
        lang = data.get("language", "")
        return f"```{lang}\n{text}\n```\n\n"

    if block_type == "divider":
        return "---\n\n"

    if block_type in ("image", "video", "file", "pdf"):
        src_data = data.get("external") or data.get("file") or {}
        src_url = src_data.get("url", "")
        caption_texts: list[dict[str, Any]] = data.get("caption", [])
        caption = _rich_text_to_str(caption_texts)
        alt = caption or block_type
        if block_type == "image":
            return f"![{alt}]({src_url})\n\n"
        return f"[{alt}]({src_url})\n\n"

    if block_type == "table_row":
        cells: list[list[dict[str, Any]]] = data.get("cells", [])
        cell_texts = [_rich_text_to_str(c) for c in cells]
        return "| " + " | ".join(cell_texts) + " |\n"

    if block_type == "child_page":
        title = data.get("title", "")
        return f"**[Page: {title}]**\n\n"

    if block_type == "child_database":
        title = data.get("title", "")
        return f"**[Database: {title}]**\n\n"

    # Unknown block type — emit a placeholder so content is not silently lost
    return f"<!-- {block_type} -->\n" if block_type else ""


def blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Convert a flat list of Notion block dicts to a Markdown string.

    Nested blocks (children) are indented by one level.  This function does
    not fetch children from the API; callers must pre-flatten the tree.
    """
    parts: list[str] = []
    depth = 0
    list_types = {"bulleted_list_item", "numbered_list_item", "to_do"}
    prev_type = ""

    for block in blocks:
        block_type = block.get("type", "")
        # Reset indent depth when transitioning into or out of a list
        if (block_type in list_types) != (prev_type in list_types):
            depth = 0

        parts.append(_block_to_markdown(block, depth=depth))
        prev_type = block_type

    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _auth_headers(secrets: dict[str, str]) -> dict[str, str]:
    token = secrets.get("integration_token", "")
    if not token:
        raise ValueError("Notion secrets must contain 'integration_token'.")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _page_title(page: dict[str, Any]) -> str:
    """Extract the title from a Notion page object."""
    props = page.get("properties", {})
    for prop_val in props.values():
        if isinstance(prop_val, dict) and prop_val.get("type") == "title":
            rich_texts = prop_val.get("title", [])
            title = _rich_text_to_str(rich_texts)
            if title:
                return title
    return str(page.get("id", ""))


def _page_metadata(
    page: dict[str, Any], include_properties: list[str]
) -> dict[str, Any]:
    """Extract selected properties as metadata."""
    meta: dict[str, Any] = {
        "page_id": page.get("id", ""),
        "created_time": page.get("created_time", ""),
        "last_edited_time": page.get("last_edited_time", ""),
        "url": page.get("url", ""),
    }
    props = page.get("properties", {})
    for key, val in props.items():
        if include_properties and key not in include_properties:
            continue
        prop_type = val.get("type", "")
        # Serialise simple property types into the metadata dict
        if prop_type == "rich_text":
            meta[key] = _rich_text_to_str(val.get("rich_text", []))
        elif prop_type == "title":
            meta[key] = _rich_text_to_str(val.get("title", []))
        elif prop_type == "select":
            sel = val.get("select")
            meta[key] = sel.get("name", "") if isinstance(sel, dict) else ""
        elif prop_type == "multi_select":
            meta[key] = [s.get("name", "") for s in val.get("multi_select", [])]
        elif prop_type == "checkbox":
            meta[key] = val.get("checkbox", False)
        elif prop_type in ("number", "date", "url", "email", "phone_number"):
            meta[key] = val.get(prop_type)
        # Complex types (relations, rollups, formulas) are skipped intentionally
    return meta


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class NotionConnector(Connector):
    """Source connector for Notion workspaces.

    Discovers pages from configured databases and individual page IDs.
    Converts Notion block trees to Markdown.  Webhook support is not
    available in the Notion API — polling is used instead.
    """

    connector_type: ClassVar[str] = "notion"
    config_schema: ClassVar[type[BaseModel]] = NotionConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the integration token works by calling /v1/users/me."""
        headers = _auth_headers(secrets)
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            resp = await client.get(f"{_NOTION_API_BASE}/users/me")
            if resp.status_code == 401:
                raise PermissionError(
                    "Notion authentication failed — check integration_token."
                )
            resp.raise_for_status()

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield DocumentRefs for all Notion pages in configured databases + individual IDs."""
        cfg: NotionConfig = config  # type: ignore[assignment]
        headers = _auth_headers(secrets)

        # Discover from databases
        for db_id in cfg.database_ids:
            async for ref in self._discover_database(db_id, cfg, headers):
                yield ref

        # Discover individual pages
        for page_id in cfg.page_ids:
            page_ref = await self._page_ref(page_id, cfg, headers)
            if page_ref is not None:
                yield page_ref

        # If neither databases nor pages are specified, use /v1/search
        if not cfg.database_ids and not cfg.page_ids:
            async for ref in self._search_all(cfg, headers):
                yield ref

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch a Notion page's block tree and return it as Markdown."""
        headers = _auth_headers(secrets)

        page_id = ref.metadata.get("page_id", "")
        if not page_id:
            raise ValueError(f"DocumentRef missing 'page_id' in metadata: {ref.uri!r}")

        # Fetch page metadata for the title
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            page_resp = await client.get(f"{_NOTION_API_BASE}/pages/{page_id}")
            page_resp.raise_for_status()
            page_data = page_resp.json()

        title = _page_title(page_data)

        # Fetch all blocks (paginated)
        blocks = await self._fetch_all_blocks(page_id, headers)

        md_body = blocks_to_markdown(blocks)
        markdown = f"# {title}\n\n{md_body}" if md_body else f"# {title}\n"

        return FetchedDocument(
            ref=ref,
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        """Notion does not support webhooks — polling only."""
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _discover_database(
        self,
        db_id: str,
        cfg: NotionConfig,
        headers: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield DocumentRefs for all pages in a Notion database."""
        url = f"{_NOTION_API_BASE}/databases/{db_id}/query"
        cursor: str | None = None

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            while True:
                body: dict[str, Any] = {"page_size": _PAGE_SIZE}
                if cursor:
                    body["start_cursor"] = cursor

                try:
                    resp = await client.post(url, json=body)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "notion.discover.database_error",
                        extra={"db_id": db_id, "status": exc.response.status_code},
                    )
                    return

                data = resp.json()
                for page in data.get("results", []):
                    page_ref = _page_to_ref(page, cfg.include_properties)
                    if page_ref is not None:
                        yield page_ref

                if data.get("has_more"):
                    cursor = data.get("next_cursor")
                else:
                    break

    async def _page_ref(
        self,
        page_id: str,
        cfg: NotionConfig,
        headers: dict[str, str],
    ) -> DocumentRef | None:
        """Fetch a single page by ID and return its DocumentRef."""
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            try:
                resp = await client.get(f"{_NOTION_API_BASE}/pages/{page_id}")
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "notion.discover.page_error",
                    extra={"page_id": page_id, "status": exc.response.status_code},
                )
                return None
        return _page_to_ref(resp.json(), cfg.include_properties)

    async def _search_all(
        self,
        cfg: NotionConfig,
        headers: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Use /v1/search to find all accessible pages."""
        url = f"{_NOTION_API_BASE}/search"
        cursor: str | None = None

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            while True:
                body: dict[str, Any] = {
                    "filter": {"property": "object", "value": "page"},
                    "page_size": _PAGE_SIZE,
                }
                if cursor:
                    body["start_cursor"] = cursor

                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()

                for page in data.get("results", []):
                    page_ref = _page_to_ref(page, cfg.include_properties)
                    if page_ref is not None:
                        yield page_ref

                if data.get("has_more"):
                    cursor = data.get("next_cursor")
                else:
                    break

    async def _fetch_all_blocks(
        self,
        page_id: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch all blocks for a page (paginated), returning a flat list."""
        blocks: list[dict[str, Any]] = []
        cursor: str | None = None
        url = f"{_NOTION_API_BASE}/blocks/{page_id}/children"

        async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
            while True:
                params: dict[str, Any] = {"page_size": _PAGE_SIZE}
                if cursor:
                    params["start_cursor"] = cursor

                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                blocks.extend(data.get("results", []))

                if data.get("has_more"):
                    cursor = data.get("next_cursor")
                else:
                    break

        return blocks


def _page_to_ref(
    page: dict[str, Any], include_properties: list[str]
) -> DocumentRef | None:
    """Convert a Notion page API response to a DocumentRef."""
    page_id = page.get("id", "")
    if not page_id:
        return None

    last_edited = page.get("last_edited_time", "")
    updated_at = _parse_date(last_edited)

    # sha1 used as a compact fingerprint for change detection, not for security
    external_id = hashlib.sha1(  # noqa: S324
        f"{page_id}:{last_edited}".encode()
    ).hexdigest()

    url = page.get("url", f"https://notion.so/{page_id.replace('-', '')}")
    meta = _page_metadata(page, include_properties)

    return DocumentRef(
        external_id=external_id,
        uri=url,
        updated_at=updated_at,
        metadata=meta,
    )
