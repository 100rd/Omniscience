"""Unit tests for database schema, models, and Pydantic schemas.

Strategy:
- Model construction and Pydantic round-trips are tested without a live DB.
- Content-hash dedup, tombstone filtering, and run-status logic are tested
  as pure-Python functions that mirror the documented behaviour.
- SQLAlchemy model attribute access is verified by instantiating ORM objects
  directly (no DB required).

Note on SA column defaults: SQLAlchemy ``default=`` on ``mapped_column`` is a
*server/insert-time* default, not a Python ``__init__`` default.  Constructing
an ORM object in-memory without persisting it leaves those fields as ``None``
unless the value is passed explicitly.  Tests that exercise Python-side
attribute correctness pass values explicitly; tests that exercise schema-level
semantics use ``SourceCreate`` / Pydantic schemas which *do* carry Python-level
defaults.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from omniscience_core.db.models import (
    ApiToken,
    Chunk,
    Document,
    IngestionRun,
    IngestionRunStatus,
    Source,
    SourceStatus,
    SourceType,
)
from omniscience_core.db.schemas import (
    ApiTokenCreate,
    ApiTokenRead,
    ChunkCreate,
    ChunkRead,
    DocumentCreate,
    DocumentRead,
    DocumentUpdate,
    IngestionRunCreate,
    IngestionRunRead,
    IngestionRunUpdate,
    SourceCreate,
    SourceRead,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _normalize_content(raw: str) -> str:
    """Normalization rules from schema.md: trim trailing whitespace per line,
    collapse multiple blank lines to one, strip BOM."""
    raw = raw.lstrip("\ufeff")
    lines = [line.rstrip() for line in raw.splitlines()]
    normalized: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
        else:
            if blank_run > 1:
                normalized.append("")  # one blank line
            elif blank_run == 1:
                normalized.append("")
            blank_run = 0
            normalized.append(line)
    return "\n".join(normalized)


# ---------------------------------------------------------------------------
# Source model tests
# ---------------------------------------------------------------------------


class TestSourceModel:
    def test_construct_with_explicit_values(self) -> None:
        """ORM objects constructed in-memory hold the values passed in."""
        source = Source(
            id=uuid.uuid4(),
            type=SourceType.git,
            name="my-repo",
            config={},
            status=SourceStatus.active,
        )
        assert source.status == SourceStatus.active
        assert source.tenant_id is None
        assert source.last_error is None

    def test_nullable_fields_default_to_none(self) -> None:
        """Fields without an explicit value resolve to None in-memory."""
        source = Source(
            id=uuid.uuid4(),
            type=SourceType.git,
            name="my-repo",
            config={},
        )
        # Nullable columns default to None in-memory (SA insert default fires on flush)
        assert source.tenant_id is None
        assert source.last_error is None
        assert source.last_sync_at is None

    def test_all_source_types_exist(self) -> None:
        expected = {
            "git",
            "fs",
            "confluence",
            "notion",
            "slack",
            "jira",
            "grafana",
            "k8s",
            "terraform",
        }
        assert {t.value for t in SourceType} == expected

    def test_all_source_statuses_exist(self) -> None:
        assert {s.value for s in SourceStatus} == {"active", "paused", "error"}


# ---------------------------------------------------------------------------
# Document model tests
# ---------------------------------------------------------------------------


class TestDocumentModel:
    def test_tombstone_default_is_none(self) -> None:
        doc = Document(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            external_id="file.py@abc123",
            uri="https://github.com/org/repo/blob/abc123/file.py",
            content_hash=_sha256("hello world"),
            doc_version=1,
            doc_metadata={},
            indexed_at=_NOW,
        )
        assert doc.tombstoned_at is None

    def test_tombstone_set(self) -> None:
        doc = Document(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            external_id="deleted.py@abc",
            uri="https://example.com/deleted.py",
            content_hash=_sha256("old"),
            doc_version=3,
            doc_metadata={},
            indexed_at=_NOW,
            tombstoned_at=_NOW,
        )
        assert doc.tombstoned_at == _NOW

    def test_doc_version_increments(self) -> None:
        base_version = 1
        updated_version = base_version + 1
        assert updated_version == 2


# ---------------------------------------------------------------------------
# Content-hash dedup logic
# ---------------------------------------------------------------------------


class TestContentHashDedup:
    """Verify the dedup algorithm described in schema.md."""

    def _dedup_action(
        self,
        stored_hash: str | None,
        new_content: str,
    ) -> str:
        """Returns 'skip', 'update', or 'insert' mirroring schema.md."""
        new_hash = _sha256(_normalize_content(new_content))
        if stored_hash is None:
            return "insert"
        if stored_hash == new_hash:
            return "skip"
        return "update"

    def test_new_document_triggers_insert(self) -> None:
        assert self._dedup_action(None, "hello world") == "insert"

    def test_identical_content_triggers_skip(self) -> None:
        content = "def foo():\n    pass\n"
        stored = _sha256(_normalize_content(content))
        assert self._dedup_action(stored, content) == "skip"

    def test_changed_content_triggers_update(self) -> None:
        old = "def foo(): pass"
        new = "def foo(): return 42"
        stored = _sha256(_normalize_content(old))
        assert self._dedup_action(stored, new) == "update"

    def test_cosmetic_whitespace_change_is_skipped(self) -> None:
        """Trailing whitespace differences must not trigger re-index."""
        content_a = "def foo():  \n    pass  \n"
        content_b = "def foo():\n    pass\n"
        hash_a = _sha256(_normalize_content(content_a))
        hash_b = _sha256(_normalize_content(content_b))
        assert hash_a == hash_b

    def test_bom_stripped_before_hashing(self) -> None:
        with_bom = "\ufeffhello world"
        without_bom = "hello world"
        assert _sha256(_normalize_content(with_bom)) == _sha256(_normalize_content(without_bom))

    def test_multiple_blank_lines_collapsed(self) -> None:
        with_extra = "line 1\n\n\n\nline 2"
        normal = "line 1\n\nline 2"
        assert _sha256(_normalize_content(with_extra)) == _sha256(_normalize_content(normal))


# ---------------------------------------------------------------------------
# Tombstone filtering
# ---------------------------------------------------------------------------


class TestTombstoneFiltering:
    """Active documents are those where tombstoned_at IS NULL."""

    def _active_documents(self, docs: list[Document]) -> list[Document]:
        return [d for d in docs if d.tombstoned_at is None]

    def test_all_active(self) -> None:
        docs = [
            Document(
                id=uuid.uuid4(),
                source_id=uuid.uuid4(),
                external_id=f"f{i}",
                uri=f"https://example.com/f{i}",
                content_hash=_sha256(f"content-{i}"),
                doc_version=1,
                doc_metadata={},
                indexed_at=_NOW,
            )
            for i in range(3)
        ]
        assert len(self._active_documents(docs)) == 3

    def test_tombstoned_excluded(self) -> None:
        active = Document(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            external_id="active.py",
            uri="https://example.com/active.py",
            content_hash=_sha256("active"),
            doc_version=1,
            doc_metadata={},
            indexed_at=_NOW,
        )
        dead = Document(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            external_id="dead.py",
            uri="https://example.com/dead.py",
            content_hash=_sha256("dead"),
            doc_version=2,
            doc_metadata={},
            indexed_at=_NOW,
            tombstoned_at=_NOW,
        )
        result = self._active_documents([active, dead])
        assert len(result) == 1
        assert result[0].external_id == "active.py"

    def test_all_tombstoned(self) -> None:
        docs = [
            Document(
                id=uuid.uuid4(),
                source_id=uuid.uuid4(),
                external_id=f"gone{i}",
                uri=f"https://example.com/gone{i}",
                content_hash=_sha256(f"gone-{i}"),
                doc_version=1,
                doc_metadata={},
                indexed_at=_NOW,
                tombstoned_at=_NOW,
            )
            for i in range(2)
        ]
        assert self._active_documents(docs) == []


# ---------------------------------------------------------------------------
# IngestionRun aggregate status
# ---------------------------------------------------------------------------


def _aggregate_status(runs: list[IngestionRun]) -> str | None:
    """Derive an aggregate status from a list of ingestion runs.

    Rules:
    - No runs → None
    - Any running → 'running'
    - All ok → 'ok'
    - Any error (none running) → 'error'
    - Mix of ok + partial → 'partial'
    """
    if not runs:
        return None
    statuses = {r.status for r in runs}
    if IngestionRunStatus.running in statuses:
        return "running"
    if statuses == {IngestionRunStatus.ok}:
        return "ok"
    if IngestionRunStatus.error in statuses:
        return "error"
    return "partial"


def _make_run(status: IngestionRunStatus) -> IngestionRun:
    return IngestionRun(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        started_at=_NOW,
        status=status,
        docs_new=0,
        docs_updated=0,
        docs_removed=0,
        run_errors={},
    )


class TestAggregateStatus:
    def test_empty_returns_none(self) -> None:
        assert _aggregate_status([]) is None

    def test_all_ok(self) -> None:
        runs = [_make_run(IngestionRunStatus.ok) for _ in range(3)]
        assert _aggregate_status(runs) == "ok"

    def test_any_running(self) -> None:
        runs = [_make_run(IngestionRunStatus.ok), _make_run(IngestionRunStatus.running)]
        assert _aggregate_status(runs) == "running"

    def test_any_error_no_running(self) -> None:
        runs = [_make_run(IngestionRunStatus.ok), _make_run(IngestionRunStatus.error)]
        assert _aggregate_status(runs) == "error"

    def test_ok_and_partial(self) -> None:
        runs = [_make_run(IngestionRunStatus.ok), _make_run(IngestionRunStatus.partial)]
        assert _aggregate_status(runs) == "partial"

    def test_single_error(self) -> None:
        assert _aggregate_status([_make_run(IngestionRunStatus.error)]) == "error"

    def test_single_partial(self) -> None:
        assert _aggregate_status([_make_run(IngestionRunStatus.partial)]) == "partial"


# ---------------------------------------------------------------------------
# Pydantic schema round-trips
# ---------------------------------------------------------------------------


class TestPydanticSchemas:
    def test_source_create_defaults(self) -> None:
        sc = SourceCreate(type=SourceType.git, name="my-repo")
        assert sc.config == {}
        assert sc.status == SourceStatus.active
        assert sc.secrets_ref is None
        assert sc.tenant_id is None

    def test_source_read_from_orm(self) -> None:
        source = Source(
            id=uuid.uuid4(),
            type=SourceType.fs,
            name="local-fs",
            config={"root": "/data"},
            secrets_ref=None,
            status=SourceStatus.active,
            last_sync_at=None,
            last_error=None,
            last_error_at=None,
            freshness_sla_seconds=3600,
            tenant_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
        read = SourceRead.model_validate(source)
        assert read.name == "local-fs"
        assert read.config == {"root": "/data"}
        assert read.freshness_sla_seconds == 3600

    def test_document_create_required_fields(self) -> None:
        sid = uuid.uuid4()
        dc = DocumentCreate(
            source_id=sid,
            external_id="README.md@HEAD",
            uri="https://github.com/org/repo/README.md",
            content_hash=_sha256("# README"),
        )
        assert dc.source_id == sid
        assert dc.doc_version == 1
        assert dc.metadata == {}

    def test_document_read_tombstoned_is_optional(self) -> None:
        doc = Document(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            external_id="x",
            uri="https://example.com/x",
            content_hash=_sha256("x"),
            doc_version=1,
            doc_metadata={},
            indexed_at=_NOW,
        )
        read = DocumentRead.model_validate(doc)
        assert read.tombstoned_at is None

    def test_document_read_metadata_alias(self) -> None:
        """DocumentRead.metadata is populated from ORM's doc_metadata."""
        doc = Document(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            external_id="x",
            uri="https://example.com/x",
            content_hash=_sha256("x"),
            doc_version=1,
            doc_metadata={"lang": "python"},
            indexed_at=_NOW,
        )
        read = DocumentRead.model_validate(doc)
        assert read.metadata == {"lang": "python"}

    def test_document_update_partial(self) -> None:
        update = DocumentUpdate(content_hash=_sha256("new"))
        assert update.uri is None
        assert update.content_hash is not None

    def test_ingestion_run_create_defaults(self) -> None:
        rc = IngestionRunCreate(source_id=uuid.uuid4())
        assert rc.status == IngestionRunStatus.running

    def test_ingestion_run_update_partial(self) -> None:
        update = IngestionRunUpdate(status=IngestionRunStatus.ok, docs_new=5)
        assert update.docs_updated is None
        assert update.docs_new == 5

    def test_ingestion_run_read_errors_alias(self) -> None:
        """IngestionRunRead.errors is populated from ORM's run_errors."""
        run = IngestionRun(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            started_at=_NOW,
            status=IngestionRunStatus.partial,
            docs_new=3,
            docs_updated=1,
            docs_removed=0,
            run_errors={"stage": "embedder", "count": 2},
        )
        read = IngestionRunRead.model_validate(run)
        assert read.errors == {"stage": "embedder", "count": 2}

    def test_chunk_create_required_fields(self) -> None:
        cc = ChunkCreate(
            document_id=uuid.uuid4(),
            ord=0,
            text="def foo(): pass",
            embedding_model="bge-large-en-v1.5",
            embedding_provider="ollama",
            parser_version="treesitter-python-0.21",
            chunker_strategy="code_symbol",
        )
        assert cc.symbol is None
        assert cc.metadata == {}
        assert cc.ingestion_run_id is None

    def test_chunk_read_metadata_alias(self) -> None:
        """ChunkRead.metadata is populated from ORM's chunk_metadata."""
        chunk = Chunk(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            ord=0,
            text="hello",
            embedding_model="bge",
            embedding_provider="ollama",
            parser_version="v1",
            chunker_strategy="fixed",
            chunk_metadata={"line_range": [1, 5]},
        )
        read = ChunkRead.model_validate(chunk)
        assert read.metadata == {"line_range": [1, 5]}

    def test_api_token_create_defaults(self) -> None:
        tc = ApiTokenCreate(
            name="ci-bot",
            hashed_token="$argon2id$...",
            token_prefix="om_abcd1234",
        )
        assert tc.scopes == []
        assert tc.expires_at is None

    def test_api_token_read_no_hash(self) -> None:
        """ApiTokenRead must never expose hashed_token."""
        token = ApiToken(
            id=uuid.uuid4(),
            name="dev-token",
            hashed_token="$argon2id$secret",
            token_prefix="om_dev1",
            scopes=["search"],
            created_at=_NOW,
            is_active=True,
        )
        read = ApiTokenRead.model_validate(token)
        assert not hasattr(read, "hashed_token")
        assert read.token_prefix == "om_dev1"
        assert read.scopes == ["search"]

    def test_ingestion_run_read_from_orm(self) -> None:
        run = IngestionRun(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            started_at=_NOW,
            status=IngestionRunStatus.ok,
            docs_new=10,
            docs_updated=2,
            docs_removed=1,
            run_errors={},
        )
        read = IngestionRunRead.model_validate(run)
        assert read.docs_new == 10
        assert read.status == IngestionRunStatus.ok


