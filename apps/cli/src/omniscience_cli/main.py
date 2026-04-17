"""Omniscience CLI entry point."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from omniscience_cli.client import OmniscienceClientError
from omniscience_cli.commands import mcp, sources, tokens
from omniscience_cli.commands.ops import _check_api, _check_config, _check_embeddings, _check_nats
from omniscience_cli.commands.search import _make_client as _search_client
from omniscience_cli.output import (
    abort,
    console,
    doctor_row,
    print_json,
    print_success,
    search_results,
)

app = typer.Typer(
    name="omniscience",
    help="Omniscience — self-hosted knowledge retrieval.",
    no_args_is_help=True,
)

# Command groups
app.add_typer(sources.app, name="sources")
app.add_typer(tokens.app, name="tokens")
app.add_typer(mcp.app, name="mcp")


@app.command("search")
def cmd_search(
    query: Annotated[str, typer.Argument(help="Natural-language or keyword query")],
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Restrict to source names (repeatable)"),
    ] = None,
    top_k: Annotated[int, typer.Option("--top-k", "-k", help="Max results")] = 10,
    max_age: Annotated[int | None, typer.Option("--max-age", help="Max age in seconds")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Search Omniscience and display results."""
    with _search_client() as client:
        try:
            data = client.search(
                query,
                sources=source,
                top_k=top_k,
                max_age_seconds=max_age,
            )
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    hits: list[dict[str, Any]] = data.get("hits", [])
    stats: dict[str, Any] = data.get("query_stats", {})
    if as_json:
        print_json(data)
        return
    search_results(hits)
    dur = stats.get("duration_ms", "?")
    total = stats.get("total_matches_before_filters", "?")
    console.print(
        f"[dim]{len(hits)} results  total_before_filters={total}  duration={dur}ms[/dim]"
    )


@app.command("migrate")
def cmd_migrate(
    revision: Annotated[str, typer.Option("--revision", "-r", help="Alembic revision")] = "head",
) -> None:
    """Run database migrations via Alembic."""
    import subprocess
    import sys

    console.print(f"[dim]Running migrations to revision '{revision}'...[/dim]")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", revision],
        check=False,
    )
    if result.returncode != 0:
        abort(f"Alembic exited with code {result.returncode}.")
        return
    print_success(f"Migrations applied to '{revision}'.")


@app.command("doctor")
def cmd_doctor() -> None:
    """Check system health: config, API, NATS, embedding model."""
    all_ok = True

    cfg_ok, cfg_detail = _check_config()
    doctor_row("config", cfg_ok, cfg_detail)
    all_ok = all_ok and cfg_ok

    api_ok, api_detail = _check_api()
    doctor_row("api (http)", api_ok, api_detail)
    all_ok = all_ok and api_ok

    nats_ok, nats_detail = _check_nats()
    doctor_row("nats", nats_ok, nats_detail)
    all_ok = all_ok and nats_ok

    embed_ok, embed_detail = _check_embeddings()
    doctor_row("embedding model", embed_ok, embed_detail)
    all_ok = all_ok and embed_ok

    console.print()
    if all_ok:
        print_success("All checks passed.")
    else:
        abort("One or more checks failed.")


def main() -> None:
    """Legacy entry point kept for backwards compatibility."""
    app()
