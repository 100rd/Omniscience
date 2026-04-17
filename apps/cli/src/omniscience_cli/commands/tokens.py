"""Token management commands: create, list, revoke."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from omniscience_cli.client import OmniscienceClient, OmniscienceClientError
from omniscience_cli.output import abort, console, print_json, print_success, tokens_table

app = typer.Typer(name="tokens", help="Manage API tokens.")

_VALID_SCOPES = ["search", "sources:read", "sources:write", "admin"]
_DEFAULT_SCOPES = "search,sources:read"


def _make_client() -> OmniscienceClient:
    return OmniscienceClient()


@app.command("create")
def tokens_create(
    name: Annotated[str, typer.Option(prompt=True, help="Token name")],
    scopes: Annotated[
        str,
        typer.Option(
            prompt=True,
            help=f"Comma-separated scopes. Valid: {', '.join(_VALID_SCOPES)}",
        ),
    ] = _DEFAULT_SCOPES,
) -> None:
    """Create a new API token. The plaintext value is shown once."""
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    invalid = [s for s in scope_list if s not in _VALID_SCOPES]
    if invalid:
        abort(f"Unknown scopes: {', '.join(invalid)}. Valid: {', '.join(_VALID_SCOPES)}")
        return
    with _make_client() as client:
        try:
            result = client.create_token(name, scope_list)
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    plaintext = result.get("token", result.get("plaintext", ""))
    print_success(f"Token '{name}' created.")
    if plaintext:
        console.print(f"\n  [bold yellow]Token (shown once):[/bold yellow]  {plaintext}\n")
    else:
        console.print(result)


@app.command("list")
def tokens_list(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List all API tokens."""
    with _make_client() as client:
        try:
            data = client.list_tokens()
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    items: list[dict[str, Any]] = data.get("tokens", [])
    if as_json:
        print_json(items)
        return
    tokens_table(items)


@app.command("revoke")
def tokens_revoke(
    token_id: Annotated[str, typer.Argument(help="Token id to revoke")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Revoke an API token."""
    if not yes:
        typer.confirm(f"Revoke token '{token_id}'?", abort=True)
    with _make_client() as client:
        try:
            client.revoke_token(token_id)
        except OmniscienceClientError as exc:
            abort(exc.message, exc.code)
            return
    print_success(f"Token '{token_id}' revoked.")


def _resolve_token_id(tokens: list[Any], name_or_id: str) -> str | None:
    """Return token id matching name or id field."""
    for t in tokens:
        if not isinstance(t, dict):
            continue
        if t.get("name") == name_or_id or t.get("id") == name_or_id:
            return str(t["id"])
    return None
