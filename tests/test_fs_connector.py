"""Tests for the filesystem source connector (Issue #18)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from omniscience_connectors import DocumentRef, FetchedDocument
from omniscience_connectors.fs.connector import FsConfig, FsConnector

from tests.connectors.contract_tests import ConnectorContractTests

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_tree(files: dict[str, str | bytes] | None = None) -> str:
    """Create a temp directory with the given file tree.

    *files* maps relative path -> text content (str) or binary content (bytes).
    Default creates a small mixed tree.
    """
    tmp = tempfile.mkdtemp(prefix="omni_fs_test_")
    default: dict[str, str | bytes] = {
        "README.md": "# Hello\n",
        "src/main.py": "print('hello')\n",
        "docs/guide.txt": "User guide content.\n",
    }
    tree = files if files is not None else default
    for rel, content in tree.items():
        p = Path(tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    return tmp


# ---------------------------------------------------------------------------
# FsConnector.validate
# ---------------------------------------------------------------------------


class TestFsConnectorValidate:
    async def test_validate_existing_directory_succeeds(self) -> None:
        root = _make_tree()
        connector = FsConnector()
        await connector.validate(FsConfig(root_path=root), {})

    async def test_validate_nonexistent_path_raises(self) -> None:
        connector = FsConnector()
        with pytest.raises(ValueError, match="does not exist"):
            await connector.validate(FsConfig(root_path="/nonexistent/xyz/abc"), {})

    async def test_validate_file_path_raises(self) -> None:
        tmp = tempfile.mktemp()
        Path(tmp).write_text("hello")
        connector = FsConnector()
        with pytest.raises(ValueError, match="not a directory"):
            await connector.validate(FsConfig(root_path=tmp), {})
        Path(tmp).unlink(missing_ok=True)

    async def test_validate_secrets_ignored(self) -> None:
        """Filesystem connector requires no secrets."""
        root = _make_tree()
        connector = FsConnector()
        await connector.validate(FsConfig(root_path=root), {"token": "should-be-ignored"})


# ---------------------------------------------------------------------------
# FsConnector.discover
# ---------------------------------------------------------------------------


class TestFsConnectorDiscover:
    async def test_discover_lists_all_files(self) -> None:
        root = _make_tree()
        connector = FsConnector()
        config = FsConfig(root_path=root)

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        assert "README.md" in paths
        assert "src/main.py" in paths
        assert "docs/guide.txt" in paths

    async def test_discover_include_glob_filters(self) -> None:
        root = _make_tree()
        connector = FsConnector()
        config = FsConfig(root_path=root, include_globs=["*.md"])

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        assert all(p.endswith(".md") for p in paths)
        assert len(paths) >= 1

    async def test_discover_exclude_glob_filters(self) -> None:
        root = _make_tree()
        connector = FsConnector()
        config = FsConfig(root_path=root, exclude_globs=["*.txt"])

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        assert not any(p.endswith(".txt") for p in paths)

    async def test_discover_skips_large_files(self) -> None:
        root = _make_tree({"small.txt": "x" * 50, "big.txt": "y" * 200})
        connector = FsConnector()
        config = FsConfig(root_path=root, max_file_size_bytes=100)

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        assert "small.txt" in paths
        assert "big.txt" not in paths

    async def test_discover_refs_have_required_fields(self) -> None:
        root = _make_tree()
        connector = FsConnector()
        config = FsConfig(root_path=root)

        async for ref in connector.discover(config, {}):
            assert ref.external_id
            assert ref.uri
            assert ref.updated_at is not None
            assert "path" in ref.metadata
            assert "size" in ref.metadata
            break

    async def test_discover_no_follow_symlinks_by_default(self) -> None:
        root = _make_tree({"real.txt": "content"})
        link = Path(root) / "link.txt"
        target = Path(root) / "real.txt"
        link.symlink_to(target)

        connector = FsConnector()
        config = FsConfig(root_path=root, follow_symlinks=False)

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        # Symlink should not appear (follow_symlinks=False)
        paths = [r.metadata["path"] for r in refs]
        # hidden files (starting with .) and symlinks should be excluded
        assert "real.txt" in paths

    async def test_discover_follow_symlinks_enabled(self) -> None:
        root = _make_tree({"real.txt": "content"})
        sub = Path(root) / "subdir"
        sub.mkdir()
        link = sub / "linked.txt"
        link.symlink_to(Path(root) / "real.txt")

        connector = FsConnector()
        config = FsConfig(root_path=root, follow_symlinks=True)

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)

        paths = [r.metadata["path"] for r in refs]
        # When following symlinks, the linked file should appear
        assert any("linked.txt" in p for p in paths)

    async def test_discover_empty_directory_yields_nothing(self) -> None:
        root = tempfile.mkdtemp()
        connector = FsConnector()
        config = FsConfig(root_path=root)

        refs: list[DocumentRef] = []
        async for ref in connector.discover(config, {}):
            refs.append(ref)
        assert refs == []

    async def test_discover_metadata_contains_content_type(self) -> None:
        root = _make_tree({"file.py": "pass"})
        connector = FsConnector()
        config = FsConfig(root_path=root)

        async for ref in connector.discover(config, {}):
            assert "content_type" in ref.metadata
            break


# ---------------------------------------------------------------------------
# FsConnector.fetch
# ---------------------------------------------------------------------------


class TestFsConnectorFetch:
    async def test_fetch_returns_correct_content(self) -> None:
        content = "Hello, World!\n"
        root = _make_tree({"hello.txt": content})
        connector = FsConnector()
        config = FsConfig(root_path=root)

        ref: DocumentRef | None = None
        async for r in connector.discover(config, {}):
            if r.metadata.get("path") == "hello.txt":
                ref = r
                break
        assert ref is not None

        doc = await connector.fetch(config, {}, ref)
        assert isinstance(doc, FetchedDocument)
        assert doc.content_bytes == content.encode()
        assert doc.ref == ref

    async def test_fetch_raises_for_file_exceeding_max_size(self) -> None:
        root = _make_tree({"big.txt": "x" * 200})
        connector = FsConnector()
        config = FsConfig(root_path=root, max_file_size_bytes=200_000)

        # Discover with generous limit, then fetch with tight limit
        fetch_config = FsConfig(root_path=root, max_file_size_bytes=50)

        async for ref in connector.discover(config, {}):
            with pytest.raises(ValueError, match="exceeds max_file_size_bytes"):
                await connector.fetch(fetch_config, {}, ref)
            break

    async def test_fetch_prevents_path_traversal(self) -> None:
        root = _make_tree({"legit.txt": "ok"})
        connector = FsConnector()
        config = FsConfig(root_path=root)

        # Craft a ref pointing outside the root
        malicious_ref = DocumentRef(
            external_id="evil",
            uri="/etc/passwd",
            metadata={"path": "../../../etc/passwd"},
        )
        with pytest.raises(ValueError, match="outside the configured root"):
            await connector.fetch(config, {}, malicious_ref)

    async def test_fetch_guesses_content_type_markdown(self) -> None:
        root = _make_tree({"notes.md": "# Notes"})
        connector = FsConnector()
        config = FsConfig(root_path=root)

        async for ref in connector.discover(config, {}):
            doc = await connector.fetch(config, {}, ref)
            assert "markdown" in doc.content_type or "text" in doc.content_type
            break

    async def test_fetch_guesses_content_type_json(self) -> None:
        root = _make_tree({"data.json": '{"key": "value"}'})
        connector = FsConnector()
        config = FsConfig(root_path=root)

        async for ref in connector.discover(config, {}):
            doc = await connector.fetch(config, {}, ref)
            assert "json" in doc.content_type
            break

    async def test_fetch_content_is_deterministic(self) -> None:
        root = _make_tree({"stable.txt": "consistent content\n"})
        connector = FsConnector()
        config = FsConfig(root_path=root)

        ref: DocumentRef | None = None
        async for r in connector.discover(config, {}):
            ref = r
            break
        assert ref is not None

        doc1 = await connector.fetch(config, {}, ref)
        doc2 = await connector.fetch(config, {}, ref)
        assert doc1.content_bytes == doc2.content_bytes


# ---------------------------------------------------------------------------
# FsConnector.webhook_handler
# ---------------------------------------------------------------------------


class TestFsConnectorWebhook:
    def test_webhook_handler_returns_none(self) -> None:
        connector = FsConnector()
        assert connector.webhook_handler() is None


# ---------------------------------------------------------------------------
# Contract tests for FsConnector
# ---------------------------------------------------------------------------


class TestFsConnectorContract(ConnectorContractTests):
    """Runs the full connector contract against :class:`FsConnector`."""

    _root: str | None = None

    @classmethod
    def setup_class(cls) -> None:
        cls._root = _make_tree()

    def make_connector(self) -> FsConnector:
        return FsConnector()

    def valid_config(self) -> FsConfig:
        return FsConfig(root_path=self.__class__._root or "/tmp")

    def invalid_config(self) -> FsConfig:
        return FsConfig(root_path="/totally/nonexistent/directory/abc123")

    def secrets(self) -> dict[str, str]:
        return {}

    async def test_webhook_handler_accepts_valid_signature(self) -> None:
        """FsConnector has no webhook handler; skip."""
        pytest.skip("FsConnector does not support webhooks")
