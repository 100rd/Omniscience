"""Regression gates for the per-document graph-write checkpoint.

Background — the defect these tests exist to prevent
-----------------------------------------------------

``Neo4jGraphStore.upsert_graph`` is called **once per document**.  The
``version`` it receives is ``documents.doc_version`` — a *per-row* counter that
starts at 1 and is bumped only when that one document changes.

The original guard compared that per-document counter against a **single
per-source** ``:StoreCheckpoint`` scalar::

    if existing_version is not None and existing_version >= version:
        return 0, 0        # <- returns before the entity write loop

The first document of a source set the source checkpoint to 1; every later
document of the same source arrived with ``doc_version == 1``, failed
``1 >= 1``, and was silently dropped.  Measured on a live stack: 978 documents
produced 15 entities.

The guard itself is not the bug — it is genuine stale-write protection for
NATS JetStream redelivery.  The bug is comparing a per-source scalar against a
per-document counter.  The fix keeps monotonic protection but compares
like with like:

* ``:DocumentCheckpoint {workspace_id, source_id, document_id}`` — the write
  guard.  Compares this document's ``doc_version`` against the version last
  applied *for this document*.
* ``:StoreCheckpoint {workspace_id, source_id}`` — a monotonic per-source
  high-water mark.  Never gates a write; it feeds ``GlobalReconciler`` and
  ``scripts/dr_verify.py`` (documented invariant: Neo4j checkpoint == Postgres
  ``max(doc_version)`` per source).

These are fast unit tests driven by a stateful in-memory fake driver.  The
live-database counterpart is
``tests/integration/test_neo4j_multi_document_source.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from omniscience_index.stores.neo4j import store as store_module
from omniscience_index.stores.neo4j._cypher import (
    _BOOTSTRAP_STATEMENTS,
    _CHECKPOINT_HEAL_STATEMENTS,
)
from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

pytestmark = pytest.mark.asyncio


_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


# ---------------------------------------------------------------------------
# In-memory fake driver
# ---------------------------------------------------------------------------


class _FakeRecord(dict[str, Any]):
    """A row that answers ``.data()`` like ``neo4j.Record`` does."""

    def data(self) -> dict[str, Any]:
        return dict(self)


class _FakeCounters:
    """Stand-in for ``neo4j.SummaryCounters``.

    Only the three counters the store folds are modelled.  Zero across the
    board is a statement that matched but changed nothing — the bitemporal
    no-op replay — and the store must report that as *not written*.
    """

    def __init__(
        self,
        *,
        nodes_created: int = 0,
        relationships_created: int = 0,
        properties_set: int = 0,
    ) -> None:
        self.nodes_created = nodes_created
        self.relationships_created = relationships_created
        self.properties_set = properties_set


class _FakeSummary:
    def __init__(self, counters: _FakeCounters) -> None:
        self.counters = counters


class _FakeResult:
    """Minimal stand-in for ``neo4j.AsyncResult``."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        counters: _FakeCounters | None = None,
    ) -> None:
        self._records = [_FakeRecord(r) for r in records]
        self._counters = counters if counters is not None else _FakeCounters()

    async def single(self) -> dict[str, Any] | None:
        return self._records[0] if self._records else None

    async def consume(self) -> _FakeSummary:
        return _FakeSummary(self._counters)

    def __aiter__(self) -> Any:
        records = iter(self._records)

        class _Iter:
            async def __anext__(self) -> dict[str, Any]:
                try:
                    return next(records)
                except StopIteration:
                    raise StopAsyncIteration from None

        return _Iter()