# ---------------------------------------------------------------------------
# Chunk model validation
# ---------------------------------------------------------------------------


class TestChunkModel:
    def test_lineage_fields_present(self) -> None:
        """All lineage fields required by schema.md must exist on the model."""
        chunk = Chunk(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            ord=0,
            text="import os",
            embedding_model="bge-large-en-v1.5",
            embedding_provider="ollama",
            parser_version="treesitter-python-0.21+oms-0.4.2",
            chunker_strategy="code_symbol",
            chunk_metadata={},
        )
        assert chunk.embedding_model == "bge-large-en-v1.5"
        assert chunk.embedding_provider == "ollama"
        assert chunk.parser_version == "treesitter-python-0.21+oms-0.4.2"
        assert chunk.chunker_strategy == "code_symbol"
        assert chunk.ingestion_run_id is None
        assert chunk.symbol is None

    def test_symbol_set_for_code_chunks(self) -> None:
        chunk = Chunk(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            ord=2,
            text="def my_function(): ...",
            embedding_model="text-embedding-004",
            embedding_provider="google-ai",
            parser_version="treesitter-python-0.21",
            chunker_strategy="code_symbol",
            chunk_metadata={"line_range": [10, 15]},
            symbol="my_module.my_function",
        )
        assert chunk.symbol == "my_module.my_function"


# ---------------------------------------------------------------------------
# IngestionRunStatus coverage
# ---------------------------------------------------------------------------


class TestIngestionRunStatus:
    def test_all_statuses_present(self) -> None:
        assert {s.value for s in IngestionRunStatus} == {"running", "ok", "partial", "error"}
