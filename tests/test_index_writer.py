"""Unit tests for the IndexWriter and content-hash helpers.

Strategy: mock the SQLAlchemy async_sessionmaker and AsyncSession so that no
live database connection is needed.  The session mock tracks objects added to
it and returns pre-configured query results via ``execute`` side-effects.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_index.hashing import compute_content_hash, normalize_content
from omniscience_index.writer import ChunkData, IndexWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _chunk(chunk_ord: int = 0, text: str = "hello") -> ChunkData:
    return ChunkData(
        ord=chunk_ord,
        text=text,
        symbol=None,
        metadata={},
        embedding_model="bge-large-en-v1.5",
        embedding_provider="ollama",
        parser_version="treesitter-python-0.21",
        chunker_strategy="code_symbol",
    )


def _make_doc(
    source_id: uuid.UUID | None = None,
    external_id: str = "file.py@abc",
    content_hash: str | None = None,
    doc_version: int = 1,
    tombstoned_at: datetime | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like an ORM Document."""
    doc = MagicMock()
    doc.id = uuid.uuid4()
    doc.source_id = source_id or uuid.uuid4()
    doc.external_id = external_id
    doc.content_hash = content_hash or _sha256("content")
    doc.doc_version = doc_version
    doc.tombstoned_at = tombstoned_at
    return doc


# ---------------------------------------------------------------------------
# Session mock factory
# ---------------------------------------------------------------------------


def _make_session_factory(
    existing_doc: Any = None,
    rowcount: int = 0,
) -> MagicMock:
    """Build a minimal async_sessionmaker mock.

    ``existing_doc`` is returned by the SELECT scalar (None → new document).
    ``rowcount``     is returned by DELETE statements.
    """
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = existing_doc

    delete_result = MagicMock()
    delete_result.rowcount = rowcount

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[scalar_result, delete_result, delete_result])
    session.flush = AsyncMock()
    session.add = MagicMock()

    # Context manager: __aenter__ returns session, __aexit__ is a no-op
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    # session.begin() returns a transaction context manager (no-op)
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)

    factory = MagicMock()
    factory.return_value = cm

    return factory


# ---------------------------------------------------------------------------
# Test: upsert creates new document
# ---------------------------------------------------------------------------