class _FakeTx:
    """Interprets just enough Cypher to model checkpoint + entity/edge writes.

    Deliberately dispatches on *structural* markers (label names, parameter
    names) that are present in BOTH the pre-fix and post-fix Cypher, so the
    same fake can execute either implementation.  That is what makes the
    "N documents produce N entities" test a genuine before/after gate rather
    than a test written against the new code's shape.
    """

    def __init__(self) -> None:
        self.source_checkpoints: dict[tuple[str, str], dict[str, Any]] = {}
        self.document_checkpoints: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.entity_writes: list[str] = []
        self.edge_writes: list[str] = []
        self.deleted_sources: list[str] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []
        # Simulates the un-constrained duplicate ``:StoreCheckpoint`` nodes that
        # the missing uniqueness constraint allowed concurrent writers to create.
        self.duplicate_source_rows: list[dict[str, Any]] = []
        # How many duplicate ``:StoreCheckpoint`` groups the heal still has to
        # collapse.  The heal is batched, so a single pass no longer converges;
        # this models the remaining work so the convergence loop is observable.
        self.duplicate_checkpoint_groups: int = 0
        self.heal_batches: list[int] = []
        # Failure injection for the two startup steps that must NOT abort
        # ``connect()``: the data heal and the checkpoint uniqueness DDL.
        self.fail_on_heal: bool = False
        self.fail_on_checkpoint_constraint: bool = False
        # Whether an entity/edge statement reports that it changed anything.
        # False models the bitemporal no-op replay: the statement runs, matches,
        # and writes nothing — which must be reported as 0 written.
        self.persist_writes: bool = True

    def _write_counters(self) -> _FakeCounters:
        if not self.persist_writes:
            return _FakeCounters()
        return _FakeCounters(nodes_created=1, properties_set=3)

    async def run(self, query: str, params: dict[str, Any] | None = None) -> _FakeResult:
        params = dict(params or {})
        self.queries.append((query, params))

        # The batched checkpoint heal: no workspace_id, returns how many
        # duplicate groups this batch collapsed.
        if "collapsed" in query and "StoreCheckpoint" in query:
            return self._run_checkpoint_heal(params)
        if self.fail_on_checkpoint_constraint and "CONSTRAINT" in query and "Checkpoint" in query:
            raise RuntimeError("simulated_constraint_violation")
        # Bootstrap DDL mentions the labels but carries no parameters — it is
        # schema maintenance, not checkpoint traffic.
        if "workspace_id" in params:
            if "DocumentCheckpoint" in query:
                return self._run_document_checkpoint(query, params)
            if "StoreCheckpoint" in query:
                return self._run_source_checkpoint(query, params)
        if "DETACH DELETE n" in query and "source_id" in params:
            self.deleted_sources.append(str(params["source_id"]))
            return _FakeResult([])
        if "batch_entity_ids" in params:
            return _FakeResult([{"entities_end_dated": 0, "edges_end_dated": 0}])
        if "MERGE (n:Entity" in query and "id" in params:
            self.entity_writes.append(str(params["id"]))
            return _FakeResult([], counters=self._write_counters())
        if "source_id_ent" in params:
            self.edge_writes.append(str(params["source_id_ent"]))
            return _FakeResult([], counters=self._write_counters())
        return _FakeResult([])

    def _run_checkpoint_heal(self, params: dict[str, Any]) -> _FakeResult:
        if self.fail_on_heal:
            raise RuntimeError("simulated_heal_failure")
        batch_size = int(params.get("batch_size", 0))
        self.heal_batches.append(batch_size)
        collapsed = min(batch_size, self.duplicate_checkpoint_groups)
        self.duplicate_checkpoint_groups -= collapsed
        return _FakeResult([{"collapsed": collapsed}])

    # -- checkpoint handlers -------------------------------------------------

    def _run_source_checkpoint(self, query: str, params: dict[str, Any]) -> _FakeResult:
        key = (str(params["workspace_id"]), str(params["source_id"]))
        if query.lstrip().startswith("MATCH"):
            row = self.source_checkpoints.get(key)
            rows = [row] if row is not None else []
            return _FakeResult([*rows, *self.duplicate_source_rows])
        self._apply_merge(self.source_checkpoints, key, query, params)
        return _FakeResult([])

    def _run_document_checkpoint(self, query: str, params: dict[str, Any]) -> _FakeResult:
        key = (
            str(params["workspace_id"]),
            str(params["source_id"]),
            str(params["document_id"]),
        )
        if query.lstrip().startswith("MATCH"):
            row = self.document_checkpoints.get(key)
            return _FakeResult([row] if row is not None else [])
        self._apply_merge(self.document_checkpoints, key, query, params)
        return _FakeResult([])

    @staticmethod
    def _apply_merge(
        store: dict[Any, dict[str, Any]],
        key: Any,
        query: str,
        params: dict[str, Any],
    ) -> None:
        """Apply a checkpoint MERGE.

        ``CASE`` in the statement marks the monotonic (max) form; its absence
        marks the pre-fix unconditional assignment.  Modelling both keeps the
        fake faithful to whichever implementation is under test.
        """
        existing = store.get(key)
        incoming_version = params.get("version")
        incoming_epoch = params.get("epoch")
        if existing is None or "CASE" not in query:
            store[key] = {"version": incoming_version, "epoch": incoming_epoch}
            return
        current_version = existing.get("version")
        current_epoch = existing.get("epoch")
        epoch_advanced = (
            incoming_epoch is not None
            and current_epoch is not None
            and incoming_epoch > current_epoch
        )
        if (
            epoch_advanced
            or current_version is None
            or (incoming_version is not None and incoming_version > current_version)
        ):
            new_version = incoming_version
        else:
            new_version = current_version
        if current_epoch is None:
            new_epoch = incoming_epoch
        elif incoming_epoch is None or incoming_epoch <= current_epoch:
            new_epoch = current_epoch
        else:
            new_epoch = incoming_epoch
        store[key] = {"version": new_version, "epoch": new_epoch}


