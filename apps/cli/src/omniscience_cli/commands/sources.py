"""Source management commands: add, list, remove, test, sync."""

from __future__ import annotations

import time
from typing import Annotated, Any

import typer

from omniscience_cli.client import OmniscienceClient, OmniscienceClientError
from omniscience_cli.output import (
    abort,
    console,
    print_json,
    print_success,
    print_warning,
    sources_table,
)

app = typer.Typer(name="sources", help="Manage data sources.")

_KNOWN_TYPES = ["git", "fs", "confluence", "slack", "jira", "linear"]


def _make_client() -> OmniscienceClient:
    return OmniscienceClient()


@app.command("add")
def sources_add(
    name: Annotated[str, typer.Option(prompt=True, help="Source name")],
    source_type: Annotated[
        str,
        typer.Option("--type", "-t", prompt=True, help=f"Source type: {', '.join(_KNOWN_TYPES)}"),
    ],
    url: Annotated[str | None, typer.Option(prompt="URL (optional)", help="Remote URL")] = None,
    branch: Annotated[
        str | None, typer.Option(prompt="Branch (optional)", help="Git branch")
    ] = None,
) -> None:
    """Add a new data source."""
    body: dict[str, Any] = {"name": name, "type": source_type}
    config: dict[str, str] = {}
    if url:
        config["url"] = url
    if branch:
        config["branch"] = branch
    if config:
        body["config"] = config
    with _make_client() as client:
        try:
            result = client.create_source(body)
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    print_success(f"Source '{name}' created with id {result.get('id', '?')}.")


@app.command("list")
def sources_list(
    source_type: Annotated[str | None, typer.Option("--type", "-t", help="Filter by type")] = None,
    status: Annotated[str | None, typer.Option(help="Filter by status")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List configured sources."""
    with _make_client() as client:
        try:
            data = client.list_sources(source_type=source_type, status=status)
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    items: list[dict[str, Any]] = data.get("sources", [])
    if as_json:
        print_json(items)
        return
    sources_table(items)


@app.command("remove")
def sources_remove(
    name: Annotated[str, typer.Argument(help="Source name or id")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Remove a source and tombstone its documents."""
    if not yes:
        typer.confirm(f"Remove source '{name}' and tombstone all its documents?", abort=True)
    with _make_client() as client:
        try:
            sources_data = client.list_sources()
            source_id = _resolve_source_id(sources_data.get("sources", []), name)
            if not source_id:
                abort(f"Source '{name}' not found.")
                return
            client.delete_source(source_id)
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    print_success(f"Source '{name}' removed.")


@app.command("test")
def sources_test(
    name: Annotated[str, typer.Argument(help="Source name or id")],
) -> None:
    """Test source connectivity and validate config."""
    with _make_client() as client:
        try:
            sources_data = client.list_sources()
            source_id = _resolve_source_id(sources_data.get("sources", []), name)
            if not source_id:
                abort(f"Source '{name}' not found.")
                return
            stats = client.validate_source(source_id)
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    print_success(f"Source '{name}' is reachable.")
    console.print(stats)


@app.command("sync")
def sources_sync(
    name: Annotated[str, typer.Argument(help="Source name or id")],
    full: Annotated[bool, typer.Option("--full", help="Force a full re-sync")] = False,
) -> None:
    """Trigger a manual sync for a source."""
    with _make_client() as client:
        try:
            sources_data = client.list_sources()
            source_id = _resolve_source_id(sources_data.get("sources", []), name)
            if not source_id:
                abort(f"Source '{name}' not found.")
                return
            result = client.sync_source(source_id)
            run_id: str = result.get("run_id", "")
            console.print(f"Sync started — run_id={run_id}")
            if full:
                print_warning("--full flag noted; server determines sync scope per source type.")
            _poll_run(client, run_id)
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)


def _resolve_source_id(sources: list[Any], name: str) -> str | None:
    """Return source id matching name or id field."""
    for s in sources:
        if not isinstance(s, dict):
            continue
        if s.get("name") == name or s.get("id") == name:
            return str(s["id"])
    return None


def _poll_run(client: OmniscienceClient, run_id: str) -> None:
    """Poll ingestion run until terminal state, showing progress."""
    terminal = {"completed", "failed", "cancelled"}
    for _ in range(60):
        try:
            run = client.get_ingestion_run(run_id)
        except OmniscienceClientError:
            break
        state: str = run.get("status", "unknown")
        docs: int = run.get("documents_processed", 0)
        console.print(f"  status={state}  docs_processed={docs}", end="\r")
        if state in terminal:
            console.print()
            if state == "completed":
                print_success(f"Run {run_id} completed ({docs} docs).")
            else:
                abort(f"Run {run_id} ended with status '{state}'.")
            return
        time.sleep(2)
    print_warning(f"Timed out polling run {run_id}. Check manually.")
