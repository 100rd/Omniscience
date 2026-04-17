"""Entry point: ``python -m omniscience_server``."""

from __future__ import annotations

import uvicorn

from omniscience_server.app import create_app


def main() -> None:
    """Run the Omniscience server with uvicorn."""
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=8000,
        log_config=None,  # structlog handles all logging; suppress uvicorn's own config
    )


if __name__ == "__main__":
    main()