class _FakeSession:
    def __init__(self, tx: _FakeTx) -> None:
        self._tx = tx

    async def execute_write(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await fn(self._tx, *args, **kwargs)

    async def execute_read(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await fn(self._tx, *args, **kwargs)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeDriver:
    def __init__(self, tx: _FakeTx) -> None:
        self.tx = tx

    def session(self, **_kwargs: Any) -> _FakeSession:
        return _FakeSession(self.tx)

    async def verify_connectivity(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _make_store(*, bitemporal: bool = True) -> tuple[Neo4jGraphStore, _FakeTx]:
    config = Neo4jStoreConfig(
        uri="bolt://fake",
        username="neo4j",
        password="fake",
        database="neo4j",
        max_connection_pool_size=1,
        connection_acquisition_timeout_seconds=1.0,
        max_transaction_retry_time_seconds=1.0,
        default_max_depth=3,
        bitemporal_enabled=bitemporal,
    )
    store = Neo4jGraphStore(config=config)
    tx = _FakeTx()
    store._driver = _FakeDriver(tx)  # type: ignore[assignment]
    return store, tx


class _Entity:
    """Duck-typed parser-style entity (matches ``_entity_to_params``)."""

    def __init__(self, name: str) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = _WORKSPACE
        self.entity_type = "resource"
        self.name = name
        self.display_name = name.rsplit(".", 1)[-1]
        self.chunk_id: uuid.UUID | None = None
        self.metadata: dict[str, Any] = {"workspace_id": str(_WORKSPACE)}


# ---------------------------------------------------------------------------
# 1. THE regression gate — N documents of one source must produce N entities
# ---------------------------------------------------------------------------


async def test_every_document_of_a_source_is_persisted() -> None:
    """Each document writes its own entities; the source checkpoint must not gate them.

    Pre-fix behaviour: document #1 sets the source checkpoint to 1, documents
    #2..#N arrive with ``doc_version == 1``, fail ``existing >= incoming`` and
    return before the entity write loop — 1 entity for N documents.
    """
    store, tx = _make_store()
    source_id = uuid.uuid4()

    for i in range(5):
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[_Entity(f"aws.resource.{i}")],
            edges=[],
            workspace_id=_WORKSPACE,
            version=1,
        )

    assert len(tx.entity_writes) == 5
    assert len(set(tx.entity_writes)) == 5


async def test_result_reports_persisted_entities_for_every_document() -> None:
    """The returned counts must describe persistence, not the input batch.

    The live-database counterpart is
    ``tests/integration/test_neo4j_multi_document_source.py::
    test_written_counts_report_persistence_not_submission`` — that one proves
    the distinction against real driver counters.  This one proves the store
    *reads* the counters at all, which is why the sensitivity case below
    matters: a loop counter would report 2 either way.
    """
    store, _tx = _make_store()
    source_id = uuid.uuid4()

    results = [
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[_Entity(f"aws.resource.{i}"), _Entity(f"aws.resource.{i}.child")],
            edges=[],
            workspace_id=_WORKSPACE,
            version=1,
        )
        for i in range(3)
    ]

    assert [r.applied for r in results] == [True, True, True]
    assert [r.entities_written for r in results] == [2, 2, 2]
    assert [r.entities_submitted for r in results] == [2, 2, 2]
    assert [r.skip_reason for r in results] == [None, None, None]


async def test_a_write_that_changed_nothing_is_not_counted_as_written() -> None:
    """The sensitivity case: submitted 2, persisted 0.

    This is the bitemporal no-op replay — the DEFAULT path, since
    ``_UPSERT_ENTITY_BITEMPORAL_CYPHER`` deliberately writes nothing when the
    state fingerprint is unchanged.  A counter incremented once per loop
    iteration reports 2 here, which is precisely the reporting-intent-as-outcome
    defect; reading the driver's counters reports 0.
    """
    store, tx = _make_store()
    tx.persist_writes = False

    result = await store.upsert_graph(
        source_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        entities=[_Entity("aws.resource.a"), _Entity("aws.resource.b")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=1,
    )

    # The transaction ran — both statements were issued...
    assert result.applied is True
    assert len(tx.entity_writes) == 2
    assert result.entities_submitted == 2
    # ...and persisted nothing.
    assert result.entities_written == 0
    assert result.nodes_created == 0
    assert result.properties_set == 0


# ---------------------------------------------------------------------------
# 2. Stale-write protection must survive the fix
# ---------------------------------------------------------------------------


async def test_redelivered_document_at_same_version_is_rejected() -> None:
    """NATS JetStream redelivery of an already-applied document must not rewrite."""
    store, tx = _make_store()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    first = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=3,
    )
    redelivered = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=3,
    )

    assert first.applied is True
    assert redelivered.applied is False
    assert redelivered.skip_reason == "stale_document_version"
    assert redelivered.entities_written == 0
    assert len(tx.entity_writes) == 1


