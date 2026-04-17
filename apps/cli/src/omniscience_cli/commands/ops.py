"""Doctor check helpers used by the ops commands in main.py."""

from __future__ import annotations

from omniscience_cli.client import OmniscienceClient, OmniscienceClientError


def _check_config() -> tuple[bool, str]:
    """Validate that required env vars are set."""
    import os

    missing = [v for v in ("OMNISCIENCE_URL", "OMNISCIENCE_TOKEN") if not os.environ.get(v)]
    if missing:
        return False, f"Missing env vars: {', '.join(missing)}"
    return True, ""


def _check_api() -> tuple[bool, str]:
    """Call /health on the configured API endpoint."""
    try:
        with OmniscienceClient() as client:
            health = client.health()
            version = health.get("version", "?")
            return True, f"version={version}"
    except OmniscienceClientError as exc:
        return False, exc.message
    except Exception as exc:
        return False, str(exc)


def _check_nats() -> tuple[bool, str]:
    """Attempt a TCP connection to the configured NATS URL."""
    import os
    import socket

    nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
    try:
        host_port = nats_url.replace("nats://", "").split("/")[0]
        host, _, port_str = host_port.partition(":")
        port = int(port_str) if port_str else 4222
        with socket.create_connection((host, port), timeout=3):
            return True, nats_url
    except Exception as exc:
        return False, str(exc)


def _check_embeddings() -> tuple[bool, str]:
    """Verify the embedding package is importable."""
    try:
        import omniscience_embeddings  # noqa: F401

        return True, "omniscience-embeddings importable"
    except ImportError as exc:
        return False, str(exc)
