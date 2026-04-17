"""Tests for the git source connector (Issue #17).

Uses temporary git repositories created locally — no network required.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from omniscience_connectors import DocumentRef, FetchedDocument
from omniscience_connectors.git.connector import GitConfig, GitConnector
from omniscience_connectors.git.webhook import GitWebhookHandler

from tests.connectors.contract_tests import ConnectorContractTests

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: str | None = None) -> None:
    """Run a git command in a subprocess; raise on failure."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(files: dict[str, str] | None = None) -> str:
    """Create a temp git repo and return its path.

    *files* maps relative path -> file content.
    """
    tmp = tempfile.mkdtemp(prefix="omni_git_test_")
    _git("init", cwd=tmp)
    _git("config", "user.email", "test@example.com", cwd=tmp)
    _git("config", "user.name", "Test", cwd=tmp)

    default_files = (
        files if files is not None else {"README.md": "# Hello\n", "src/main.py": "print('hi')\n"}
    )
    for rel_path, content in default_files.items():
        full = Path(tmp) / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    _git("add", ".", cwd=tmp)
    _git("commit", "-m", "init", cwd=tmp)
    return tmp


# ---------------------------------------------------------------------------
# GitConnector.validate
# ---------------------------------------------------------------------------


class TestGitConnectorValidate:
    async def test_validate_local_repo_succeeds(self) -> None:
        repo = _make_repo()
        connector = GitConnector()
        config = GitConfig(url=repo)
        await connector.validate(config, {})

    async def test_validate_nonexistent_path_raises(self) -> None:
        connector = GitConnector()
        config = GitConfig(url="/tmp/does-not-exist-xyz-abc")
        with pytest.raises(Exception):
            await connector.validate(config, {})

    async def test_validate_non_git_directory_raises(self) -> None:
        tmp = tempfile.mkdtemp()
        connector = GitConnector()
        config = GitConfig(url=tmp)
        with pytest.raises(Exception):
            await connector.validate(config, {})

    async def test_validate_invalid_ref_raises(self) -> None:
        repo = _make_repo()
        connector = GitConnector()
        config = GitConfig(url=repo, ref="refs/heads/nonexistent-branch")
        with pytest.raises(Exception):
            await connector.validate(config, {})


# ---------------------------------------------------------------------------
# GitConnector.discover
# ---------------------------------------------------------------------------


class TestGitConnectorDiscover:
    async def test_discover_lists_all_files(self) -> None:
        repo = _make_repo({"a.md": "# A", "b.txt": "hello", "src/c.py": "pass"})
        connector = GitConnector()
        config = GitConfig(url=repo)

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        assert "a.md" in paths
        assert "b.txt" in paths
        assert "src/c.py" in paths

    async def test_discover_respects_include_pattern(self) -> None:
        repo = _make_repo({"a.md": "# A", "b.txt": "hello", "c.py": "pass"})
        connector = GitConnector()
        config = GitConfig(url=repo, path_include=["*.md"])

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        assert paths == ["a.md"]

    async def test_discover_respects_exclude_pattern(self) -> None:
        repo = _make_repo({"a.md": "# A", "b.txt": "hello", "c.py": "pass"})
        connector = GitConnector()
        config = GitConfig(url=repo, path_exclude=["*.txt"])

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        assert "b.txt" not in paths
        assert "a.md" in paths

    async def test_discover_refs_have_required_fields(self) -> None:
        repo = _make_repo()
        connector = GitConnector()
        config = GitConfig(url=repo)

        async for ref in connector.discover(config, {}):
            assert ref.external_id
            assert ref.uri
            assert "path" in ref.metadata
            break

    async def test_discover_uri_contains_repo_and_path(self) -> None:
        repo = _make_repo({"docs/README.md": "# Docs"})
        connector = GitConnector()
        config = GitConfig(url=repo)

        async for ref in connector.discover(config, {}):
            if ref.metadata.get("path") == "docs/README.md":
                assert "docs/README.md" in ref.uri
                break

    async def test_discover_empty_repo_yields_nothing(self) -> None:
        tmp = tempfile.mkdtemp(prefix="omni_empty_")
        _git("init", cwd=tmp)
        _git("config", "user.email", "x@x.com", cwd=tmp)
        _git("config", "user.name", "X", cwd=tmp)

        connector = GitConnector()
        config = GitConfig(url=tmp)
        refs: list[DocumentRef] = []
        try:
            async for ref in connector.discover(config, {}):
                refs.append(ref)
        except RuntimeError:
            pass  # Empty repo has no HEAD; that's acceptable

        assert refs == []


# ---------------------------------------------------------------------------
# GitConnector.fetch
# ---------------------------------------------------------------------------