async def test_out_of_order_document_version_is_rejected() -> None:
    """A lower doc_version arriving after a higher one must not overwrite it."""
    store, tx = _make_store()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=7,
    )
    stale = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=6,
    )

    assert stale.applied is False
    assert stale.skip_reason == "stale_document_version"
    assert len(tx.entity_writes) == 1


async def test_newer_document_version_is_applied() -> None:
    """A genuine document update (higher doc_version) must be written."""
    store, tx = _make_store()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    for version in (1, 2, 3):
        await store.upsert_graph(
            source_id=source_id,
            document_id=document_id,
            entities=[_Entity("aws.resource.a")],
            edges=[],
            workspace_id=_WORKSPACE,
            version=version,
        )

    assert len(tx.entity_writes) == 3


async def test_a_newer_epoch_supersedes_the_document_guard() -> None:
    """ADR-0017/0018: a rebuild epoch bump replays even a lower version."""
    store, tx = _make_store()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=10,
        epoch=1,
    )
    replay = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=5,
        epoch=2,
    )

    assert replay.applied is True
    assert len(tx.entity_writes) == 2


async def test_forced_replay_bypasses_the_document_guard() -> None:
    store, tx = _make_store()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=10,
    )
    forced = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=2,
        forced_replay=True,
    )

    assert forced.applied is True
    assert len(tx.entity_writes) == 2


