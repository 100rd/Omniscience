"""``omniscience init`` — one-shot IDE MCP-config writer.

What it does (in order):

1. Resolves connection params (server URL + workspace name).
2. Generates a workspace token via the Omniscience REST API
   (``POST /api/v1/tokens``) — unless one is supplied with ``--token``.
3. Builds the IDE-specific server entry, merges it into any existing
   config file, and writes the result (or prints to stdout with
   ``--print``).
4. Runs a JSON-RPC ``tools/list`` smoke test against ``{url}/mcp`` to
   prove the token works end-to-end.
5. Prints a tight success summary.

Design notes
------------

* Per-client formatting lives in :mod:`omniscience_cli.init_templates`,
  one module per IDE.  Adding an editor = one new template module.
* All filesystem mutations route through :func:`_write_atomic` so a
  crashed run never leaves a half-written settings.json.
* The smoke test is best-effort: a network failure prints a warning but
  does not unwrite the config.  Use ``--no-smoke-test`` in CI / air-gapped
  setups.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from omniscience_cli.client import OmniscienceClient, OmniscienceClientError
from omniscience_cli.init_templates import CLIENTS, ServerParams
from omniscience_cli.init_templates.base import ClientSpec
from omniscience_cli.output import abort, console, err_console, print_json, print_warning

app = typer.Typer(name="init", help="Bootstrap an MCP-aware editor (Claude Code, Cursor, ...).")

# JSON-RPC version + tools/list method id used by the smoke test.
_SMOKE_TIMEOUT_S = 10.0
_DEFAULT_NAME = "omniscience"
_DEFAULT_SCOPES = ("search", "sources:read")


# ---------------------------------------------------------------------------
# Token + smoke-test helpers
# ---------------------------------------------------------------------------


def _provision_token(name: str, scopes: tuple[str, ...]) -> str:
    """Create a workspace token via the REST API.

    Returns the plaintext value (shown once by the server) — callers
    embed it directly in the IDE config.
    """
    with OmniscienceClient() as client:
        try:
            result = client.create_token(name, list(scopes))
        except OmniscienceClientError as exc:
            abort(
                f"Failed to provision workspace token: {exc.message}",
                exc.code,
            )
            raise AssertionError from None  # pragma: no cover - abort() raises SystemExit
    plaintext = result.get("token") or result.get("plaintext")
    if not isinstance(plaintext, str) or not plaintext:
        abort(
            "Token API did not return a plaintext value; cannot continue.",
            "no_plaintext_token",
        )
        raise AssertionError from None  # pragma: no cover
    return plaintext


def smoke_test(url: str, token: str, *, timeout: float = _SMOKE_TIMEOUT_S) -> tuple[bool, str]:
    """Issue a JSON-RPC ``tools/list`` call to ``{url}/mcp``.

    Returns ``(ok, detail)`` so the caller can decide how loud to be.
    Network errors and non-2xx HTTP statuses are converted into a
    boolean result — we never raise.
    """
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    target = url.rstrip("/") + "/mcp"
    try:
        resp = httpx.post(target, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except ValueError:
        # Streamable-HTTP responses may be SSE; treat any 2xx as success.
        return True, f"HTTP {resp.status_code} (non-JSON body — assumed SSE)"
    if "error" in body:
        return False, f"JSON-RPC error: {body['error']}"
    result = body.get("result", {})
    tools = result.get("tools", []) if isinstance(result, dict) else []
    return True, f"{len(tools)} tools registered"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _read_existing(path: Path) -> dict[str, Any] | None:
    """Read + parse an existing JSON config file.

    Returns ``None`` if the file does not exist.  A corrupt file is
    *not* silently overwritten — we abort so the user can decide.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        abort(
            f"Existing config at {path} is not valid JSON ({exc}). "
            "Fix or remove it, or re-run with --force.",
            "corrupt_config",
        )
        raise AssertionError from None  # pragma: no cover
    if not isinstance(data, dict):
        abort(
            f"Existing config at {path} is JSON but not an object; refusing to merge.",
            "non_object_config",
        )
        raise AssertionError from None  # pragma: no cover
    return data