class TestGitConnectorFetch:
    async def test_fetch_returns_correct_content(self) -> None:
        content_text = "# Hello World\nThis is the README.\n"
        repo = _make_repo({"README.md": content_text})
        connector = GitConnector()
        config = GitConfig(url=repo)

        ref: DocumentRef | None = None
        async for r in connector.discover(config, {}):
            if r.metadata.get("path") == "README.md":
                ref = r
                break
        assert ref is not None

        doc = await connector.fetch(config, {}, ref)
        assert isinstance(doc, FetchedDocument)
        assert doc.content_bytes == content_text.encode()
        assert doc.ref == ref

    async def test_fetch_guesses_content_type_markdown(self) -> None:
        repo = _make_repo({"notes.md": "# Notes"})
        connector = GitConnector()
        config = GitConfig(url=repo)

        async for ref in connector.discover(config, {}):
            doc = await connector.fetch(config, {}, ref)
            assert "markdown" in doc.content_type or "text" in doc.content_type
            break

    async def test_fetch_guesses_content_type_python(self) -> None:
        repo = _make_repo({"app.py": "print('hello')"})
        connector = GitConnector()
        config = GitConfig(url=repo)

        async for ref in connector.discover(config, {}):
            doc = await connector.fetch(config, {}, ref)
            assert "python" in doc.content_type or "text" in doc.content_type
            break

    async def test_fetch_skips_file_exceeding_max_size(self) -> None:
        repo = _make_repo({"big.txt": "x" * 200})
        connector = GitConnector()
        config = GitConfig(url=repo, max_file_size_bytes=100)

        async for ref in connector.discover(config, {}):
            with pytest.raises(ValueError, match="exceeds max_file_size_bytes"):
                await connector.fetch(config, {}, ref)
            break

    async def test_fetch_skips_binary_file(self) -> None:
        tmp = tempfile.mkdtemp(prefix="omni_bin_")
        _git("init", cwd=tmp)
        _git("config", "user.email", "x@x.com", cwd=tmp)
        _git("config", "user.name", "X", cwd=tmp)
        bin_path = Path(tmp) / "image.bin"
        bin_path.write_bytes(b"\x00\x01\x02\x03" * 100)
        _git("add", ".", cwd=tmp)
        _git("commit", "-m", "bin", cwd=tmp)

        connector = GitConnector()
        config = GitConfig(url=tmp)

        async for ref in connector.discover(config, {}):
            if "image.bin" in ref.metadata.get("path", ""):
                with pytest.raises(ValueError, match="binary"):
                    await connector.fetch(config, {}, ref)
                break


# ---------------------------------------------------------------------------
# GitWebhookHandler
# ---------------------------------------------------------------------------


class TestGitWebhookHandlerSignature:
    def _make_handler(self) -> GitWebhookHandler:
        return GitWebhookHandler()

    def _github_headers(self, payload: bytes, secret: str) -> dict[str, str]:
        mac = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return {"x-hub-signature-256": f"sha256={mac}"}

    async def test_github_valid_signature_accepted(self) -> None:
        handler = self._make_handler()
        payload = b'{"ref":"refs/heads/main"}'
        secret = "test-secret"
        headers = self._github_headers(payload, secret)
        assert await handler.verify_signature(payload, headers, secret) is True

    async def test_github_bad_signature_rejected(self) -> None:
        handler = self._make_handler()
        payload = b'{"ref":"refs/heads/main"}'
        headers = {"x-hub-signature-256": "sha256=badbadbadbad"}
        assert await handler.verify_signature(payload, headers, "secret") is False

    async def test_github_tampered_payload_rejected(self) -> None:
        handler = self._make_handler()
        payload = b'{"ref":"refs/heads/main"}'
        secret = "secret"
        headers = self._github_headers(payload, secret)
        tampered = b'{"ref":"refs/heads/evil"}'
        assert await handler.verify_signature(tampered, headers, secret) is False

    async def test_gitlab_valid_token_accepted(self) -> None:
        handler = self._make_handler()
        secret = "my-gitlab-token"
        headers: dict[str, str] = {"x-gitlab-token": secret}
        assert await handler.verify_signature(b"payload", headers, secret) is True

    async def test_gitlab_wrong_token_rejected(self) -> None:
        handler = self._make_handler()
        headers = {"x-gitlab-token": "wrong-token"}
        assert await handler.verify_signature(b"payload", headers, "correct-token") is False

    async def test_no_signature_header_rejected(self) -> None:
        handler = self._make_handler()
        empty_headers: dict[str, str] = {}
        assert await handler.verify_signature(b"payload", empty_headers, "secret") is False

    async def test_github_sig_missing_sha256_prefix_rejected(self) -> None:
        handler = self._make_handler()
        headers = {"x-hub-signature-256": "noprefixhash"}
        assert await handler.verify_signature(b"payload", headers, "secret") is False


