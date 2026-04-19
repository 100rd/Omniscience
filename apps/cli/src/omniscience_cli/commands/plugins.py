"""Plugin management commands: list, install, info."""

from __future__ import annotations

import subprocess
import sys
from typing import Annotated

import typer

from omniscience_cli.output import abort, console, print_json, print_success, print_warning

app = typer.Typer(name="plugins", help="Manage third-party connector plugins.")


@app.command("list")
def plugins_list(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List all installed connector plugins discovered via entry points."""
    from omniscience_connectors.plugins.loader import PluginLoader

    loader = PluginLoader()
    plugins = loader.discover_plugins()

    if not plugins:
        console.print("[dim]No connector plugins installed.[/dim]")
        console.print("[dim]Install a plugin with: omniscience plugins install <package>[/dim]")
        return

    if as_json:
        print_json([p.model_dump() for p in plugins])
        return

    from rich.table import Table

    table = Table(title="Installed Connector Plugins", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Connector Type")
    table.add_column("Author")
    table.add_column("Description")

    for plugin in plugins:
        table.add_row(
            plugin.name,
            plugin.version,
            plugin.connector_type,
            plugin.author or "—",
            plugin.description or "—",
        )

    console.print(table)
    console.print(f"\n[dim]{len(plugins)} plugin(s) installed.[/dim]")


@app.command("install")
def plugins_install(
    package: Annotated[
        str, typer.Argument(help="PyPI package name, e.g. omniscience-plugin-github")
    ],
    version: Annotated[
        str | None,
        typer.Option("--version", "-v", help="Pin to a specific version, e.g. 1.2.3"),
    ] = None,
    upgrade: Annotated[
        bool, typer.Option("--upgrade", "-U", help="Upgrade if already installed")
    ] = False,
) -> None:
    """Install a connector plugin from PyPI (or a local path).

    The package must declare its connectors via entry points under the
    group ``omniscience.connectors``.  After installation the plugin is
    immediately available — no restart required.

    Example:

        omniscience plugins install omniscience-plugin-github
        omniscience plugins install ./path/to/local/plugin
        omniscience plugins install omniscience-plugin-github --version 1.0.0
    """
    target = package if version is None else f"{package}=={version}"
    cmd = [sys.executable, "-m", "pip", "install", target]
    if upgrade:
        cmd.append("--upgrade")

    console.print(f"[dim]Installing {target!r}...[/dim]")
    result = subprocess.run(cmd, check=False)  # noqa: S603

    if result.returncode != 0:
        abort(f"pip exited with code {result.returncode}.")
        return

    print_success(f"Plugin package {target!r} installed.")

    # Try to discover and show what was registered
    from omniscience_connectors.plugins.loader import PluginLoader

    loader = PluginLoader()
    plugins = [p for p in loader.discover_plugins() if p.name in package or package in p.name]
    if plugins:
        for p in plugins:
            console.print(
                f"  [green]+[/green] connector [bold]{p.connector_type!r}[/bold] ({p.class_name})"
            )
    else:
        print_warning(
            f"Package installed but no entry points found for group "
            f"'omniscience.connectors' in {package!r}. "
            "Ensure the package declares entry points correctly."
        )


@app.command("info")
def plugins_info(
    name: Annotated[str, typer.Argument(help="Plugin name (entry-point key)")],
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show detailed information about an installed connector plugin."""
    from omniscience_connectors.plugins.loader import PluginLoader

    loader = PluginLoader()
    plugins = loader.discover_plugins()

    matches = [p for p in plugins if p.name == name]
    if not matches:
        # Try partial match
        matches = [p for p in plugins if name.lower() in p.name.lower()]

    if not matches:
        abort(
            f"Plugin {name!r} not found. Use 'omniscience plugins list' to see installed plugins."
        )
        return

    plugin = matches[0]

    if as_json:
        print_json(plugin.model_dump())
        return

    console.rule(f"[bold]{plugin.name}[/bold]")
    console.print(f"  [bold]Name:[/bold]           {plugin.name}")
    console.print(f"  [bold]Version:[/bold]        {plugin.version}")
    console.print(f"  [bold]Connector Type:[/bold] {plugin.connector_type}")
    console.print(f"  [bold]Module:[/bold]         {plugin.module_path}")
    console.print(f"  [bold]Class:[/bold]          {plugin.class_name}")
    console.print(f"  [bold]Author:[/bold]         {plugin.author or '—'}")
    console.print(f"  [bold]Description:[/bold]    {plugin.description or '—'}")
    if plugin.homepage:
        console.print(
            f"  [bold]Homepage:[/bold]       [link={plugin.homepage}]{plugin.homepage}[/link]"
        )

    # Attempt to load and validate the class
    try:
        connector_cls = loader.load_plugin(plugin)
        console.print()
        console.print("[green]Connector class loads and validates successfully.[/green]")

        # Show config schema fields if available
        config_schema = getattr(connector_cls, "config_schema", None)
        if config_schema is not None:
            fields = getattr(config_schema, "model_fields", {})
            if fields:
                console.print()
                console.print("[bold]Config schema fields:[/bold]")
                for field_name, field_info in fields.items():
                    annotation = getattr(field_info, "annotation", "?")
                    console.print(f"  [cyan]{field_name}[/cyan]: {annotation}")
    except Exception as exc:
        print_warning(f"Could not load connector class: {exc}")
