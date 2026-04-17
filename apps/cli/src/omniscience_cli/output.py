"""Rich-based output helpers for human and machine-readable display."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


def print_json(data: Any) -> None:
    """Serialize data to JSON and write to stdout."""
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def print_error(message: str, code: str | None = None) -> None:
    """Print a styled error message to stderr."""
    prefix = "[red]error[/red]"
    if code:
        prefix = f"[red]error[/red] [dim]({code})[/dim]"
    err_console.print(f"{prefix}: {message}")


def print_success(message: str) -> None:
    """Print a styled success message."""
    console.print(f"[green]ok[/green]  {message}")


def print_warning(message: str) -> None:
    """Print a styled warning message to stderr."""
    err_console.print(f"[yellow]warn[/yellow] {message}")


def sources_table(sources: list[dict[str, Any]]) -> None:
    """Render a Rich table of sources."""
    table = Table(title="Sources", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Last Sync")
    table.add_column("Docs", justify="right")
    table.add_column("Stale")
    for s in sources:
        last_sync = s.get("last_sync_at") or "—"
        stale = "[red]yes[/red]" if s.get("is_stale") else "[green]no[/green]"
        table.add_row(
            s.get("name", ""),
            s.get("type", ""),
            s.get("status", ""),
            str(last_sync),
            str(s.get("indexed_document_count", 0)),
            stale,
        )
    console.print(table)


def tokens_table(tokens: list[dict[str, Any]]) -> None:
    """Render a Rich table of API tokens."""
    table = Table(title="Tokens", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Prefix")
    table.add_column("Scopes")
    table.add_column("Last Used")
    table.add_column("ID")
    for t in tokens:
        table.add_row(
            t.get("name", ""),
            t.get("prefix", ""),
            ", ".join(t.get("scopes", [])),
            str(t.get("last_used_at") or "never"),
            t.get("id", ""),
        )
    console.print(table)


def search_results(hits: list[dict[str, Any]]) -> None:
    """Pretty-print search results with citations."""
    if not hits:
        console.print("[dim]No results.[/dim]")
        return
    for i, hit in enumerate(hits, 1):
        score = hit.get("score", 0.0)
        text = hit.get("text", "")
        citation = hit.get("citation") or {}
        uri = citation.get("uri", "")
        title = citation.get("title", "")
        source = hit.get("source") or {}
        source_name = source.get("name", "")

        console.rule(f"[bold]#{i}[/bold]  score={score:.4f}  {source_name}")
        if uri:
            console.print(f"  [link={uri}][cyan]{title or uri}[/cyan][/link]")
        console.print(f"  {text[:300]}{'...' if len(text) > 300 else ''}")
        console.print()


def doctor_row(check: str, ok: bool, detail: str = "") -> None:
    """Print a single doctor check result line."""
    status = "[green]pass[/green]" if ok else "[red]FAIL[/red]"
    suffix = f"  [dim]{detail}[/dim]" if detail else ""
    console.print(f"  {status}  {check}{suffix}")


def abort(message: str, code: str | None = None) -> None:
    """Print error and exit with status 1."""
    print_error(message, code)
    sys.exit(1)
