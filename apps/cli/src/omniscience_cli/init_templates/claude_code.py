"""Claude Code template.

Spec: https://docs.claude.com/en/docs/claude-code/mcp

Config file: ``~/.claude/mcp_servers.json``.
Shape::

    {
      "mcpServers": {
        "<name>": {
          "type": "http",
          "url": "https://...",
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
    return home / ".claude" / "mcp_servers.json"


_MERGER = merge_into("mcpServers")


def _merge(existing: dict[str, Any] | None, params: ServerParams) -> dict[str, Any]:
    return _MERGER(existing, params, http_entry)


CLAUDE_CODE = ClientSpec(
    key="claude-code",
    display_name="Claude Code",
    scope="global",
    path_resolver=_path,
    build_entry=http_entry,
    merge=_merge,
    spec_source="https://docs.claude.com/en/docs/claude-code/mcp",
)