def _write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (write-tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _format_json(data: dict[str, Any]) -> str:
    """Stable, diff-friendly JSON: 2-space indent, sorted keys."""
    return json.dumps(data, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Command entry point
# ---------------------------------------------------------------------------


def _resolve_client(key: str) -> ClientSpec:
    spec = CLIENTS.get(key)
    if spec is None:
        valid = ", ".join(sorted(CLIENTS))
        abort(f"Unknown --client {key!r}. Valid choices: {valid}", "unknown_client")
        raise AssertionError from None  # pragma: no cover
    return spec


def _run(
    *,
    client_key: str,
    server_name: str,
    server_url: str | None,
    token: str | None,
    scopes_csv: str,
    print_only: bool,
    force: bool,
    run_smoke: bool,
    home: Path,
    cwd: Path,
) -> None:
    """Pure-ish core of the init command.

    All filesystem / network boundaries are passed in or hit via
    well-isolated helpers, so this function is straightforward to test.
    """
    spec = _resolve_client(client_key)

    url = server_url or os.environ.get("OMNISCIENCE_URL", "http://localhost:8000")
    url = url.rstrip("/")

    scopes = tuple(s.strip() for s in scopes_csv.split(",") if s.strip())
    if not scopes:
        scopes = _DEFAULT_SCOPES

    # 1. Token: explicit > env > provision.
    effective_token = token or os.environ.get("OMNISCIENCE_TOKEN") or ""
    if not effective_token:
        effective_token = _provision_token(name=f"{server_name}-{spec.key}", scopes=scopes)

    params = ServerParams(name=server_name, url=url, token=effective_token)

    # 2. Build full document.
    path = spec.resolve_path(home=home, cwd=cwd)
    existing = _read_existing(path) if not print_only else None
    document = spec.merge(existing, params)

    if print_only:
        print_json(
            {
                "client": spec.key,
                "path": str(path),
                "config": document,
            }
        )
        return

    # 3. Refuse to clobber unless --force when the named entry already exists.
    if existing is not None and not force and _entry_exists(spec, existing, server_name):
        abort(
            f"An MCP entry named {server_name!r} already exists in {path}. "
            "Re-run with --force to overwrite, or pick a different --name.",
            "entry_exists",
        )

    _write_atomic(path, _format_json(document))
    console.print(f"[green]ok[/green]  wrote {spec.display_name} config -> {path}")

    # 4. Smoke test (best effort).
    if run_smoke:
        ok, detail = smoke_test(url, effective_token)
        if ok:
            console.print(f"[green]ok[/green]  smoke test passed ({detail})")
        else:
            print_warning(f"smoke test failed: {detail}")
            err_console.print(
                "[dim]The config was written — re-run `omniscience doctor` once the "
                "server is reachable.[/dim]"
            )

    # 5. Success summary.
    console.print()
    console.print(f"[bold]Server:[/bold]  {server_name}")
    console.print(f"[bold]URL:[/bold]     {url}")
    console.print(f"[bold]Client:[/bold]  {spec.display_name}  [dim]({spec.scope})[/dim]")
    console.print(f"[bold]Config:[/bold]  {path}")
    if not token and not os.environ.get("OMNISCIENCE_TOKEN"):
        console.print(
            "[dim]A workspace token was provisioned; revoke via `omniscience tokens revoke`.[/dim]"
        )


def _entry_exists(spec: ClientSpec, existing: dict[str, Any], name: str) -> bool:
    """True if ``name`` already lives in the existing config under any of
    the well-known container keys."""
    for key in ("mcpServers", "context_servers"):
        bucket = existing.get(key)
        if isinstance(bucket, dict) and name in bucket:
            return True
    servers = existing.get("mcpServers")
    if isinstance(servers, list):
        for item in servers:
            if isinstance(item, dict) and item.get("name") == name:
                return True
    # Reference spec so linters don't flag it unused (kept for future
    # per-client overrides).
    _ = spec
    return False


@app.callback(invoke_without_command=True)
def init(
    client: Annotated[
        str,
        typer.Option(
            "--client",
            "-c",
            help="Target IDE: claude-code | cursor | cline | continue | zed",
        ),
    ],
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Server name (key inside the IDE config)"),
    ] = _DEFAULT_NAME,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Omniscience server URL (default: $OMNISCIENCE_URL)"),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Use an existing workspace token instead of provisioning a new one",
        ),
    ] = None,
    scopes: Annotated[
        str,
        typer.Option("--scopes", help="Comma-separated scopes for the provisioned token"),
    ] = ",".join(_DEFAULT_SCOPES),
    print_only: Annotated[
        bool,
        typer.Option(
            "--print",
            help="Emit the resulting config to stdout as JSON instead of writing it",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite an existing entry with the same name"),
    ] = False,
    no_smoke_test: Annotated[
        bool,
        typer.Option("--no-smoke-test", help="Skip the tools/list smoke test"),
    ] = False,
) -> None:
    """Write the MCP config for ``--client`` and verify with a smoke test."""
    _run(
        client_key=client,
        server_name=name,
        server_url=url,
        token=token,
        scopes_csv=scopes,
        print_only=print_only,
        force=force,
        run_smoke=not no_smoke_test,
        home=Path.home(),
        cwd=Path.cwd(),
    )
