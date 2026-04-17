"""Search command helpers."""

from __future__ import annotations

from omniscience_cli.client import OmniscienceClient


def _make_client() -> OmniscienceClient:
    """Return a configured client from environment."""
    return OmniscienceClient()
