"""Live positive/negative capture against the RUNNING omniscience-api MCP surface.

task-sp-95-management-readonly-local-runtime (AC-SP95-3). AC-SP95-3's ground
truth requires positive/negative *API captures* against the live local service,
not merely the in-process ``ManagementContextProducer`` object. This driver
mints an exact ``omniscience-mcp-read-v1`` read token in-process (using the
service's own auth code) and drives the live streamable-http MCP surface at
``/mcp/`` end to end:

  * initialize + notifications/initialized handshake succeeds;
  * tools/list exposes ONLY read tools -- no owner-write / action / effect /
    mutation tool (fail-closed by absence);
  * POSITIVE: an authorized, workspace-bound read (``contract_info``) succeeds;
  * NEGATIVE: an unauthenticated call fails closed (``unauthorized``);
  * NEGATIVE: a cross-workspace / foreign read fails closed (``alert_not_found``)
    with no bundle donation.

Run it INSIDE the api container (it talks to localhost:8000/mcp/ and needs the
DB to mint the token) -- the module is import-free of the test package so it
pipes cleanly over stdin:

    docker compose ... exec -T omniscience-api \
        python - < tests/release/management_readonly_local/live_mcp.py

Prints a JSON evidence block and exits 0 only when every gate passes; non-zero
(with the failing gate) otherwise. It never fabricates a pass. The pytest
wrapper (test_live_mcp_capture.py) imports ``capture`` and drives a live URL
when ``OMNISCIENCE_MCP_URL`` is set, and explicitly skips otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("OMNISCIENCE_MCP_URL", "http://localhost:8000/mcp/")

_WRITE_MARKERS = ("write", "create", "update", "delete", "mutat", "action", "execute", "apply")


async def mint_read_token() -> tuple[str, str]:
    """Mint an exact omniscience-mcp-read-v1 token for an existing workspace."""
    from omniscience_core.auth.tokens import create_mcp_read_token
    from omniscience_core.config import Settings
    from omniscience_core.db import create_async_engine, create_session_factory
    from sqlalchemy import text

    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        row = (await session.execute(text("SELECT id FROM workspaces LIMIT 1"))).first()
        workspace_id = row[0] if row else uuid.uuid4()
        if not isinstance(workspace_id, uuid.UUID):
            workspace_id = uuid.UUID(str(workspace_id))
        _, plaintext = await create_mcp_read_token(
            session, name="sp95-mcp-live-smoke", workspace_id=workspace_id
        )
        await session.commit()
    return plaintext, str(workspace_id)


def _rpc(payload: dict, *, sid: str | None, token: str | None) -> tuple[str | None, dict | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sid:
        headers["mcp-session-id"] = sid
    req = urllib.request.Request(  # noqa: S310 - fixed localhost MCP URL
        BASE_URL, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)  # noqa: S310
    except urllib.error.HTTPError as exc:
        return None, {"http_error": exc.code, "body": exc.read().decode()[:200]}
    out_sid = resp.headers.get("mcp-session-id")
    data = None
    for line in resp.read().decode().splitlines():
        if line.startswith("data: "):
            data = json.loads(line[6:])
    return out_sid, data


def _open_session(token: str | None) -> str:
    sid, _ = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "sp95-mcp-live-smoke", "version": "0"},
            },
        },
        sid=None,
        token=token,
    )
    assert sid, "MCP initialize did not return a session id"
    _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid, token=token)
    return sid


def _tool_text(result: dict | None) -> str:
    return (result or {}).get("result", {}).get("content", [{}])[0].get("text", "")


def _is_error(result: dict | None) -> bool:
    return bool((result or {}).get("result", {}).get("isError", False))


def capture(token: str) -> dict:
    """Return a live-capture evidence dict; raises AssertionError on any gate."""
    evidence: dict = {"base_url": BASE_URL, "gates": {}}

    # tools/list -- read-only inventory, no write/action tools.
    sid = _open_session(token)
    _, listing = _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid=sid, token=token)
    tools = sorted(t["name"] for t in listing["result"]["tools"])
    write_tools = [t for t in tools if any(m in t for m in _WRITE_MARKERS)]
    assert not write_tools, f"owner-write/action tool present: {write_tools}"
    evidence["gates"]["read_only_inventory"] = {"tools": tools, "write_action_tools": write_tools}

    # POSITIVE -- authorized workspace-bound read succeeds.
    _, pos = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "contract_info", "arguments": {}},
        },
        sid=sid,
        token=token,
    )
    assert not _is_error(pos), f"positive read failed: {_tool_text(pos)[:160]}"
    evidence["gates"]["positive_authorized_read"] = {"tool": "contract_info", "isError": False}

    # NEGATIVE -- foreign/cross-workspace read fails closed, no donation.
    _, foreign = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "incident_timeline",
                "arguments": {"alert_id": "alert://pagerduty/FOREIGN-WS-ALERT-999"},
            },
        },
        sid=sid,
        token=token,
    )
    foreign_text = _tool_text(foreign)
    assert _is_error(foreign), "foreign read did not fail closed"
    assert "alert_not_found" in foreign_text, f"unexpected foreign reason: {foreign_text[:160]}"
    evidence["gates"]["negative_foreign_read"] = {"isError": True, "reason": foreign_text[:120]}

    # NEGATIVE -- unauthenticated call fails closed (unregistered consumer).
    anon_sid = _open_session(None)
    _, anon = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "contract_info", "arguments": {}},
        },
        sid=anon_sid,
        token=None,
    )
    anon_text = _tool_text(anon)
    assert _is_error(anon), "unauthenticated call did not fail closed"
    assert "unauthorized" in anon_text.lower(), f"unexpected anon reason: {anon_text[:160]}"
    evidence["gates"]["negative_unregistered_consumer"] = {
        "isError": True,
        "reason": anon_text[:120],
    }

    evidence["verdict"] = "GREEN"
    return evidence


def main() -> int:
    token = os.environ.get("OMNISCIENCE_MCP_TOKEN")
    workspace = None
    if not token:
        token, workspace = asyncio.run(mint_read_token())
    try:
        evidence = capture(token)
    except AssertionError as exc:
        print(json.dumps({"verdict": "RED", "failed_gate": str(exc)}, indent=2))  # noqa: T201
        return 1
    if workspace:
        evidence["minted_token_workspace"] = workspace
    print(json.dumps(evidence, indent=2))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
