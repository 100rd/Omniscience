"""Filesystem source connector.

Discovers and fetches files from a local directory tree using pathlib.
Path traversal is prevented by resolving all paths and verifying containment.
"""

from __future__ import annotations

import fnmatch
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, Field

from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument, WebhookHandler

__all__ = ["FsConfig", "FsConnector"]

logger = logging.getLogger(__name__)

# Sentinel pattern meaning "match everything"
_MATCH_ALL = {"**/*", "**", "*"}


class FsConfig(BaseModel):
    """Public configuration for the filesystem connector (no secrets)."""

    root_path: str
    """Absolute path to the root directory to index."""

    include_globs: list[str] = Field(default_factory=lambda: ["**/*"])
    """Glob patterns to include.  Matched relative to *root_path*."""

    exclude_globs: list[str] = Field(default_factory=list)
    """Glob patterns to exclude.  Matched relative to *root_path*."""

    follow_symlinks: bool = False
    """Whether to follow symbolic links during directory traversal."""

    max_file_size_bytes: int = 1_000_000
    """Files larger than this are skipped during discovery and fetch (bytes)."""


def _path_is_within(root: Path, candidate: Path) -> bool:
    """Return True when *candidate* is inside *root* (prevents traversal)."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _pattern_matches(rel_posix: str, pattern: str) -> bool:
    """Return True if *rel_posix* matches *pattern*.

    Supports:
    - ``**/*`` / ``**`` / ``*`` — match everything
    - Simple globs: ``*.md``, ``*.txt``
    - Path globs: ``src/**``, ``docs/*.md``
    - pathlib-style matching for patterns containing ``/``
    """
    if pattern in _MATCH_ALL:
        return True
    # Use pathlib for patterns containing path separators or **
    if "/" in pattern or "**" in pattern:
        return PurePosixPath(rel_posix).match(pattern)
    # Simple extension/name glob: check against basename and full path
    name = rel_posix.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_posix, pattern)


def _rel_matches(rel_posix: str, patterns: list[str]) -> bool:
    """Return True if *rel_posix* matches any of the given patterns."""
    return any(_pattern_matches(rel_posix, p) for p in patterns)


def _content_type(path: Path) -> str:
    """Guess MIME type from file extension; default to text/plain."""
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "text/plain"


class FsConnector(Connector):
    """Connector for local filesystem directories.

    Stateless: configuration is passed at call time so one instance can serve
    multiple source records simultaneously.
    """

    connector_type: ClassVar[str] = "fs"
    config_schema: ClassVar[type[BaseModel]] = FsConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the root path exists and is a readable directory."""
        cfg: FsConfig = config  # type: ignore[assignment]
        root = Path(cfg.root_path).resolve()
        if not root.exists():
            raise ValueError(f"root_path {cfg.root_path!r} does not exist")
        if not root.is_dir():
            raise ValueError(f"root_path {cfg.root_path!r} is not a directory")
        if not os.access(root, os.R_OK):
            raise ValueError(f"root_path {cfg.root_path!r} is not readable")

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Walk the filesystem and yield DocumentRef for each matching file."""
        cfg: FsConfig = config  # type: ignore[assignment]
        root = Path(cfg.root_path).resolve()

        for file_path in _walk(root, cfg.follow_symlinks):
            # Safety: prevent path traversal
            if not _path_is_within(root, file_path.resolve()):
                logger.warning("fs.connector.traversal_attempt", extra={"path": str(file_path)})
                continue

            rel = file_path.relative_to(root)
            rel_posix = rel.as_posix()

            # Apply include/exclude filters
            if not _rel_matches(rel_posix, cfg.include_globs):
                continue
            if cfg.exclude_globs and _rel_matches(rel_posix, cfg.exclude_globs):
                continue

            try:
                stat = file_path.stat()
            except OSError:
                continue

            if stat.st_size > cfg.max_file_size_bytes:
                logger.debug(
                    "fs.connector.skip_large_file",
                    extra={"path": rel_posix, "size": stat.st_size},
                )
                continue

            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            yield DocumentRef(
                external_id=str(file_path.resolve()),
                uri=str(file_path.resolve()),
                updated_at=mtime,
                metadata={
                    "path": rel_posix,
                    "size": stat.st_size,
                    "content_type": _content_type(file_path),
                },
            )

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Read and return file content for the given DocumentRef."""
        cfg: FsConfig = config  # type: ignore[assignment]
        root = Path(cfg.root_path).resolve()

        # The uri field stores the resolved absolute path
        file_path = Path(ref.uri).resolve()

        # Safety: prevent path traversal
        if not _path_is_within(root, file_path):
            raise ValueError(
                f"Path {ref.uri!r} is outside the configured root {cfg.root_path!r}"
            )

        stat = file_path.stat()
        if stat.st_size > cfg.max_file_size_bytes:
            raise ValueError(
                f"File {ref.uri!r} ({stat.st_size} bytes) exceeds "
                f"max_file_size_bytes={cfg.max_file_size_bytes}"
            )

        content = file_path.read_bytes()
        content_type = ref.metadata.get("content_type") or _content_type(file_path)
        return FetchedDocument(ref=ref, content_bytes=content, content_type=content_type)

    def webhook_handler(self) -> WebhookHandler | None:
        """Filesystem connector uses a watcher, not webhooks."""
        return None


def _walk(root: Path, follow_symlinks: bool) -> list[Path]:
    """Return a list of all regular files under *root*."""
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks, topdown=True):
        # Remove hidden directories in-place to skip them
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            results.append(Path(dirpath) / fname)
    return results
