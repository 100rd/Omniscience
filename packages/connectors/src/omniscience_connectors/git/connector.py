"""Git source connector.

Discovers and fetches files from a git repository (local or remote).
Uses subprocess calls to git with list-form arguments to prevent shell injection.
"""

from __future__ import annotations

import fnmatch
import logging
import mimetypes
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument, WebhookHandler
from omniscience_connectors.git.webhook import GitWebhookHandler

__all__ = ["GitConfig", "GitConnector"]

logger = logging.getLogger(__name__)

# Byte sequences that indicate a binary file (null byte is the most reliable)
_BINARY_HEURISTIC_BYTES = 8192
_NULL_BYTE = b"\x00"


def _is_binary(data: bytes) -> bool:
    """Return True if *data* appears to be binary content."""
    return _NULL_BYTE in data[:_BINARY_HEURISTIC_BYTES]


def _run_git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run a git command; raises ``RuntimeError`` on non-zero exit.

    Uses list-form args (never shell=True) to prevent shell injection.
    """
    cmd = ["git", *args]
    result = subprocess.run(  # noqa: S603
        cmd,
        cwd=cwd,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {args[0]} failed: {stderr}")
    return result


def _matches_patterns(path: str, patterns: list[str]) -> bool:
    """Return True if *path* matches any of the given glob patterns."""
    return any(fnmatch.fnmatch(path, p) for p in patterns)


class GitConfig(BaseModel):
    """Public configuration for the git connector (no secrets)."""

    url: str
    """Local filesystem path or remote URL (HTTPS/SSH)."""

    ref: str = "HEAD"
    """Git ref (branch, tag, commit SHA) to discover files from."""

    path_include: list[str] = Field(default_factory=list)
    """Glob patterns for files to include.  Empty = include everything."""

    path_exclude: list[str] = Field(default_factory=list)
    """Glob patterns for files to exclude."""

    max_file_size_bytes: int = 1_000_000
    """Files larger than this are skipped during fetch (bytes)."""

    webhook_secret: str | None = None
    """Webhook secret for verifying GitHub/GitLab push events.

    Note: treat this as *configuration* (not a runtime secret) for local
    validation convenience.  The ingestion pipeline also passes it via
    secrets dict at runtime.
    """


def _resolve_repo(url: str) -> tuple[str, str | None]:
    """Return (repo_path, tmp_dir_or_None).

    For local paths: returns the path as-is, no tmp dir.
    For remote URLs: clones into a temp dir and returns that path + temp dir.
    """
    p = Path(url)
    if p.exists() and p.is_dir():
        return str(p), None

    tmp = tempfile.mkdtemp(prefix="omniscience_git_")
    _run_git(["clone", "--depth=1", "--no-tags", url, tmp])
    return tmp, tmp


class GitConnector(Connector):
    """Connector for git repositories (local or remote).

    Stateless: all state is derived from config at call time so one instance
    can serve multiple source records simultaneously.
    """

    connector_type: ClassVar[str] = "git"
    config_schema: ClassVar[type[BaseModel]] = GitConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the repository is accessible and the ref exists."""
        cfg: GitConfig = config  # type: ignore[assignment]

        p = Path(cfg.url)
        if p.exists():
            # Local repo: verify it is a git repository
            _run_git(["rev-parse", "--git-dir"], cwd=str(p))
            _run_git(["rev-parse", "--verify", cfg.ref], cwd=str(p))
        else:
            # Remote repo: perform a lightweight ls-remote check
            env_token = secrets.get("token") or secrets.get("password")
            url = cfg.url
            if env_token and url.startswith("https://"):
                # Inject token into URL for authentication
                url = url.replace("https://", f"https://oauth2:{env_token}@", 1)
            result = subprocess.run(  # noqa: S603
                ["git", "ls-remote", url, cfg.ref],  # noqa: S607
                capture_output=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Cannot access repository {cfg.url!r}: "
                    + result.stderr.decode(errors="replace").strip()
                )

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield a DocumentRef for every file matching the include/exclude patterns."""
        cfg: GitConfig = config  # type: ignore[assignment]
        repo_path, tmp_dir = _resolve_repo(cfg.url)
        try:
            result = _run_git(
                ["ls-tree", "-r", "--name-only", cfg.ref],
                cwd=repo_path,
            )
            for raw_path in result.stdout.splitlines():
                rel_path = raw_path.decode(errors="replace")

                if cfg.path_include and not _matches_patterns(rel_path, cfg.path_include):
                    continue
                if cfg.path_exclude and _matches_patterns(rel_path, cfg.path_exclude):
                    continue

                blob_sha = _get_blob_sha(repo_path, cfg.ref, rel_path)
                uri = f"git+{cfg.url}#{cfg.ref}:{rel_path}"
                yield DocumentRef(
                    external_id=blob_sha,
                    uri=uri,
                    metadata={"path": rel_path, "ref": cfg.ref, "repo": cfg.url},
                )
        finally:
            if tmp_dir:
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch file content for the given DocumentRef."""
        cfg: GitConfig = config  # type: ignore[assignment]
        rel_path = ref.metadata.get("path", "")
        git_ref = ref.metadata.get("ref", cfg.ref)

        repo_path, tmp_dir = _resolve_repo(cfg.url)
        try:
            result = _run_git(["show", f"{git_ref}:{rel_path}"], cwd=repo_path)
        finally:
            if tmp_dir:
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)

        content = result.stdout
        if len(content) > cfg.max_file_size_bytes:
            raise ValueError(
                f"File {rel_path!r} ({len(content)} bytes) exceeds "
                f"max_file_size_bytes={cfg.max_file_size_bytes}"
            )
        if _is_binary(content):
            raise ValueError(f"File {rel_path!r} appears to be binary; skipping")

        content_type = _guess_content_type(rel_path)
        return FetchedDocument(ref=ref, content_bytes=content, content_type=content_type)

    def webhook_handler(self) -> WebhookHandler | None:
        return GitWebhookHandler()


def _get_blob_sha(repo_path: str, ref: str, rel_path: str) -> str:
    """Return the git blob SHA for *rel_path* at *ref*."""
    result = _run_git(["rev-parse", f"{ref}:{rel_path}"], cwd=repo_path)
    return result.stdout.decode().strip()


def _guess_content_type(path: str) -> str:
    """Guess MIME type from file extension; fall back to text/plain."""
    mime, _ = mimetypes.guess_type(path)
    return mime or "text/plain"
