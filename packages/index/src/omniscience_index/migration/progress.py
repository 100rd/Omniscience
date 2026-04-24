"""Progress-file I/O for resumable migrations (issue #108 §resume).

The file is a single JSON document living at the path configured in
:class:`~omniscience_index.migration.config.MigrationConfig.progress_file`.
Shape:

.. code-block:: json

    {
      "schema_version": 1,
      "started_at": "2026-04-24T12:00:00+00:00",
      "updated_at": "2026-04-24T12:07:13+00:00",
      "batch_size": 500,
      "last_completed_source_id": "…",
      "completed_source_ids": ["…", "…"]
    }

Source granularity (not chunk granularity) is deliberate: the adapters
are idempotent per-source (``upsert_graph`` replaces by ``source_id``;
``upsert_chunks`` keys on ``(source_id, external_id, content_hash)``),
so a re-run that processes a partially-migrated source is a no-op on
unchanged hashes and a correct replace on changed ones.  Chunk-level
checkpointing would be wasted precision.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# JSON schema version — bump when the shape changes so we can reject
# stale progress files instead of misinterpreting them.
_SCHEMA_VERSION: int = 1


@dataclass(slots=True)
class ProgressState:
    """In-memory progress record, round-trips to :class:`Path` via JSON."""

    batch_size: int
    started_at: datetime
    updated_at: datetime
    last_completed_source_id: uuid.UUID | None = None
    completed_source_ids: list[uuid.UUID] = field(default_factory=list)

    def mark_source_completed(self, source_id: uuid.UUID) -> None:
        """Record one source as done and refresh ``updated_at``."""
        if source_id not in self.completed_source_ids:
            self.completed_source_ids.append(source_id)
        self.last_completed_source_id = source_id
        self.updated_at = datetime.now(UTC)

    def is_completed(self, source_id: uuid.UUID) -> bool:
        """Return True when this source appears in ``completed_source_ids``."""
        return source_id in self.completed_source_ids

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "batch_size": self.batch_size,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_completed_source_id": (
                str(self.last_completed_source_id)
                if self.last_completed_source_id is not None
                else None
            ),
            "completed_source_ids": [str(sid) for sid in self.completed_source_ids],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ProgressState:
        """Build a :class:`ProgressState` from a JSON-loaded dict.

        Rejects unknown schema versions loudly — a mid-upgrade operator
        should see a clear error instead of silently corrupted state.
        """
        version = data.get("schema_version")
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported progress-file schema_version={version!r}; "
                f"expected {_SCHEMA_VERSION}."
            )
        raw_last = data.get("last_completed_source_id")
        raw_completed = data.get("completed_source_ids")
        if not isinstance(raw_completed, list):
            raise ValueError("completed_source_ids must be a list")
        last_uuid = uuid.UUID(raw_last) if isinstance(raw_last, str) else None
        completed = [uuid.UUID(str(s)) for s in raw_completed]
        raw_batch = data.get("batch_size")
        if not isinstance(raw_batch, int):
            raise ValueError(f"batch_size must be int, got {type(raw_batch).__name__}")
        return cls(
            batch_size=raw_batch,
            started_at=datetime.fromisoformat(str(data["started_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            last_completed_source_id=last_uuid,
            completed_source_ids=completed,
        )


def load_progress(path: Path) -> ProgressState | None:
    """Read a progress file.  Returns ``None`` when the file is absent.

    Any other read error (malformed JSON, wrong schema) is raised — the
    caller must decide whether to abort or bail.  We never silently
    ignore a broken progress file because that would cause the runner
    to re-process sources an operator thought were done.
    """
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    data: object = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Progress file {path} does not contain a JSON object")
    return ProgressState.from_dict(data)


def save_progress(state: ProgressState, path: Path) -> None:
    """Atomically write a progress file at ``path``.

    The write goes to ``<path>.tmp`` first and is then renamed so a
    crash mid-write cannot leave the runner with a partial JSON doc.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def new_progress_state(*, batch_size: int) -> ProgressState:
    """Build a fresh :class:`ProgressState` with ``started_at=now``."""
    now = datetime.now(UTC)
    return ProgressState(
        batch_size=batch_size,
        started_at=now,
        updated_at=now,
    )


__all__ = [
    "ProgressState",
    "load_progress",
    "new_progress_state",
    "save_progress",
]