async def test_version_none_writes_unconditionally() -> None:
    """``version=None`` is the legacy/unversioned path — no guard, no checkpoint."""
    store, tx = _make_store()
    source_id = uuid.uuid4()

    for _ in range(2):
        result = await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[_Entity("aws.resource.a")],
            edges=[],
            workspace_id=_WORKSPACE,
        )
        assert result.applied is True

    assert len(tx.entity_writes) == 2
    assert tx.source_checkpoints == {}


# ---------------------------------------------------------------------------
# 3. Source watermark stays monotonic (GlobalReconciler / dr_verify contract)
# ---------------------------------------------------------------------------


async def test_source_watermark_is_the_max_document_version() -> None:
    """``scripts/dr_verify.py``: Neo4j checkpoint == Postgres max(doc_version)."""
    store, tx = _make_store()
    source_id = uuid.uuid4()

    for version in (4, 1, 2):
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[_Entity("aws.resource.a")],
            edges=[],
            workspace_id=_WORKSPACE,
            version=version,
        )

    key = (str(_WORKSPACE), str(source_id))
    assert tx.source_checkpoints[key]["version"] == 4


async def test_source_watermark_resets_on_a_new_epoch() -> None:
    """A rebuild epoch restarts document versions, so the watermark must follow."""
    store, tx = _make_store()
    source_id = uuid.uuid4()

    await store.upsert_graph(
        source_id=source_id,
        document_id=uuid.uuid4(),
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=9,
        epoch=1,
    )
    await store.upsert_graph(
        source_id=source_id,
        document_id=uuid.uuid4(),
        entities=[_Entity("aws.resource.b")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=1,
        epoch=2,
    )

    key = (str(_WORKSPACE), str(source_id))
    assert tx.source_checkpoints[key] == {"version": 1, "epoch": 2}


# ---------------------------------------------------------------------------
# 4. Duplicate-checkpoint tolerance + the missing uniqueness constraint
# ---------------------------------------------------------------------------


async def test_checkpoint_read_tolerates_duplicate_nodes_and_takes_the_max() -> None:
    """10 duplicate ``:StoreCheckpoint`` nodes exist in the live database.

    ``Result.single()`` warns and hands back an arbitrary row when the match
    is not unique, so the guard becomes non-deterministic.  The read must
    consume every row and fold to the most protective (highest) value.
    """
    tx = _FakeTx()
    version, epoch = await Neo4jGraphStore._read_checkpoint(
        tx,
        "MATCH (c:StoreCheckpoint {workspace_id: $workspace_id, source_id: $source_id})"
        " RETURN c.version AS version, c.epoch AS epoch",
        {"workspace_id": str(_WORKSPACE), "source_id": str(uuid.uuid4())},
    )
    assert version is None
    assert epoch is None

    tx.duplicate_source_rows = [
        {"version": 2, "epoch": 1},
        {"version": 9, "epoch": 3},
        {"version": 4, "epoch": 2},
    ]
    version, epoch = await Neo4jGraphStore._read_checkpoint(
        tx,
        "MATCH (c:StoreCheckpoint {workspace_id: $workspace_id, source_id: $source_id})"
        " RETURN c.version AS version, c.epoch AS epoch",
        {"workspace_id": str(_WORKSPACE), "source_id": str(uuid.uuid4())},
    )
    assert version == 9
    assert epoch == 3


async def test_duplicate_checkpoints_do_not_let_a_stale_entity_write_through() -> None:
    """Behavioural consequence of the fold, on the read that faces the live duplicates.

    ``upsert_entity``'s coarse source-checkpoint guard is the code path that
    reads the 10 forked ``:StoreCheckpoint`` nodes.  Taking the first row (2)
    instead of the maximum (9) would admit a stale version-5 write.
    """
    from omniscience_core.storage.graph import EntityUpsert

    store, tx = _make_store()
    source_id = uuid.uuid4()
    tx.duplicate_source_rows = [
        {"version": 2, "epoch": None},
        {"version": 9, "epoch": None},
        {"version": 4, "epoch": None},
    ]

    await store.upsert_entity(
        entity=EntityUpsert(
            id=uuid.uuid4(),
            source_id=source_id,
            entity_type="resource",
            name="aws.resource.a",
            display_name="a",
            chunk_id=None,
            metadata={},
            version=5,
        ),
        workspace_id=_WORKSPACE,
    )

    assert tx.entity_writes == []


def test_bootstrap_declares_the_store_checkpoint_uniqueness_constraint() -> None:
    """The missing constraint is what let concurrent writers fork the checkpoint."""
    constraint = next(
        s for s in _BOOTSTRAP_STATEMENTS if "CONSTRAINT" in s and "StoreCheckpoint" in s
    )
    assert "IF NOT EXISTS" in constraint
    assert "workspace_id" in constraint
    assert "source_id" in constraint
    assert "IS UNIQUE" in constraint


def test_bootstrap_declares_the_document_checkpoint_uniqueness_constraint() -> None:
    constraint = next(
        s for s in _BOOTSTRAP_STATEMENTS if "CONSTRAINT" in s and "DocumentCheckpoint" in s
    )
    assert "IF NOT EXISTS" in constraint
    assert "document_id" in constraint
    assert "IS UNIQUE" in constraint


def test_a_checkpoint_heal_statement_exists() -> None:
    """``CREATE CONSTRAINT`` fails outright when duplicates already exist."""
    heal = next(s for s in _CHECKPOINT_HEAL_STATEMENTS if "StoreCheckpoint" in s)
    assert "collect(c)" in heal
    assert "size(nodes) > 1" in heal
    assert "DETACH DELETE" in heal


def test_the_checkpoint_heal_is_bounded_and_reports_its_progress() -> None:
    """Unbounded, it scans every tenant's checkpoints in one startup transaction.

    That is the transaction most likely to exceed the timeout or exhaust the
    heap on exactly the database that needs repairing — and it ran before the
    DDL with nothing catching it, so the server boot-looped.
    """
    heal = next(s for s in _CHECKPOINT_HEAL_STATEMENTS if "StoreCheckpoint" in s)
    assert "LIMIT $batch_size" in heal, "the heal must be batched"
    assert "collapsed" in heal, "each batch must report what it collapsed"


def test_the_checkpoint_heal_keeps_the_version_that_belongs_to_the_max_epoch() -> None:
    """Folding max(version) and max(epoch) separately fabricates a watermark.

    {epoch: 5, version: 2} + {epoch: 3, version: 99} used to survive as
    {epoch: 5, version: 99} — a pair no write ever produced, and one that
    rejects every legitimate epoch-5 document below version 99.  Ordering the
    group and keeping the head makes the survivor an observed row.
    """
    heal = next(s for s in _CHECKPOINT_HEAL_STATEMENTS if "StoreCheckpoint" in s)
    order_by = heal.index("ORDER BY")
    collect = heal.index("collect(c)")
    assert order_by < collect, "the keeper must be chosen by ORDER BY before collect()"
    assert "coalesce(c.epoch, -1) DESC" in heal
    assert "coalesce(c.version, -1) DESC" in heal
    assert "elementId(c)" in heal, "the tie-break must make the keeper deterministic"
    # Independent reduction is exactly what fabricated the pair.
    assert "reduce(" not in heal


async def test_the_checkpoint_heal_converges_over_several_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One batch is not enough; the loop must run until a batch collapses nothing."""
    store, tx = _make_store()
    monkeypatch.setattr(store_module, "_CHECKPOINT_HEAL_BATCH_SIZE", 2)
    tx.duplicate_checkpoint_groups = 7

    await store._bootstrap_schema()

    assert tx.duplicate_checkpoint_groups == 0, "the heal did not converge"
    # 2 + 2 + 2 + 1, then one more batch that collapses nothing and stops.
    assert len(tx.heal_batches) == 5
    assert set(tx.heal_batches) == {2}


async def test_a_failing_checkpoint_heal_does_not_prevent_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database that needs the repair must still be able to boot.

    The heal ran unconditionally with nothing catching it, so a heal that
    timed out took ``connect()`` with it — and there was no flag to skip it.
    """
    store, tx = _make_store()
    tx.fail_on_heal = True
    recorder = _RecordingLogger()
    monkeypatch.setattr(store_module, "log", recorder)

    await store.connect()

    assert store._bootstrapped is True
    events = {event for _method, event, _fields in recorder.events}
    assert "neo4j_checkpoint_heal_failed" in events


async def test_a_failing_checkpoint_constraint_does_not_prevent_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CREATE CONSTRAINT`` aborts against violating data — startup must not.

    Running without the constraint is degraded, not broken: ``_read_checkpoint``
    already folds the maximum over duplicate rows.  Boot-looping is worse.
    """
    store, tx = _make_store()
    tx.fail_on_checkpoint_constraint = True
    recorder = _RecordingLogger()
    monkeypatch.setattr(store_module, "log", recorder)

    await store.connect()

    assert store._bootstrapped is True
    events = {event for _method, event, _fields in recorder.events}
    assert "neo4j_checkpoint_constraint_not_installed" in events


async def test_bootstrap_runs_the_heal_before_the_constraint_ddl() -> None:
    """Order is load-bearing: healing after the DDL would never run."""
    store, tx = _make_store()
    await store._bootstrap_schema()

    executed = [q for q, _ in tx.queries]
    heal_index = next(
        i for i, q in enumerate(executed) if "DETACH DELETE d" in q and "StoreCheckpoint" in q
    )
    constraint_index = next(
        i for i, q in enumerate(executed) if "CONSTRAINT" in q and "StoreCheckpoint" in q
    )
    assert heal_index < constraint_index


# ---------------------------------------------------------------------------
# 5. The log must report persistence, not intent
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """Records (method, event, fields) instead of emitting.

    Patched over the store module's bound logger rather than reconfiguring
    structlog globally: ``structlog.get_logger()`` caches its bound logger on
    first use, so a global reconfigure mid-suite does not reliably reach a
    module-level logger another test already exercised.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, method: str) -> Any:
        def _log(event: str, **fields: Any) -> None:
            self.events.append((method, event, fields))

        return _log

    def __getattr__(self, name: str) -> Any:
        if name in {"debug", "info", "warning", "error", "critical"}:
            return self._record(name)
        raise AttributeError(name)

    def graph_events(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [e for e in self.events if e[1].startswith("neo4j_upsert_graph")]


async def test_a_skipped_write_is_logged_as_a_skip_at_info_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-fix log emitted ``entities=1`` for a write that persisted nothing."""
    store, _tx = _make_store()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=5,
    )

    recorder = _RecordingLogger()
    monkeypatch.setattr(store_module, "log", recorder)
    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity("aws.resource.a")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=5,
    )

    events = recorder.graph_events()
    assert events, "upsert_graph emitted no log line"
    method, event, fields = events[-1]
    # `debug` is invisible under the deployed LOG_LEVEL=info profile.
    assert method in {"info", "warning", "error"}
    assert event == "neo4j_upsert_graph_skipped"
    assert fields["applied"] is False
    assert fields["skip_reason"] == "stale_document_version"
    assert fields["entities_written"] == 0
    assert fields["edges_written"] == 0
    # The input batch is still reported, but as intent — clearly distinct from
    # what was written.  Reporting only this is what hid the drop.
    assert fields["entities_in_batch"] == 1


async def test_an_applied_write_logs_what_was_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _tx = _make_store()

    recorder = _RecordingLogger()
    monkeypatch.setattr(store_module, "log", recorder)
    await store.upsert_graph(
        source_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        entities=[_Entity("a"), _Entity("b")],
        edges=[],
        workspace_id=_WORKSPACE,
        version=1,
    )

    events = recorder.graph_events()
    assert events
    method, event, fields = events[-1]
    assert method == "info"
    assert event == "neo4j_upsert_graph"
    assert fields["applied"] is True
    assert fields["entities_written"] == 2
    assert fields["entities_in_batch"] == 2