class TestGitWebhookHandlerPayload:
    def _make_handler(self) -> GitWebhookHandler:
        return GitWebhookHandler()

    def _github_push(self, added: list[str], modified: list[str], removed: list[str]) -> bytes:
        return json.dumps(
            {
                "ref": "refs/heads/main",
                "repository": {"full_name": "org/repo"},
                "commits": [
                    {
                        "id": "abc123",
                        "added": added,
                        "modified": modified,
                        "removed": removed,
                    }
                ],
            }
        ).encode()

    async def test_parse_github_push_added_files(self) -> None:
        handler = self._make_handler()
        payload = self._github_push(added=["src/new.py"], modified=[], removed=[])
        headers = {"x-github-event": "push"}
        result = await handler.parse_payload(payload, headers)
        uris = [r.uri for r in result.affected_refs]
        assert "src/new.py" in uris

    async def test_parse_github_push_modified_files(self) -> None:
        handler = self._make_handler()
        payload = self._github_push(added=[], modified=["README.md"], removed=[])
        headers: dict[str, str] = {}
        result = await handler.parse_payload(payload, headers)
        assert any(r.uri == "README.md" for r in result.affected_refs)

    async def test_parse_github_push_removed_files(self) -> None:
        handler = self._make_handler()
        payload = self._github_push(added=[], modified=[], removed=["old.txt"])
        headers: dict[str, str] = {}
        result = await handler.parse_payload(payload, headers)
        assert any(r.uri == "old.txt" for r in result.affected_refs)

    async def test_parse_uses_repo_full_name_as_source(self) -> None:
        handler = self._make_handler()
        payload = self._github_push(added=["f.py"], modified=[], removed=[])
        headers = {"x-hub-signature-256": "sha256=dummy"}
        result = await handler.parse_payload(payload, headers)
        assert result.source_name == "org/repo"

    async def test_parse_deduplicates_paths_across_commits(self) -> None:
        handler = self._make_handler()
        data: dict[str, Any] = {
            "commits": [
                {"id": "c1", "added": ["a.py"], "modified": [], "removed": []},
                {"id": "c2", "added": ["a.py"], "modified": [], "removed": []},
            ]
        }
        empty: dict[str, str] = {}
        result = await handler.parse_payload(json.dumps(data).encode(), empty)
        uris = [r.uri for r in result.affected_refs]
        assert uris.count("a.py") == 1

    async def test_parse_empty_commits_yields_no_refs(self) -> None:
        handler = self._make_handler()
        data: dict[str, Any] = {"commits": []}
        empty: dict[str, str] = {}
        result = await handler.parse_payload(json.dumps(data).encode(), empty)
        assert result.affected_refs == []

    async def test_parse_invalid_json_raises_value_error(self) -> None:
        handler = self._make_handler()
        empty: dict[str, str] = {}
        with pytest.raises(ValueError, match="not valid JSON"):
            await handler.parse_payload(b"not-json", empty)

    async def test_raw_headers_stored_lowercase(self) -> None:
        handler = self._make_handler()
        data: dict[str, Any] = {"commits": []}
        payload = json.dumps(data).encode()
        headers = {"X-GitHub-Event": "push", "Content-Type": "application/json"}
        result = await handler.parse_payload(payload, headers)
        assert "x-github-event" in result.raw_headers
        assert "content-type" in result.raw_headers


# ---------------------------------------------------------------------------
# Contract tests for GitConnector
# ---------------------------------------------------------------------------


class TestGitConnectorContract(ConnectorContractTests):
    """Runs the full connector contract against :class:`GitConnector`."""

    _repo: str | None = None

    @classmethod
    def setup_class(cls) -> None:
        cls._repo = _make_repo({"README.md": "# Contract Test Repo\n", "src/app.py": "pass\n"})

    def make_connector(self) -> GitConnector:
        return GitConnector()

    def valid_config(self) -> GitConfig:
        return GitConfig(url=self.__class__._repo or "")

    def invalid_config(self) -> GitConfig:
        return GitConfig(url="/totally/nonexistent/path/abc123")

    def secrets(self) -> dict[str, str]:
        return {}

    async def test_webhook_handler_accepts_valid_signature(self) -> None:
        connector = self.make_connector()
        handler = connector.webhook_handler()
        assert handler is not None

        payload = b'{"ref":"refs/heads/main"}'
        secret = "contract-test-secret"
        mac = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        result = await handler.verify_signature(
            payload=payload,
            headers={"x-hub-signature-256": f"sha256={mac}"},
            secret=secret,
        )
        assert result is True
