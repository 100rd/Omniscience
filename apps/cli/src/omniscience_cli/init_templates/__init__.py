"""IDE-specific MCP configuration templates for ``omniscience init``.

Each client has two well-defined responsibilities:

* :func:`build_entry` — given a server name + connection params, return a
  dict matching exactly one server entry in that client's native config
  shape (e.g. Cursor's ``mcpServers.<name>``).
* :func:`config_path` — resolve the on-disk location where that client
  expects its MCP config (honouring ``$HOME`` / cwd as appropriate).
* :func:`merge` — given existing on-disk config (or ``None``) and a new
  entry, return the full file contents to write back.  This is the
  *only* place each client's surrounding-document shape leaks in, so
  golden-file tests can pin it precisely.

Adding a new client = one new module + registration in :data:`CLIENTS`.
Spec sources are linked in each module's docstring.
"""

from __future__ import annotations

from omniscience_cli.init_templates.base import ClientSpec, ServerParams
from omniscience_cli.init_templates.claude_code import CLAUDE_CODE
from omniscience_cli.init_templates.cline import CLINE
from omniscience_cli.init_templates.continue_dev import CONTINUE
from omniscience_cli.init_templates.cursor import CURSOR
from omniscience_cli.init_templates.zed import ZED

CLIENTS: dict[str, ClientSpec] = {
    CLAUDE_CODE.key: CLAUDE_CODE,
    CURSOR.key: CURSOR,
    CLINE.key: CLINE,
    CONTINUE.key: CONTINUE,
    ZED.key: ZED,
}

__all__ = [
    "CLAUDE_CODE",
    "CLIENTS",
    "CLINE",
    "CONTINUE",
    "CURSOR",
    "ZED",
    "ClientSpec",
    "ServerParams",
]
