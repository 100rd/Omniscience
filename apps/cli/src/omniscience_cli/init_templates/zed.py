"""Zed template.

Spec: https://zed.dev/docs/assistant/model-context-protocol

Config file: ``~/.config/zed/settings.json``.  MCP servers live under
``context_servers.<name>`` (Zed uses snake_case keys).  Each entry uses
``settings`` to carry transport details.  Example::

    {
      "context_servers": {
        "<name>": {
          "command": null,
          "settings": {
            "transport": "http",
            "url": "https://.../mcp",
            "headers": {"Authorization": "Bearer ..."}
          }
        }
      }
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniscience_cli.init_templates.base import ClientSpec, ServerParams


def _entry(params: ServerParams) -> dict[str, Any]:
    return {
        "settings": {
            "transport": "http",
            "url": params.mcp_url,
            "headers": params.auth_headers,
        },
    }


def _path(*, home: Path, cwd: Path) -> Path:
    return home / ".config" / "zed" / "settings.json"


def _merge(existing: dict[str, Any] | None, params: ServerParams) -> dict[str, Any]:
    doc: dict[str, Any] = dict(existing) if existing else {}
    bucket_raw = doc.get("context_servers")
    bucket: dict[str, Any] = dict(bucket_raw) if isinstance(bucket_raw, dict) else {}
    bucket[params.name] = _entry(params)
    doc["context_servers"] = bucket
    return doc


ZED = ClientSpec(
    key="zed",
    display_name="Zed",
    scope="global",
    path_resolver=_path,
    build_entry=_entry,
    merge=_merge,
    spec_source="https://zed.dev/docs/assistant/model-context-protocol",
)