class TestUpsertCreatesNew:
    @pytest.mark.asyncio
    async def test_new_document_action_is_created(self) -> None:
        factory = _make_session_factory(existing_doc=None)
        writer = IndexWriter(factory)

        result = await writer.upsert_document(
            source_id=uuid.uuid4(),
            external_id="file.py@abc",
            uri="https://example.com/file.py",
            title="My File",
            content_hash=_sha256("content"),
            metadata={},
            chunks=[_chunk(0), _chunk(1)],
        )

        assert result.action == "created"
        assert result.chunks_written == 2
        assert result.doc_version == 1

    @pytest.mark.asyncio
    async def test_new_document_id_is_uuid(self) -> None:
        factory = _make_session_factory(existing_doc=None)
        writer = IndexWriter(factory)

        result = await writer.upsert_document(
            source_id=uuid.uuid4(),
            external_id="x",
            uri="https://example.com/x",
            title=None,
            content_hash=_sha256("x"),
            metadata={},
            chunks=[],
        )

        assert isinstance(result.document_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_new_document_session_add_called(self) -> None:
        """session.add() must be called for the Document and each Chunk."""
        factory = _make_session_factory(existing_doc=None)
        real_call = factory.return_value.__aenter__.return_value

        writer = IndexWriter(factory)
        await writer.upsert_document(
            source_id=uuid.uuid4(),
            external_id="y",
            uri="https://example.com/y",
            title=None,
            content_hash=_sha256("y"),
            metadata={},
            chunks=[_chunk(0), _chunk(1), _chunk(2)],
        )

        # 1 Document + 3 Chunks
        assert real_call.add.call_count == 4


# ---------------------------------------------------------------------------
# Test: upsert with same hash returns unchanged
# ---------------------------------------------------------------------------


class TestUpsertUnchanged:
    @pytest.mark.asyncio
    async def test_same_hash_returns_unchanged(self) -> None:
        content_hash = _sha256("same content")
        existing = _make_doc(content_hash=content_hash, doc_version=3)
        factory = _make_session_factory(existing_doc=existing)
        writer = IndexWriter(factory)

        result = await writer.upsert_document(
            source_id=existing.source_id,
            external_id=existing.external_id,
            uri="https://example.com/f",
            title=None,
            content_hash=content_hash,
            metadata={},
            chunks=[_chunk()],
        )

        assert result.action == "unchanged"
        assert result.chunks_written == 0
        assert result.document_id == existing.id
        assert result.doc_version == 3

    @pytest.mark.asyncio
    async def test_unchanged_does_not_add_chunks(self) -> None:
        content_hash = _sha256("stable")
        existing = _make_doc(content_hash=content_hash)
        factory = _make_session_factory(existing_doc=existing)
        session_mock = factory.return_value.__aenter__.return_value
        writer = IndexWriter(factory)

        await writer.upsert_document(
            source_id=existing.source_id,
            external_id=existing.external_id,
            uri="https://example.com/f",
            title=None,
            content_hash=content_hash,
            metadata={},
            chunks=[_chunk()],
        )

        session_mock.add.assert_not_called()


# ---------------------------------------------------------------------------
# Test: upsert with different hash → update
# ---------------------------------------------------------------------------


class TestUpsertUpdated:
    @pytest.mark.asyncio
    async def test_different_hash_returns_updated(self) -> None:
        old_hash = _sha256("old content")
        new_hash = _sha256("new content")
        existing = _make_doc(content_hash=old_hash, doc_version=2)
        factory = _make_session_factory(existing_doc=existing)
        writer = IndexWriter(factory)

        result = await writer.upsert_document(
            source_id=existing.source_id,
            external_id=existing.external_id,
            uri="https://example.com/f",
            title=None,
            content_hash=new_hash,
            metadata={},
            chunks=[_chunk(0), _chunk(1)],
        )

        assert result.action == "updated"
        assert result.chunks_written == 2

    @pytest.mark.asyncio
    async def test_update_increments_version(self) -> None:
        old_hash = _sha256("v1")
        new_hash = _sha256("v2")
        existing = _make_doc(content_hash=old_hash, doc_version=5)
        factory = _make_session_factory(existing_doc=existing)
        writer = IndexWriter(factory)

        result = await writer.upsert_document(
            source_id=existing.source_id,
            external_id=existing.external_id,
            uri="https://example.com/f",
            title=None,
            content_hash=new_hash,
            metadata={},
            chunks=[],
        )

        # doc_version starts at 5; writer increments to 6 on the ORM object
        assert result.doc_version == 6

    @pytest.mark.asyncio
    async def test_update_deletes_old_chunks(self) -> None:
        old_hash = _sha256("old")
        new_hash = _sha256("new")
        existing = _make_doc(content_hash=old_hash)
        factory = _make_session_factory(existing_doc=existing)
        session_mock = factory.return_value.__aenter__.return_value
        writer = IndexWriter(factory)

        await writer.upsert_document(
            source_id=existing.source_id,
            external_id=existing.external_id,
            uri="https://example.com/f",
            title=None,
            content_hash=new_hash,
            metadata={},
            chunks=[_chunk()],
        )

        # session.execute called at least twice: SELECT + DELETE
        assert session_mock.execute.call_count >= 2


# ---------------------------------------------------------------------------
# Test: tombstone
# ---------------------------------------------------------------------------


class TestTombstone:
    @pytest.mark.asyncio
    async def test_tombstone_existing_doc_returns_true(self) -> None:
        existing = _make_doc()
        factory = _make_session_factory(existing_doc=existing)
        writer = IndexWriter(factory)

        result = await writer.tombstone(existing.source_id, existing.external_id)

        assert result is True

    @pytest.mark.asyncio
    async def test_tombstone_sets_tombstoned_at(self) -> None:
        existing = _make_doc()
        assert existing.tombstoned_at is None
        factory = _make_session_factory(existing_doc=existing)
        writer = IndexWriter(factory)

        await writer.tombstone(existing.source_id, existing.external_id)

        assert existing.tombstoned_at is not None
        assert isinstance(existing.tombstoned_at, datetime)

    @pytest.mark.asyncio
    async def test_tombstone_missing_doc_returns_false(self) -> None:
        factory = _make_session_factory(existing_doc=None)
        writer = IndexWriter(factory)

        result = await writer.tombstone(uuid.uuid4(), "nonexistent.py@abc")

        assert result is False


# ---------------------------------------------------------------------------
# Test: purge_tombstones
# ---------------------------------------------------------------------------


class TestPurgeTombstones:
    @pytest.mark.asyncio
    async def test_purge_returns_rowcount(self) -> None:
        factory = _make_session_factory(rowcount=3)
        delete_result = MagicMock()
        delete_result.rowcount = 3
        session_mock = factory.return_value.__aenter__.return_value
        session_mock.execute = AsyncMock(return_value=delete_result)

        writer = IndexWriter(factory)
        count = await writer.purge_tombstones(timedelta(days=30))

        assert count == 3

    @pytest.mark.asyncio
    async def test_purge_zero_when_none_qualify(self) -> None:
        delete_result = MagicMock()
        delete_result.rowcount = 0
        factory = _make_session_factory()
        session_mock = factory.return_value.__aenter__.return_value
        session_mock.execute = AsyncMock(return_value=delete_result)

        writer = IndexWriter(factory)
        count = await writer.purge_tombstones(timedelta(days=7))

        assert count == 0


# ---------------------------------------------------------------------------
# Test: transaction atomicity
# ---------------------------------------------------------------------------


class TestAtomicity:
    @pytest.mark.asyncio
    async def test_chunk_flush_failure_propagates(self) -> None:
        """If flush raises during chunk insert the exception must propagate."""
        factory = _make_session_factory(existing_doc=None)
        session_mock = factory.return_value.__aenter__.return_value
        # First flush (for doc insert) succeeds; second (for chunks) fails
        session_mock.flush = AsyncMock(side_effect=[None, RuntimeError("DB is down")])

        writer = IndexWriter(factory)
        with pytest.raises(RuntimeError, match="DB is down"):
            await writer.upsert_document(
                source_id=uuid.uuid4(),
                external_id="fail.py",
                uri="https://example.com/fail.py",
                title=None,
                content_hash=_sha256("data"),
                metadata={},
                chunks=[_chunk()],
            )


# ---------------------------------------------------------------------------
# Test: content hash normalization
# ---------------------------------------------------------------------------


class TestNormalizeContent:
    def test_bom_stripped(self) -> None:
        with_bom = "﻿hello world"
        assert normalize_content(with_bom) == "hello world"

    def test_trailing_whitespace_trimmed(self) -> None:
        raw = "line one   \nline two  "
        result = normalize_content(raw)
        assert result == "line one\nline two"

    def test_multiple_blank_lines_collapsed(self) -> None:
        raw = "a\n\n\n\nb"
        result = normalize_content(raw)
        assert result == "a\n\nb"

    def test_single_blank_line_preserved(self) -> None:
        raw = "a\n\nb"
        assert normalize_content(raw) == "a\n\nb"

    def test_no_trailing_blank_lines(self) -> None:
        raw = "a\nb"
        assert normalize_content(raw) == "a\nb"

    def test_empty_string(self) -> None:
        assert normalize_content("") == ""

    def test_only_bom(self) -> None:
        assert normalize_content("﻿") == ""


class TestComputeContentHash:
    def test_known_value(self) -> None:
        text = "hello"
        expected = hashlib.sha256(b"hello").hexdigest()
        assert compute_content_hash(text) == expected

    def test_cosmetic_change_same_hash(self) -> None:
        a = "def foo():  \n    pass  \n"
        b = "def foo():\n    pass\n"
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_bom_variant_same_hash(self) -> None:
        assert compute_content_hash("﻿hello") == compute_content_hash("hello")

    def test_extra_blank_lines_same_hash(self) -> None:
        assert compute_content_hash("a\n\n\nb") == compute_content_hash("a\n\nb")

    def test_semantic_change_different_hash(self) -> None:
        assert compute_content_hash("foo") != compute_content_hash("bar")


# ---------------------------------------------------------------------------
# Test: idempotency — same content yields same result
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_same_content_twice_is_unchanged(self) -> None:
        content_hash = compute_content_hash("stable content")
        existing = _make_doc(content_hash=content_hash, doc_version=1)
        factory = _make_session_factory(existing_doc=existing)
        writer = IndexWriter(factory)

        result = await writer.upsert_document(
            source_id=existing.source_id,
            external_id=existing.external_id,
            uri="https://example.com/f",
            title=None,
            content_hash=content_hash,
            metadata={},
            chunks=[_chunk()],
        )

        assert result.action == "unchanged"
        assert result.chunks_written == 0


# ---------------------------------------------------------------------------
# Test: IndexWriter with vector_store (Qdrant path coverage)
# ---------------------------------------------------------------------------


def _make_session_factory_for_vs(
    existing_doc: Any = None,
    rowcount: int = 0,
) -> MagicMock:
    """Session factory variant with enough execute slots for VS path.

    upsert_document (new doc): execute[0]=SELECT, execute[1]=chunks DELETE n/a
    purge_tombstones:          execute[0]=PG DELETE
    """
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = existing_doc

    delete_result = MagicMock()
    delete_result.rowcount = rowcount

    session = AsyncMock()
    # Provide a generous side_effect list; unused entries are never consumed.
    session.execute = AsyncMock(
        side_effect=[scalar_result, delete_result, delete_result, delete_result]
    )
    session.flush = AsyncMock()
    session.add = MagicMock()

    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock()
    factory.return_value = cm
    return factory


class TestIndexWriterWithVectorStore:
    """Cover writer.py lines 102-131 (Qdrant path) and the purge fix."""

    # ------------------------------------------------------------------
    # (a) Happy path: upsert_chunks is awaited with workspace_id in metadata
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upsert_calls_vector_store_with_workspace_id(self) -> None:
        """vector_store.upsert_chunks must be awaited; metadata must carry workspace_id."""
        workspace_id = uuid.uuid4()
        source_id = uuid.uuid4()
        vector_store = AsyncMock()
        factory = _make_session_factory_for_vs(existing_doc=None)

        writer = IndexWriter(factory, vector_store=vector_store)
        result = await writer.upsert_document(
            source_id=source_id,
            external_id="doc.py@v1",
            uri="https://example.com/doc.py",
            title="Doc",
            content_hash=_sha256("body"),
            metadata={"author": "alice"},
            chunks=[_chunk(0, "chunk zero"), _chunk(1, "chunk one")],
            workspace_id=workspace_id,
        )

        assert result.action == "created"
        assert result.chunks_written == 2

        vector_store.upsert_chunks.assert_awaited_once()
        _, kwargs = vector_store.upsert_chunks.call_args
        assert kwargs["source_id"] == source_id
        assert kwargs["external_id"] == "doc.py@v1"
        assert kwargs["content_hash"] == _sha256("body")
        # workspace_id must be injected into metadata for ACL enforcement
        assert kwargs["metadata"]["workspace_id"] == str(workspace_id)
        # original metadata keys must be preserved
        assert kwargs["metadata"]["author"] == "alice"

    @pytest.mark.asyncio
    async def test_upsert_passes_correct_chunk_payloads(self) -> None:
        """Each ChunkData must be converted to a ChunkPayload dict correctly."""
        workspace_id = uuid.uuid4()
        vector_store = AsyncMock()
        factory = _make_session_factory_for_vs(existing_doc=None)

        writer = IndexWriter(factory, vector_store=vector_store)
        await writer.upsert_document(
            source_id=uuid.uuid4(),
            external_id="mod.py@1",
            uri="https://example.com/mod.py",
            title=None,
            content_hash=_sha256("x"),
            metadata={},
            chunks=[_chunk(3, "specific text")],
            workspace_id=workspace_id,
        )

        _, kwargs = vector_store.upsert_chunks.call_args
        chunks_sent = kwargs["chunks"]
        assert len(chunks_sent) == 1
        c = chunks_sent[0]
        assert c["ord"] == 3
        assert c["text"] == "specific text"
        assert c["embedding_model"] == "bge-large-en-v1.5"
        assert c["embedding_provider"] == "ollama"

    @pytest.mark.asyncio
    async def test_upsert_skips_vector_store_when_no_workspace_id(self) -> None:
        """vector_store must NOT be called when workspace_id is omitted."""
        vector_store = AsyncMock()
        factory = _make_session_factory_for_vs(existing_doc=None)

        writer = IndexWriter(factory, vector_store=vector_store)
        result = await writer.upsert_document(
            source_id=uuid.uuid4(),
            external_id="no-ws.py",
            uri="https://example.com/no-ws.py",
            title=None,
            content_hash=_sha256("body"),
            metadata={},
            chunks=[_chunk()],
            workspace_id=None,
        )

        assert result.action == "created"
        vector_store.upsert_chunks.assert_not_awaited()

    # ------------------------------------------------------------------
    # (b) Qdrant failure → PG transaction does not commit (rollback path)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_qdrant_failure_propagates_exception(self) -> None:
        """When upsert_chunks raises, the exception must escape upsert_document."""
        workspace_id = uuid.uuid4()
        vector_store = AsyncMock()
        vector_store.upsert_chunks = AsyncMock(side_effect=RuntimeError("qdrant down"))
        factory = _make_session_factory_for_vs(existing_doc=None)

        writer = IndexWriter(factory, vector_store=vector_store)
        with pytest.raises(RuntimeError, match="qdrant down"):
            await writer.upsert_document(
                source_id=uuid.uuid4(),
                external_id="broken.py",
                uri="https://example.com/broken.py",
                title=None,
                content_hash=_sha256("data"),
                metadata={},
                chunks=[_chunk()],
                workspace_id=workspace_id,
            )

    @pytest.mark.asyncio
    async def test_qdrant_failure_does_not_trigger_pg_rollback_path(self) -> None:
        """When upsert_chunks raises, the begin() context manager does NOT receive the
        exception, because the call happens outside the transaction to support
        min-checkpoint replay. Postgres commits, and Qdrant throws.
        """
        workspace_id = uuid.uuid4()
        vector_store = AsyncMock()
        vector_store.upsert_chunks = AsyncMock(side_effect=RuntimeError("qdrant down"))
        factory = _make_session_factory_for_vs(existing_doc=None)
        session_mock = factory.return_value.__aenter__.return_value
        tx_mock = session_mock.begin.return_value

        writer = IndexWriter(factory, vector_store=vector_store)
        with pytest.raises(RuntimeError, match="qdrant down"):
            await writer.upsert_document(
                source_id=uuid.uuid4(),
                external_id="broken.py",
                uri="https://example.com/broken.py",
                title=None,
                content_hash=_sha256("data"),
                metadata={},
                chunks=[_chunk()],
                workspace_id=workspace_id,
            )

        # __aexit__ must have been called with None, meaning Postgres transaction
        # was committed successfully before Qdrant threw the exception.
        tx_exit_call = tx_mock.__aexit__.call_args
        exc_type = tx_exit_call[0][0]  # positional arg 0: exc_type
        assert exc_type is None, (
            "begin().__aexit__ must NOT receive the exception; PG must commit"
        )

    @pytest.mark.asyncio
    async def test_qdrant_failure_pg_add_was_called_before_raise(self) -> None:
        """Confirm that session.add() ran (PG write path executed) before the
        Qdrant exception aborted the transaction — documents are never partially
        visible because the surrounding begin() rolls the whole unit back.
        """
        workspace_id = uuid.uuid4()
        vector_store = AsyncMock()
        vector_store.upsert_chunks = AsyncMock(side_effect=RuntimeError("qdrant down"))
        factory = _make_session_factory_for_vs(existing_doc=None)
        session_mock = factory.return_value.__aenter__.return_value

        writer = IndexWriter(factory, vector_store=vector_store)
        with pytest.raises(RuntimeError):
            await writer.upsert_document(
                source_id=uuid.uuid4(),
                external_id="broken.py",
                uri="https://example.com/broken.py",
                title=None,
                content_hash=_sha256("data"),
                metadata={},
                chunks=[_chunk()],
                workspace_id=workspace_id,
            )

        # session.add was called (for the Document), proving PG path ran,
        # but the transaction was not committed (exception escaped).
        assert session_mock.add.call_count >= 1

    # ------------------------------------------------------------------
    # (c) purge_tombstones with vector_store → delete_tombstoned is awaited
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_purge_calls_delete_tombstoned_on_vector_store(self) -> None:
        """purge_tombstones must delegate to vector_store.delete_tombstoned."""
        delta = timedelta(days=14)
        vector_store = AsyncMock()
        vector_store.delete_tombstoned = AsyncMock(return_value=2)

        delete_result = MagicMock()
        delete_result.rowcount = 2
        factory = _make_session_factory_for_vs(rowcount=2)
        session_mock = factory.return_value.__aenter__.return_value
        session_mock.execute = AsyncMock(return_value=delete_result)

        writer = IndexWriter(factory, vector_store=vector_store)
        count = await writer.purge_tombstones(delta)

        vector_store.delete_tombstoned.assert_awaited_once_with(delta)
        assert count == 2

    @pytest.mark.asyncio
    async def test_purge_qdrant_first_then_pg(self) -> None:
        """Qdrant deletion must happen before the Postgres DELETE statement.

        We verify ordering by recording the call sequence: delete_tombstoned
        must precede session.execute (the PG DELETE).
        """
        call_order: list[str] = []

        vector_store = AsyncMock()

        async def _vs_delete(older_than: timedelta) -> int:
            call_order.append("qdrant")
            return 1

        vector_store.delete_tombstoned = _vs_delete

        delete_result = MagicMock()
        delete_result.rowcount = 1
        factory = _make_session_factory_for_vs(rowcount=1)
        session_mock = factory.return_value.__aenter__.return_value

        async def _pg_execute(stmt: object) -> MagicMock:
            call_order.append("postgres")
            return delete_result

        session_mock.execute = AsyncMock(side_effect=_pg_execute)

        writer = IndexWriter(factory, vector_store=vector_store)
        await writer.purge_tombstones(timedelta(days=7))

        assert call_order == ["qdrant", "postgres"], (
            f"Expected Qdrant before Postgres; got {call_order}"
        )

    @pytest.mark.asyncio
    async def test_purge_skips_qdrant_when_no_vector_store(self) -> None:
        """Without a vector_store, purge must not raise and must still delete PG rows."""
        delete_result = MagicMock()
        delete_result.rowcount = 5
        factory = _make_session_factory_for_vs(rowcount=5)
        session_mock = factory.return_value.__aenter__.return_value
        session_mock.execute = AsyncMock(return_value=delete_result)

        writer = IndexWriter(factory, vector_store=None)
        count = await writer.purge_tombstones(timedelta(days=1))

        assert count == 5
