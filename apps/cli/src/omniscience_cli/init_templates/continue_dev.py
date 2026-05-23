"""Continue.dev template.

Spec: https://docs.continue.dev/customize/deep-dives/mcp

Config file: ``~/.continue/config.json``.  Continue uses an array under
``experimental.modelContextProtocolServers`` (legacy) and a top-level
``mcpServers`` map on newer versions.  We populate **both** so users on
either release line get a working setup; Continue silently ignores the
shape it doesn't recognise.

Entry shape::

    {
      "name": "<name>",
      "transport": {
        "type": "streamable-http",
        "url": "https://.../mcp",
        "headers": {"Authorization": "Bearer ..."}
      }
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniscience_cli.init_templates.base import ClientSpec, ServerParams


def _entry(params: ServerParams) -> dict[str, Any]:
    return {
        "name": params.name,
        "transport": {
            "type": "streamable-http",
            "url": params.mcp_url,
            "headers": params.auth_headers,
        },
    }


def _path(*, home: Path, cwd: Path) -> Path:
    return home / ".continue" / "config.json"


def _merge(existing: dict[str, Any] | None, params: ServerParams) -> dict[str, Any]:
    doc: dict[str, Any] = dict(existing) if existing else {}
    entry = _entry(params)

    # New-style: top-level mcpServers array.
    servers_raw = doc.get("mcpServers")
    servers: list[Any] = list(servers_raw) if isinstance(servers_raw, list) else []
    servers = [s for s in servers if not (isinstance(s, dict) and s.get("name") == params.name)]
    servers.append(entry)
    doc["mcpServers"] = servers

    # Legacy: experimental.modelContextProtocolServers.
    exp_raw = doc.get("experimental")
    exp: dict[str, Any] = dict(exp_raw) if isinstance(exp_raw, dict) else {}
    legacy_raw = exp.get("modelContextProtocolServers")
    legacy: list[Any] = list(legacy_raw) if isinstance(legacy_raw, list) else []
    legacy = [s for s in legacy if not (isinstance(s, dict) and s.get("name") == params.name)]
    legacy.append(entry)
    exp["modelContextProtocolServers"] = legacy
    doc["experimental"] = exp

    return doc


CONTINUE = ClientSpec(
    key="continue",
    display_name="Continue",
    scope="global",
    path_resolver=_path,
    build_entry=_entry,
    merge=_merge,
    spec_source="https://docs.continue.dev/customize/deep-dives/mcp",
)
