"""MCP serve command — launch Omniscience as an MCP server."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Annotated

import typer

from omniscience_cli.output import abort, err_console

app = typer.Typer(name="mcp", help="MCP server operations.")


@app.command("serve")
def mcp_serve(
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            "-t",
            help="Transport type: stdio or http",
            metavar="TRANSPORT",
        ),
    ] = "stdio",
    port: Annotated[
        int, typer.Option("--port", "-p", help="HTTP port (http transport only)")
    ] = 8000,
) -> None:
    """Launch the Omniscience MCP server.

    For Claude Code / Cursor, use --transport stdio.
    For hosted deployments, use --transport http.
    """
    if transport not in ("stdio", "http"):
        abort(f"Unknown transport '{transport}'. Choose stdio or http.")
        return

    if transport == "stdio":
        _serve_stdio()
    else:
        _serve_http(port)


def _serve_stdio() -> None:
    """Start the MCP server with stdio transport by launching the server process."""
    server_mod = "omniscience_server.mcp_stdio"
    err_console.print("[dim]Starting MCP server (stdio)...[/dim]")
    env = os.environ.copy()
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", server_mod],
            env=env,
            check=False,
        )
        if proc.returncode not in (0, -2, -15):  # 0=clean, -2=SIGINT, -15=SIGTERM
            abort(f"MCP stdio server exited with code {proc.returncode}.")
    except FileNotFoundError:
        abort(
            "Could not launch omniscience server. "
            "Ensure omniscience-server is installed in this environment."
        )


def _serve_http(port: int) -> None:
    """Start the full HTTP server (Uvicorn) for streamable-http MCP."""
    err_console.print(f"[dim]Starting MCP server (http) on port {port}...[/dim]")
    env = os.environ.copy()
    try:
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "omniscience_server.app:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ],
            env=env,
            check=False,
        )
        if proc.returncode not in (0, -2, -15):
            abort(f"MCP HTTP server exited with code {proc.returncode}.")
    except FileNotFoundError:
        abort("uvicorn not found. Install it with: uv add uvicorn")
