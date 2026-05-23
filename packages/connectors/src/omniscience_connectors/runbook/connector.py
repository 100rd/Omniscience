"""Runbook source connector (issue #231).

Discovers ``*.md`` / ``*.markdown`` files under a configured root, parses
each into a :class:`RunbookDocument`, and emits one
:class:`DocumentRef` per runbook with the parsed structure stashed in
``metadata`` so downstream ingestion produces:

* one ``runbook://`` entity per file,
* one ``runbook://...#step`` entity per heading,
* ``MATCHES`` edges (added by the entity linker, not here) from any alert
  whose canonical name matches the front-matter ``alert_names`` /
  ``alert_patterns``.

This connector is **read-only** and pull-based.  Webhook-style updates
should arrive via the existing git or fs connector and re-trigger a fetch;
the dedicated handler is intentionally not implemented here to keep this
connector single-purpose (parse markdown, surface structure).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
)
from omniscience_connectors.runbook.models import (
    RunbookDocument,
    canonical_runbook_name,
)
from omniscience_connectors.runbook.parser import ParseError, parse_runbook

__all__ = [
    "DEFAULT_RUNBOOK_GLOBS",
    "RunbookConfig",
    "RunbookConnector",
]

logger = logging.getLogger(__name__)

# Default file extensions a runbook author would reasonably use.  ``.mdx``
# is intentionally included so doc-site-hosted runbooks (Docusaurus,
# Nextra) can be ingested without renaming.
DEFAULT_RUNBOOK_GLOBS: tuple[str, ...] = ("*.md", "*.markdown", "*.mdx")

# Hard cap per file (1 MiB).  Runbooks should not realistically exceed this;
# a larger file is almost certainly a misconfiguration.
_MAX_FILE_SIZE_BYTES: int = 1 * 1024 * 1024


class RunbookConfig(BaseModel):
    """Public configuration for the runbook connector (no secrets)."""

    root_path: str = Field(description="Absolute path to the runbook directory tree.")
    source_uid: str = Field(
        description=(
            "Short, stable identifier for this runbook source.  Used as the "
            "first segment of the canonical runbook name so multiple runbook "
            "sources (one per team, one per repo) can coexist without "
            "colliding on relative paths."
        ),
    )
    include_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RUNBOOK_GLOBS),
        description="Filename globs that count as runbooks (matched on basename).",
    )
    exclude_globs: list[str] = Field(
        default_factory=list,
        description="Path globs to exclude (matched on POSIX-relative path).",
    )
    follow_symlinks: bool = False

    @field_validator("source_uid")
    @classmethod
    def _validate_source_uid(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("source_uid must be non-empty and not surrounded by whitespace")
        return value


def _path_is_within(root: Path, candidate: Path) -> bool:
    """Containment check — same shape as the fs connector's guard."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _basename_match(name: str, patterns: list[str]) -> bool:
    """Filename-basename glob match."""
    import fnmatch as _fn

    return any(_fn.fnmatchcase(name, pat) for pat in patterns)


def _path_excluded(rel_posix: str, patterns: list[str]) -> bool:
    """POSIX-relative path glob match."""
    import fnmatch as _fn

    return any(_fn.fnmatchcase(rel_posix, pat) for pat in patterns)


