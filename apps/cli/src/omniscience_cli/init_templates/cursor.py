"""Cursor template.

Spec: https://docs.cursor.com/context/mcp

Config file: project-scoped ``.cursor/mcp.json`` (Cursor also supports
``~/.cursor/mcp.json`` globally, but the per-workspace path is what the
issue requires).

Shape::

    {
      "mcpServers": {
        "<name>": {
          "type": "http",
          "url": "https://.../mcp",
          "headers": {"Authorization": "Bearer ..."}
        }
      }
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniscience_cli.init_templates.base import (
    ClientSpec,
    ServerParams,
    http_entry,
    merge_into,
)


def _path(*, home: Path, cwd: Path) -> Path:
    return cwd / ".cursor" / "mcp.json"


_MERGER = merge_into("mcpServers")


def _merge(existing: dict[str, Any] | None, params: ServerParams) -> dict[str, Any]:
    return _MERGER(existing, params, http_entry)


CURSOR = ClientSpec(
    key="cursor",
    display_name="Cursor",
    scope="workspace",
    path_resolver=_path,
    build_entry=http_entry,
    merge=_merge,
    spec_source="https://docs.cursor.com/context/mcp",
)
