"""AC-SP95-3 (live): positive/negative captures against the RUNNING
omniscience-api ``/mcp/`` surface -- the AC's own groundTruth ("positive/negative
API captures"), not merely the in-process producer object.

This is an integration test: it runs only when ``OMNISCIENCE_MCP_URL`` points at
a reachable running fragment (set by the smoke/CI), and otherwise **explicitly
skips** -- a named skip, never a silent pass (mirrors SP-86's go-toolchain
skipif). When enabled it uses ``OMNISCIENCE_MCP_TOKEN`` if provided, else mints
an exact ``omniscience-mcp-read-v1`` token in-process. The live evidence for the
current run is captured by ``live_mcp.py`` executed inside the api container.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.release.management_readonly_local import live_mcp

_LIVE_URL = os.environ.get("OMNISCIENCE_MCP_URL")

pytestmark = pytest.mark.skipif(
    not _LIVE_URL,
    reason="live /mcp capture requires OMNISCIENCE_MCP_URL to point at a running fragment",
)


def _token() -> str:
    token = os.environ.get("OMNISCIENCE_MCP_TOKEN")
    if token:
        return token
    minted, _workspace = asyncio.run(live_mcp.mint_read_token())
    return minted


def test_live_mcp_positive_and_negative_captures() -> None:
    evidence = live_mcp.capture(_token())
    gates = evidence["gates"]
    # POSITIVE: authorized workspace-bound read succeeds.
    assert gates["positive_authorized_read"]["isError"] is False
    # NEGATIVE: no owner-write / action / effect tool exists (fail closed by absence).
    assert gates["read_only_inventory"]["write_action_tools"] == []
    # NEGATIVE: foreign/cross-workspace read fails closed.
    assert gates["negative_foreign_read"]["isError"] is True
    assert "alert_not_found" in gates["negative_foreign_read"]["reason"]
    # NEGATIVE: unregistered consumer fails closed.
    assert gates["negative_unregistered_consumer"]["isError"] is True
    assert "unauthorized" in gates["negative_unregistered_consumer"]["reason"].lower()
    assert evidence["verdict"] == "GREEN"