class RunbookConnector(Connector):
    """Connector for markdown runbooks on local disk.

    Stateless: all configuration is passed per call so a single instance
    can serve many sources.  Symmetric with :class:`FsConnector`.
    """

    connector_type: ClassVar[str] = "runbook"
    config_schema: ClassVar[type[BaseModel]] = RunbookConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Check the root path exists, is a directory, and is readable."""
        cfg = self._cfg(config)
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
        """Yield one DocumentRef per matching runbook file."""
        cfg = self._cfg(config)
        root = Path(cfg.root_path).resolve()

        for file_path in _walk(root, cfg.follow_symlinks):
            resolved = file_path.resolve()
            if not _path_is_within(root, resolved):
                logger.warning(
                    "runbook.connector.traversal_attempt",
                    extra={"path": str(file_path)},
                )
                continue

            rel = resolved.relative_to(root)
            rel_posix = rel.as_posix()

            if not _basename_match(resolved.name, cfg.include_globs):
                continue
            if cfg.exclude_globs and _path_excluded(rel_posix, cfg.exclude_globs):
                continue

            try:
                stat = resolved.stat()
            except OSError:
                continue
            if stat.st_size > _MAX_FILE_SIZE_BYTES:
                logger.debug(
                    "runbook.connector.skip_large_file",
                    extra={"path": rel_posix, "size": stat.st_size},
                )
                continue

            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            yield DocumentRef(
                external_id=str(resolved),
                uri=canonical_runbook_name(cfg.source_uid, rel_posix),
                updated_at=mtime,
                metadata={
                    "path": rel_posix,
                    "size": stat.st_size,
                    "source_uid": cfg.source_uid,
                    "content_type": "text/markdown",
                    "edges": [],
                },
            )

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Read a runbook file, parse it, and return the parsed JSON payload.

        Unlike the plain fs connector we do not return raw bytes — the
        parsed :class:`RunbookDocument` is the canonical artefact for
        downstream indexing.  Raw markdown is preserved on the
        :attr:`RunbookDocument.body_markdown` slot.
        """
        cfg = self._cfg(config)
        root = Path(cfg.root_path).resolve()
        file_path = Path(ref.external_id).resolve()

        if not _path_is_within(root, file_path):
            raise ValueError(
                f"Path {ref.external_id!r} is outside the configured root {cfg.root_path!r}"
            )

        stat = file_path.stat()
        if stat.st_size > _MAX_FILE_SIZE_BYTES:
            raise ValueError(
                f"Runbook {ref.external_id!r} ({stat.st_size} bytes) exceeds "
                f"max size {_MAX_FILE_SIZE_BYTES}"
            )

        text = file_path.read_text(encoding="utf-8", errors="replace")
        rel_posix_obj = ref.metadata.get("path", file_path.name)
        rel_posix = str(rel_posix_obj)

        try:
            parsed = parse_runbook(
                source_uid=cfg.source_uid,
                rel_path=rel_posix,
                text=text,
            )
        except ParseError:
            logger.warning(
                "runbook.connector.unparseable",
                extra={"path": rel_posix},
            )
            # Emit a placeholder document so ingestion can record the entity
            # but mark zero steps — the matcher will score 0 against it.
            parsed = RunbookDocument(
                source_uid=cfg.source_uid,
                rel_path=rel_posix,
                canonical_name=canonical_runbook_name(cfg.source_uid, rel_posix),
                front_matter={},  # type: ignore[arg-type]
                raw_front_matter={},
                title=rel_posix.rsplit("/", 1)[-1],
                body_markdown="",
                steps=[],
                parse_warnings=["empty_or_unparseable"],
            )

        payload = parsed.model_dump_json().encode("utf-8")
        return FetchedDocument(
            ref=ref,
            content_bytes=payload,
            content_type="application/vnd.omniscience.runbook+json",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        """No webhook surface — file watcher / git pull drives re-ingest."""
        return None

    # ----- helpers -----

    @staticmethod
    def _cfg(config: BaseModel) -> RunbookConfig:
        """Narrow ``BaseModel`` to :class:`RunbookConfig` for mypy."""
        if not isinstance(config, RunbookConfig):
            raise TypeError(
                f"RunbookConnector requires RunbookConfig, got {type(config).__name__}"
            )
        return config


def _walk(root: Path, follow_symlinks: bool) -> list[Path]:
    """Return every regular file under *root* (hidden files / dirs skipped)."""
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks, topdown=True):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            results.append(Path(dirpath) / fname)
    return results
