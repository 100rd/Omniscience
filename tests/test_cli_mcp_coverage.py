"""Coverage tests for omniscience_cli.commands.mcp (target ≥ 80%).

Exercises:
- Default transport (stdio) success path
- HTTP transport with custom port
- Unknown transport → abort / exit code 1
- Stdio server non-zero exit → abort / exit code 1
- Stdio server signal exits (SIGINT / SIGTERM) → clean exit 0
- HTTP server non-zero exit → abort / exit code 1
- FileNotFoundError on stdio (server not installed)
- FileNotFoundError on http (uvicorn not installed)
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from omniscience_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(returncode: int) -> MagicMock:
    """Stub subprocess.CompletedProcess with the given return code."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# mcp serve — invalid transport
# ---------------------------------------------------------------------------


class TestMcpServeTransport:
    def test_unknown_transport_exits_nonzero(self) -> None:
        result = runner.invoke(app, ["mcp", "serve", "--transport", "grpc"])
        assert result.exit_code != 0

    def test_help_exits_zero(self) -> None:
        result = runner.invoke(app, ["mcp", "serve", "--help"])
        assert result.exit_code == 0
        assert "stdio" in result.output


# ---------------------------------------------------------------------------
# mcp serve --transport stdio
# ---------------------------------------------------------------------------


class TestMcpServeStdio:
    def test_clean_exit_zero(self) -> None:
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            result = runner.invoke(app, ["mcp", "serve", "--transport", "stdio"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "omniscience_server.mcp_stdio" in cmd

    def test_sigint_exit_clean(self) -> None:
        """Return code -2 (SIGINT) should NOT trigger abort."""
        with patch("subprocess.run", return_value=_proc(-2)):
            result = runner.invoke(app, ["mcp", "serve", "--transport", "stdio"])
        assert result.exit_code == 0

    def test_sigterm_exit_clean(self) -> None:
        """Return code -15 (SIGTERM) should NOT trigger abort."""
        with patch("subprocess.run", return_value=_proc(-15)):
            result = runner.invoke(app, ["mcp", "serve", "--transport", "stdio"])
        assert result.exit_code == 0

    def test_nonzero_exit_aborts(self) -> None:
        with patch("subprocess.run", return_value=_proc(1)):
            result = runner.invoke(app, ["mcp", "serve", "--transport", "stdio"])
        assert result.exit_code != 0

    def test_file_not_found_aborts(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = runner.invoke(app, ["mcp", "serve", "--transport", "stdio"])
        assert result.exit_code != 0

    def test_default_transport_is_stdio(self) -> None:
        """Invoking without --transport defaults to stdio path."""
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            result = runner.invoke(app, ["mcp", "serve"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert any("mcp_stdio" in s for s in cmd)


# ---------------------------------------------------------------------------
# mcp serve --transport http
# ---------------------------------------------------------------------------


class TestMcpServeHttp:
    def test_clean_exit_zero(self) -> None:
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            result = runner.invoke(
                app, ["mcp", "serve", "--transport", "http"]
            )
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "uvicorn" in cmd

    def test_default_port_8000(self) -> None:
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            runner.invoke(app, ["mcp", "serve", "--transport", "http"])
        cmd = mock_run.call_args[0][0]
        assert "8000" in cmd

    def test_custom_port(self) -> None:
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            runner.invoke(
                app, ["mcp", "serve", "--transport", "http", "--port", "9090"]
            )
        cmd = mock_run.call_args[0][0]
        assert "9090" in cmd

    def test_nonzero_exit_aborts(self) -> None:
        with patch("subprocess.run", return_value=_proc(2)):
            result = runner.invoke(
                app, ["mcp", "serve", "--transport", "http"]
            )
        assert result.exit_code != 0

    def test_sigint_exit_clean(self) -> None:
        with patch("subprocess.run", return_value=_proc(-2)):
            result = runner.invoke(
                app, ["mcp", "serve", "--transport", "http"]
            )
        assert result.exit_code == 0

    def test_file_not_found_aborts(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = runner.invoke(
                app, ["mcp", "serve", "--transport", "http"]
            )
        assert result.exit_code != 0

    def test_short_flags(self) -> None:
        """Short flags -t and -p are accepted."""
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            result = runner.invoke(app, ["mcp", "serve", "-t", "http", "-p", "8080"])
        assert result.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert "8080" in cmd
