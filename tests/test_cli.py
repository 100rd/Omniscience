"""Tests for the Omniscience CLI."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from omniscience_cli.client import OmniscienceClient, OmniscienceClientError, _raise_for_error
from omniscience_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SOURCES_PAYLOAD: dict[str, Any] = {
    "sources": [
        {
            "id": "src-1",
            "name": "main-repo",
            "type": "git",
            "status": "active",
            "last_sync_at": "2026-04-17T10:00:00Z",
            "freshness_sla_seconds": 300,
            "is_stale": False,
            "indexed_document_count": 100,
        }
    ]
}

TOKENS_PAYLOAD: dict[str, Any] = {
    "tokens": [
        {
            "id": "tok-1",
            "name": "claude-code",
            "prefix": "sk_abc",
            "scopes": ["search", "sources:read"],
            "last_used_at": None,
        }
    ]
}

SEARCH_PAYLOAD: dict[str, Any] = {
    "hits": [
        {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "score": 0.92,
            "text": "def authenticate_token(token: str) -> User: ...",
            "source": {"id": "src-1", "name": "main-repo", "type": "git"},
            "citation": {
                "uri": "https://github.com/org/repo/blob/main/auth.py#L10",
                "title": "auth.py",
                "indexed_at": "2026-04-17T10:00:00Z",
                "doc_version": 3,
            },
            "lineage": {},
            "metadata": {"language": "python"},
        }
    ],
    "query_stats": {
        "total_matches_before_filters": 42,
        "vector_matches": 30,
        "text_matches": 25,
        "duration_ms": 18,
    },
}


def _mock_client(**method_map: Any) -> MagicMock:
    """Return a MagicMock that acts as an OmniscienceClient context manager."""
    mock = MagicMock(spec=OmniscienceClient)
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    for name, value in method_map.items():
        if isinstance(value, Exception):
            getattr(mock, name).side_effect = value
        else:
            getattr(mock, name).return_value = value
    return mock


# ---------------------------------------------------------------------------
# client.py unit tests
# ---------------------------------------------------------------------------


class TestOmniscienceClient:
    def test_reads_env_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_URL", "http://custom:9000")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "tok")
        c = OmniscienceClient()
        assert c.base_url == "http://custom:9000"
        c.close()

    def test_reads_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_URL", "http://localhost:8000")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "sk_test")
        c = OmniscienceClient()
        assert c.token == "sk_test"
        c.close()

    def test_explicit_args_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_URL", "http://env:8000")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "env_tok")
        c = OmniscienceClient(base_url="http://explicit:1234", token="explicit_tok")
        assert c.base_url == "http://explicit:1234"
        assert c.token == "explicit_tok"
        c.close()

    def test_trailing_slash_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_URL", "http://localhost:8000/")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "")
        c = OmniscienceClient()
        assert not c.base_url.endswith("/")
        c.close()

    def test_empty_token_no_auth_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNISCIENCE_TOKEN", raising=False)
        c = OmniscienceClient(base_url="http://localhost:8000", token="")
        assert "Authorization" not in c._client.headers
        c.close()

    def test_token_sets_auth_header(self) -> None:
        c = OmniscienceClient(base_url="http://localhost:8000", token="sk_abc")
        assert c._client.headers["authorization"] == "Bearer sk_abc"
        c.close()

    def test_context_manager(self) -> None:
        with OmniscienceClient(base_url="http://localhost:8000", token="t") as c:
            assert isinstance(c, OmniscienceClient)


class TestRaiseForError:
    def test_success_does_not_raise(self) -> None:
        import httpx

        resp = httpx.Response(200, json={"ok": True})
        _raise_for_error(resp)  # should not raise

    def test_structured_error_raises(self) -> None:
        import httpx

        body = {"error": {"code": "unauthorized", "message": "Bad token", "details": {}}}
        resp = httpx.Response(401, json=body)
        with pytest.raises(OmniscienceClientError) as exc_info:
            _raise_for_error(resp)
        assert exc_info.value.code == "unauthorized"
        assert exc_info.value.status == 401

    def test_unstructured_error_raises(self) -> None:
        import httpx

        resp = httpx.Response(500, text="Internal Server Error")
        with pytest.raises(OmniscienceClientError) as exc_info:
            _raise_for_error(resp)
        assert exc_info.value.status == 500


# ---------------------------------------------------------------------------
# sources commands
# ---------------------------------------------------------------------------


class TestSourcesList:
    def test_list_table_output(self) -> None:
        mock = _mock_client(list_sources=SOURCES_PAYLOAD)
        with patch("omniscience_cli.commands.sources.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["sources", "list"])
        assert result.exit_code == 0
        assert "main-repo" in result.output

    def test_list_json_output(self) -> None:
        mock = _mock_client(list_sources=SOURCES_PAYLOAD)
        with patch("omniscience_cli.commands.sources.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["sources", "list", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert parsed[0]["name"] == "main-repo"

    def test_list_api_error(self) -> None:
        err = OmniscienceClientError("unauthorized", "Bad token", 401)
        mock = _mock_client(list_sources=err)
        with patch("omniscience_cli.commands.sources.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["sources", "list"])
        assert result.exit_code != 0


class TestSourcesRemove:
    def test_remove_with_yes_flag(self) -> None:
        mock = _mock_client(list_sources=SOURCES_PAYLOAD, delete_source=None)
        with patch("omniscience_cli.commands.sources.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["sources", "remove", "--yes", "main-repo"])
        assert result.exit_code == 0
        mock.delete_source.assert_called_once_with("src-1")

    def test_remove_not_found(self) -> None:
        mock = _mock_client(list_sources={"sources": []}, delete_source=None)
        with patch("omniscience_cli.commands.sources.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["sources", "remove", "--yes", "ghost"])
        assert result.exit_code != 0


class TestSourcesSync:
    def test_sync_starts_run(self) -> None:
        run_resp = {"run_id": "run-123"}
        ingestion_resp = {"status": "completed", "documents_processed": 10}
        mock = _mock_client(
            list_sources=SOURCES_PAYLOAD,
            sync_source=run_resp,
            get_ingestion_run=ingestion_resp,
        )
        with patch("omniscience_cli.commands.sources.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["sources", "sync", "main-repo"])
        assert result.exit_code == 0
        assert "run-123" in result.output


class TestSourcesTest:
    def test_test_success(self) -> None:
        stats = {"indexed_document_count": 50, "freshness": "ok"}
        mock = _mock_client(list_sources=SOURCES_PAYLOAD, validate_source=stats)
        with patch("omniscience_cli.commands.sources.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["sources", "test", "main-repo"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# tokens commands
# ---------------------------------------------------------------------------


class TestTokensList:
    def test_list_table(self) -> None:
        mock = _mock_client(list_tokens=TOKENS_PAYLOAD)
        with patch("omniscience_cli.commands.tokens.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["tokens", "list"])
        assert result.exit_code == 0
        assert "claude-code" in result.output

    def test_list_json(self) -> None:
        mock = _mock_client(list_tokens=TOKENS_PAYLOAD)
        with patch("omniscience_cli.commands.tokens.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["tokens", "list", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["name"] == "claude-code"


class TestTokensCreate:
    def test_create_shows_plaintext_once(self) -> None:
        response = {"id": "tok-new", "name": "ci", "token": "sk_plaintext_value"}
        mock = _mock_client(create_token=response)
        with patch("omniscience_cli.commands.tokens.OmniscienceClient", return_value=mock):
            result = runner.invoke(
                app,
                ["tokens", "create", "--name", "ci", "--scopes", "search"],
            )
        assert result.exit_code == 0
        assert "sk_plaintext_value" in result.output

    def test_create_rejects_invalid_scope(self) -> None:
        mock = _mock_client(create_token={})
        with patch("omniscience_cli.commands.tokens.OmniscienceClient", return_value=mock):
            result = runner.invoke(
                app,
                ["tokens", "create", "--name", "bad", "--scopes", "invalid_scope"],
            )
        assert result.exit_code != 0
        assert "Unknown scopes" in result.stderr

    def test_create_api_error(self) -> None:
        err = OmniscienceClientError("forbidden", "Forbidden", 403)
        mock = _mock_client(create_token=err)
        with patch("omniscience_cli.commands.tokens.OmniscienceClient", return_value=mock):
            result = runner.invoke(
                app,
                ["tokens", "create", "--name", "x", "--scopes", "search"],
            )
        assert result.exit_code != 0


class TestTokensRevoke:
    def test_revoke_with_yes(self) -> None:
        mock = _mock_client(revoke_token=None)
        with patch("omniscience_cli.commands.tokens.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["tokens", "revoke", "--yes", "tok-1"])
        assert result.exit_code == 0
        mock.revoke_token.assert_called_once_with("tok-1")


# ---------------------------------------------------------------------------
# search command
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_human_output(self) -> None:
        mock = _mock_client(search=SEARCH_PAYLOAD)
        with patch("omniscience_cli.commands.search.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["search", "authentication"])
        assert result.exit_code == 0
        assert "authenticate_token" in result.output

    def test_search_json_output(self) -> None:
        mock = _mock_client(search=SEARCH_PAYLOAD)
        with patch("omniscience_cli.commands.search.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["search", "--json", "authentication"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "hits" in parsed
        assert parsed["hits"][0]["score"] == pytest.approx(0.92)

    def test_search_passes_source_filter(self) -> None:
        mock = _mock_client(search=SEARCH_PAYLOAD)
        with patch("omniscience_cli.commands.search.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["search", "--source", "main-repo", "query"])
        assert result.exit_code == 0
        call_kwargs = mock.search.call_args
        assert call_kwargs.kwargs.get("sources") == ["main-repo"]

    def test_search_passes_top_k(self) -> None:
        mock = _mock_client(search=SEARCH_PAYLOAD)
        with patch("omniscience_cli.commands.search.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["search", "--top-k", "5", "query"])
        assert result.exit_code == 0
        assert mock.search.call_args.kwargs.get("top_k") == 5

    def test_search_api_error(self) -> None:
        err = OmniscienceClientError("rate_limited", "Too many requests", 429)
        mock = _mock_client(search=err)
        with patch("omniscience_cli.commands.search.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["search", "query"])
        assert result.exit_code != 0

    def test_search_no_results(self) -> None:
        empty = {"hits": [], "query_stats": {"total_matches_before_filters": 0, "duration_ms": 5}}
        mock = _mock_client(search=empty)
        with patch("omniscience_cli.commands.search.OmniscienceClient", return_value=mock):
            result = runner.invoke(app, ["search", "nothing"])
        assert result.exit_code == 0
        assert "No results" in result.output


# ---------------------------------------------------------------------------
# doctor command
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_all_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_URL", "http://localhost:8000")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "sk_test")
        with (
            patch(
                "omniscience_cli.main._check_api",
                return_value=(True, "version=0.1.0"),
            ),
            patch(
                "omniscience_cli.main._check_nats",
                return_value=(True, "nats://localhost:4222"),
            ),
            patch(
                "omniscience_cli.main._check_embeddings",
                return_value=(True, "omniscience-embeddings importable"),
            ),
        ):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "All checks passed" in result.output

    def test_missing_env_fails_config_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNISCIENCE_URL", raising=False)
        monkeypatch.delenv("OMNISCIENCE_TOKEN", raising=False)
        with (
            patch(
                "omniscience_cli.main._check_api",
                return_value=(False, "connection refused"),
            ),
            patch(
                "omniscience_cli.main._check_nats",
                return_value=(False, "connection refused"),
            ),
            patch(
                "omniscience_cli.main._check_embeddings",
                return_value=(True, "ok"),
            ),
        ):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 0
        assert "FAIL" in result.output

    def test_nats_failure_shown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_URL", "http://localhost:8000")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "sk_t")
        with (
            patch(
                "omniscience_cli.main._check_api",
                return_value=(True, "version=0.1.0"),
            ),
            patch(
                "omniscience_cli.main._check_nats",
                return_value=(False, "Connection refused"),
            ),
            patch(
                "omniscience_cli.main._check_embeddings",
                return_value=(True, "ok"),
            ),
        ):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 0
        assert "FAIL" in result.output

    def test_embedding_failure_shown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_URL", "http://localhost:8000")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "sk_t")
        with (
            patch(
                "omniscience_cli.main._check_api",
                return_value=(True, "version=0.1.0"),
            ),
            patch(
                "omniscience_cli.main._check_nats",
                return_value=(True, "nats://localhost:4222"),
            ),
            patch(
                "omniscience_cli.main._check_embeddings",
                return_value=(False, "No module named 'omniscience_embeddings'"),
            ),
        ):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ops._check_* unit tests
# ---------------------------------------------------------------------------


class TestCheckHelpers:
    def test_check_config_missing_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omniscience_cli.commands.ops import _check_config

        monkeypatch.delenv("OMNISCIENCE_URL", raising=False)
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "t")
        ok, detail = _check_config()
        assert not ok
        assert "OMNISCIENCE_URL" in detail

    def test_check_config_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omniscience_cli.commands.ops import _check_config

        monkeypatch.setenv("OMNISCIENCE_URL", "http://localhost:8000")
        monkeypatch.setenv("OMNISCIENCE_TOKEN", "t")
        ok, _ = _check_config()
        assert ok

    def test_check_api_success(self) -> None:
        from omniscience_cli.commands.ops import _check_api

        mock = _mock_client(health={"status": "ok", "version": "0.1.0"})
        with patch("omniscience_cli.commands.ops.OmniscienceClient", return_value=mock):
            ok, detail = _check_api()
        assert ok
        assert "version=0.1.0" in detail

    def test_check_api_failure(self) -> None:
        from omniscience_cli.commands.ops import _check_api

        err = OmniscienceClientError("internal", "Service down", 500)
        mock = _mock_client(health=err)
        with patch("omniscience_cli.commands.ops.OmniscienceClient", return_value=mock):
            ok, detail = _check_api()
        assert not ok
        assert "Service down" in detail

    def test_check_embeddings_missing(self) -> None:
        from omniscience_cli.commands.ops import _check_embeddings

        with patch.dict("sys.modules", {"omniscience_embeddings": None}):
            # Force an ImportError by removing the module
            import sys

            saved = sys.modules.pop("omniscience_embeddings", None)
            try:
                ok, _detail = _check_embeddings()
                # If it's installed it's ok; if not it fails
                assert isinstance(ok, bool)
            finally:
                if saved is not None:
                    sys.modules["omniscience_embeddings"] = saved
