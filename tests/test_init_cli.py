"""Tests for ``omniscience init`` — IDE MCP-config writer.

Coverage strategy
-----------------

* **Per-client golden files** under ``tests/fixtures/init_cli/`` pin the
  exact on-disk shape for each editor.  Updating a template is a
  deliberate act: the diff lands in review.
* **CLI integration tests** drive ``omniscience init`` through Typer's
  ``CliRunner`` with the token-provisioning + smoke-test seams mocked,
  proving the end-to-end flow without needing a live server.
* **Helper tests** cover ``_write_atomic``, ``_read_existing`` and the
  ``smoke_test`` HTTP wrapper individually so failures point at the
  right layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from omniscience_cli.commands import init as init_cmd
from omniscience_cli.init_templates import CLAUDE_CODE, CLIENTS, CLINE, CONTINUE, CURSOR, ZED
from omniscience_cli.init_templates.base import ServerParams
from omniscience_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "init_cli"

_PARAMS = ServerParams(
    name="omniscience",
    url="https://omni.example.com",
    token="sk_test_TOKEN",
)


# ---------------------------------------------------------------------------
# Golden-file tests — one per client
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "fixture"),
    [
        (CLAUDE_CODE, "claude-code.json"),
        (CURSOR, "cursor.json"),
        (CLINE, "cline.json"),
        (CONTINUE, "continue.json"),
        (ZED, "zed.json"),
    ],
)
def test_template_matches_golden(spec: Any, fixture: str) -> None:
    """The rendered config exactly matches the on-disk golden file.

    Goldens are sorted-keys + 2-space JSON to match what the CLI writes.
    """
    document = spec.merge(None, _PARAMS)
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    expected = (FIXTURE_DIR / fixture).read_text(encoding="utf-8")
    assert rendered == expected, (
        f"Golden drift for {spec.key}.\n--- got ---\n{rendered}\n--- want ---\n{expected}"
    )


def test_clients_registry_covers_all_required_keys() -> None:
    """The CLI must support all five clients named in the issue."""
    assert set(CLIENTS) == {"claude-code", "cursor", "cline", "continue", "zed"}


def test_path_resolvers_use_correct_scope(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    assert CLAUDE_CODE.resolve_path(home=home, cwd=cwd) == home / ".claude" / "mcp_servers.json"
    assert CURSOR.resolve_path(home=home, cwd=cwd) == cwd / ".cursor" / "mcp.json"
    assert CLINE.resolve_path(home=home, cwd=cwd) == cwd / ".vscode" / "cline_mcp_settings.json"
    assert CONTINUE.resolve_path(home=home, cwd=cwd) == home / ".continue" / "config.json"
    assert ZED.resolve_path(home=home, cwd=cwd) == home / ".config" / "zed" / "settings.json"


# ---------------------------------------------------------------------------
# Merge semantics — existing files
# ---------------------------------------------------------------------------


def test_merge_preserves_unrelated_servers_claude_code() -> None:
    existing = {
        "mcpServers": {"other": {"type": "http", "url": "https://other/mcp"}},
        "unrelatedKey": True,
    }
    merged = CLAUDE_CODE.merge(existing, _PARAMS)
    assert merged["unrelatedKey"] is True
    assert "other" in merged["mcpServers"]
    assert merged["mcpServers"]["omniscience"]["url"] == "https://omni.example.com/mcp"


def test_merge_replaces_same_name_continue() -> None:
    existing = {
        "mcpServers": [
            {"name": "omniscience", "transport": {"url": "https://stale/mcp"}},
            {"name": "other", "transport": {"url": "https://other/mcp"}},
        ]
    }
    merged = CONTINUE.merge(existing, _PARAMS)
    servers = merged["mcpServers"]
    names = [s["name"] for s in servers]
    assert names.count("omniscience") == 1
    omni = next(s for s in servers if s["name"] == "omniscience")
    assert omni["transport"]["url"] == "https://omni.example.com/mcp"
    assert any(s["name"] == "other" for s in servers)


# ---------------------------------------------------------------------------
# Helpers — _read_existing / _write_atomic / smoke_test
# ---------------------------------------------------------------------------


def test_read_existing_returns_none_for_missing(tmp_path: Path) -> None:
    assert init_cmd._read_existing(tmp_path / "nope.json") is None


def test_read_existing_aborts_on_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        init_cmd._read_existing(p)


def test_read_existing_aborts_on_non_object(tmp_path: Path) -> None:
    p = tmp_path / "arr.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SystemExit):
        init_cmd._read_existing(p)


def test_write_atomic_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.json"
    init_cmd._write_atomic(target, '{"x":1}')
    assert target.read_text(encoding="utf-8") == '{"x":1}\n'


def test_smoke_test_success() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "search"}]}}
        )
    )
    with patch.object(
        httpx,
        "post",
        lambda *a, **kw: transport.handle_request(
            httpx.Request("POST", a[0], json=kw.get("json"))
        ),
    ):
        ok, detail = init_cmd.smoke_test("https://x", "tok")
    assert ok
    assert "1 tools" in detail


def test_smoke_test_http_error() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(_handler)
    with patch.object(
        httpx,
        "post",
        lambda *a, **kw: transport.handle_request(
            httpx.Request("POST", a[0], json=kw.get("json"))
        ),
    ):
        ok, detail = init_cmd.smoke_test("https://x", "tok")
    assert not ok
    assert "500" in detail


def test_smoke_test_network_error() -> None:
    def _raise(*_a: Any, **_kw: Any) -> Any:
        raise httpx.ConnectError("nope")

    with patch.object(httpx, "post", _raise):
        ok, detail = init_cmd.smoke_test("https://x", "tok")
    assert not ok
    assert "network error" in detail


def test_smoke_test_jsonrpc_error() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad"}}
        )
    )
    with patch.object(
        httpx,
        "post",
        lambda *a, **kw: transport.handle_request(
            httpx.Request("POST", a[0], json=kw.get("json"))
        ),
    ):
        ok, detail = init_cmd.smoke_test("https://x", "tok")
    assert not ok
    assert "JSON-RPC error" in detail


# ---------------------------------------------------------------------------
# End-to-end CLI tests
# ---------------------------------------------------------------------------


def _patched_client(token_value: str = "sk_test_TOKEN") -> Any:
    """Return a context-manager mock of OmniscienceClient with a token API."""
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.create_token.return_value = {"id": "tok-1", "token": token_value}
    return mock


@pytest.mark.parametrize("client_key", ["claude-code", "cursor", "cline", "continue", "zed"])
def test_cli_writes_config_and_runs_smoke(tmp_path: Path, client_key: str) -> None:
    """`omniscience init --client=X` writes the expected file and smoke-tests it."""
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    home.mkdir()
    cwd.mkdir()

    mock_client = _patched_client()
    with (
        patch("omniscience_cli.commands.init.OmniscienceClient", return_value=mock_client),
        patch(
            "omniscience_cli.commands.init.smoke_test", return_value=(True, "2 tools registered")
        ),
        patch("omniscience_cli.commands.init.Path") as path_mock,
    ):
        path_mock.home.return_value = home
        path_mock.cwd.return_value = cwd
        # Path() called with str should still produce a real Path
        path_mock.side_effect = lambda *a, **kw: Path(*a, **kw)
        result = runner.invoke(
            app,
            [
                "init",
                "--client",
                client_key,
                "--url",
                "https://omni.example.com",
            ],
        )

    assert result.exit_code == 0, result.output
    spec = CLIENTS[client_key]
    target = spec.resolve_path(home=home, cwd=cwd)
    assert target.exists(), f"expected {target} to be written"
    written = json.loads(target.read_text(encoding="utf-8"))
    golden_name = "continue.json" if client_key == "continue" else f"{client_key}.json"
    expected = json.loads((FIXTURE_DIR / golden_name).read_text(encoding="utf-8"))
    assert written == expected
    mock_client.create_token.assert_called_once()


def test_cli_print_mode_emits_json_no_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    home.mkdir()
    cwd.mkdir()
    mock_client = _patched_client()
    with (
        patch("omniscience_cli.commands.init.OmniscienceClient", return_value=mock_client),
        patch("omniscience_cli.commands.init.Path") as path_mock,
    ):
        path_mock.home.return_value = home
        path_mock.cwd.return_value = cwd
        path_mock.side_effect = lambda *a, **kw: Path(*a, **kw)
        result = runner.invoke(
            app,
            ["init", "--client", "cursor", "--url", "https://omni.example.com", "--print"],
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["client"] == "cursor"
    assert payload["config"]["mcpServers"]["omniscience"]["url"] == "https://omni.example.com/mcp"
    # Nothing on disk.
    assert not (cwd / ".cursor" / "mcp.json").exists()


def test_cli_uses_supplied_token_no_provisioning(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    home.mkdir()
    cwd.mkdir()
    mock_client = _patched_client()
    with (
        patch("omniscience_cli.commands.init.OmniscienceClient", return_value=mock_client),
        patch("omniscience_cli.commands.init.smoke_test", return_value=(True, "ok")),
        patch("omniscience_cli.commands.init.Path") as path_mock,
    ):
        path_mock.home.return_value = home
        path_mock.cwd.return_value = cwd
        path_mock.side_effect = lambda *a, **kw: Path(*a, **kw)
        result = runner.invoke(
            app,
            [
                "init",
                "--client",
                "claude-code",
                "--url",
                "https://omni.example.com",
                "--token",
                "sk_explicit",
            ],
        )
    assert result.exit_code == 0, result.output
    mock_client.create_token.assert_not_called()
    written = json.loads((home / ".claude" / "mcp_servers.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["omniscience"]["headers"]["Authorization"] == "Bearer sk_explicit"


def test_cli_unknown_client_aborts() -> None:
    result = runner.invoke(app, ["init", "--client", "nopepad"])
    assert result.exit_code != 0


def test_cli_refuses_clobber_without_force(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    home.mkdir()
    cwd.mkdir()
    target = home / ".claude" / "mcp_servers.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"mcpServers": {"omniscience": {"type": "http", "url": "x"}}}),
        encoding="utf-8",
    )
    mock_client = _patched_client()
    with (
        patch("omniscience_cli.commands.init.OmniscienceClient", return_value=mock_client),
        patch("omniscience_cli.commands.init.smoke_test", return_value=(True, "ok")),
        patch("omniscience_cli.commands.init.Path") as path_mock,
    ):
        path_mock.home.return_value = home
        path_mock.cwd.return_value = cwd
        path_mock.side_effect = lambda *a, **kw: Path(*a, **kw)
        result = runner.invoke(
            app,
            ["init", "--client", "claude-code", "--token", "sk_x"],
        )
    assert result.exit_code != 0
    # File unchanged.
    assert (
        json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["omniscience"]["url"] == "x"
    )


def test_cli_force_overwrites_existing_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "proj"
    home.mkdir()
    cwd.mkdir()
    target = home / ".claude" / "mcp_servers.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"mcpServers": {"omniscience": {"type": "http", "url": "stale"}}}),
        encoding="utf-8",
    )
    mock_client = _patched_client()
    with (
        patch("omniscience_cli.commands.init.OmniscienceClient", return_value=mock_client),
        patch("omniscience_cli.commands.init.smoke_test", return_value=(True, "ok")),
        patch("omniscience_cli.commands.init.Path") as path_mock,
    ):
        path_mock.home.return_value = home
        path_mock.cwd.return_value = cwd
        path_mock.side_effect = lambda *a, **kw: Path(*a, **kw)
        result = runner.invoke(
            app,
            [
                "init",
                "--client",
                "claude-code",
                "--url",
                "https://omni.example.com",
                "--token",
                "sk_test_TOKEN",
                "--force",
            ],
        )
    assert result.exit_code == 0, result.output
    refreshed = json.loads(target.read_text(encoding="utf-8"))
    assert refreshed["mcpServers"]["omniscience"]["url"] == "https://omni.example.com/mcp"
