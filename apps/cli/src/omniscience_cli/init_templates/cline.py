"""Cline (VS Code extension) template.

Spec: https://docs.cline.bot/mcp/configuring-mcp-servers

Config file: ``.vscode/cline_mcp_settings.json`` (per-workspace).
Shape mirrors the Claude/Cursor ``mcpServers`` map::

    {
      "mcpServers": {
        "<name>": {
          "type": "http",
          "url": "https://.../mcp",
          "headers": {"Authorization": "Bearer ..."},
          "disabled": false,
          "autoApprove": []
        }
      }
    }

The ``disabled`` and ``autoApprove`` keys are Cline-specific and ship
with sensible defaults so the entry "just works" after init.
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


def _entry(params: ServerParams) -> dict[str, Any]:
    base = http_entry(params)
    base["disabled"] = False
    base["autoApprove"] = []
    return base


def _path(*, home: Path, cwd: Path) -> Path:
    return cwd / ".vscode" / "cline_mcp_settings.json"


_MERGER = merge_into("mcpServers")


def _merge(existing: dict[str, Any] | None, params: ServerParams) -> dict[str, Any]:
    return _MERGER(existing, params, _entry)


CLINE = ClientSpec(
    key="cline",
    display_name="Cline",
    scope="workspace",
    path_resolver=_path,
    build_entry=_entry,
    merge=_merge,
    spec_source="https://docs.cline.bot/mcp/configuring-mcp-servers",
)
