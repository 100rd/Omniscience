"""Shared types for IDE MCP-config templates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ServerParams:
    """Parameters used to render an MCP-server entry for any client.

    Attributes:
        name: Server identifier (e.g. ``"omniscience"``) — becomes the
            map key inside the client's config file.
        url: Public HTTP base URL of the Omniscience server.  Used for
            ``streamable-http`` transport entries (Cursor / Cline / Zed /
            Continue all support this; Claude Code uses the same form).
        token: Workspace API token; injected as a ``Bearer`` header.
    """

    name: str
    url: str
    token: str

    @property
    def mcp_url(self) -> str:
        """Return the full MCP endpoint (``{url}/mcp``).

        The Omniscience server mounts FastMCP at ``/mcp``; see
        ``apps/server/src/omniscience_server/mcp/mount.py``.
        """
        return self.url.rstrip("/") + "/mcp"

    @property
    def auth_headers(self) -> dict[str, str]:
        """Return the Authorization header map for HTTP transports."""
        return {"Authorization": f"Bearer {self.token}"}


class _BuildEntry(Protocol):
    def __call__(self, params: ServerParams) -> dict[str, Any]: ...


class _Merge(Protocol):
    def __call__(
        self,
        existing: dict[str, Any] | None,
        params: ServerParams,
    ) -> dict[str, Any]: ...


class _PathResolver(Protocol):
    def __call__(self, *, home: Path, cwd: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class ClientSpec:
    """Describes a single IDE / MCP-aware editor.

    Attributes:
        key: CLI ``--client`` value (kebab-case, stable).
        display_name: Human-friendly label for the success summary.
        scope: ``"global"`` (file lives under ``$HOME``) or
            ``"workspace"`` (file lives under the current project).
        path_resolver: Callable returning the absolute path to the
            client's config file.
        build_entry: Render the per-server config entry.
        merge: Merge new entry into existing file contents.
        spec_source: URL to the official documentation describing the
            file shape — included so reviewers can audit golden files.
    """

    key: str
    display_name: str
    scope: str
    path_resolver: _PathResolver
    build_entry: _BuildEntry
    merge: _Merge
    spec_source: str

    def resolve_path(self, *, home: Path, cwd: Path) -> Path:
        return self.path_resolver(home=home, cwd=cwd)


# ---------------------------------------------------------------------------
# Generic helpers shared by every client.
# ---------------------------------------------------------------------------


def http_entry(params: ServerParams) -> dict[str, Any]:
    """Standard ``streamable-http`` server entry shape.

    This shape is accepted by Cursor, Cline, Zed and Continue.  Claude
    Code (CLI/desktop) uses a near-identical structure with the same
    keys.  Keeping one helper avoids drift.
    """
    return {
        "type": "http",
        "url": params.mcp_url,
        "headers": params.auth_headers,
    }


def merge_into(
    container_key: str,
) -> Callable[[dict[str, Any] | None, ServerParams, _BuildEntry], dict[str, Any]]:
    """Return a merger that inserts ``entry`` under ``container_key``.

    Used by Claude Code, Cursor and Cline whose top-level shape is
    ``{container_key: {name: entry}}``.
    """

    def _merger(
        existing: dict[str, Any] | None,
        params: ServerParams,
        build_entry: _BuildEntry,
    ) -> dict[str, Any]:
        doc: dict[str, Any] = dict(existing) if existing else {}
        bucket_raw = doc.get(container_key)
        bucket: dict[str, Any] = dict(bucket_raw) if isinstance(bucket_raw, dict) else {}
        bucket[params.name] = build_entry(params)
        doc[container_key] = bucket
        return doc

    return _merger
