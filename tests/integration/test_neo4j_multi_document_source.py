"""Live-Neo4j gate: every document of a source must reach the graph.

Regression gate for the per-document graph-write checkpoint.  The unit-level
counterpart is ``tests/test_neo4j_graph_write_checkpoint.py``; this module
proves the same properties against a real Neo4j 5.19 instance, including the
two things a fake driver cannot prove:

* concurrent writers converge on **one** ``:StoreCheckpoint`` node once the
  uniqueness constraint exists (without it, ``MERGE`` takes no
  cross-transaction lock and each writer forks its own node — 10 such nodes
  exist in the live database this fix was diagnosed against);
* the bootstrap heals a database that already contains those duplicates,
  instead of aborting ``connect()`` on the ``CREATE CONSTRAINT``.

Configuration note
------------------

The store is built with ``bitemporal_enabled=True`` because that is the
deployed default (``Settings.graph_bitemporal`` defaults to ``"enabled"``
since #317, and the live database carries ``:EntityState`` nodes).  The
legacy path is a *replace-by-source* writer: it ``DETACH DELETE``s the whole
source before writing one document's batch, so a multi-document source
converges to the last document's entities under either the old or the new
guard.  That is a pre-existing property of the legacy writer, not something
this checkpoint fix changes — see the fix's integration notes.

Gate: runs whenever Docker + testcontainers are available; no opt-in env var,
for the reason given in ``test_neo4j_bootstrap_schema.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("testcontainers.neo4j")

from omniscience_index.stores.neo4j import store as store_module
from omniscience_index.stores.neo4j.store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
)

pytestmark = pytest.mark.asyncio

_NEO4J_PASSWORD = "multi_document_password"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(uri: str) -> Neo4jStoreConfig:
    return Neo4jStoreConfig(
        uri=uri,
        username="neo4j",
        password=_NEO4J_PASSWORD,
        database="neo4j",
        max_connection_pool_size=10,
        connection_acquisition_timeout_seconds=30.0,
        max_transaction_retry_time_seconds=15.0,
        default_max_depth=3,
        # Production default (Settings.graph_bitemporal == "enabled").
        bitemporal_enabled=True,
    )


def _neo4j_container() -> Any:
    """A memory-bounded Neo4j container.

    Two of the tests below need their own container (the uniqueness constraint
    must genuinely not exist when the duplicates are planted), so up to two run
    alongside the module fixture.  Neo4j sizes its heap and page cache from the
    host by default, which on a developer machine reserves far more than these
    few-node fixtures need and gets the container OOM-killed.  Capping both
    keeps the suite runnable next to whatever else is already on the daemon.
    """
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    return (
        Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
        .with_env("NEO4J_server_memory_heap_initial__size", "256m")
        .with_env("NEO4J_server_memory_heap_max__size", "512m")
        .with_env("NEO4J_server_memory_pagecache_size", "128m")
    )


@pytest.fixture(scope="module")
def neo4j_container() -> Any:
    with _neo4j_container() as container:
        yield container


@pytest.fixture
async def store(neo4j_container: Any) -> AsyncIterator[Neo4jGraphStore]:
    """A connected store; each test uses its own workspace for isolation."""
    graph_store = Neo4jGraphStore(config=_config(neo4j_container.get_connection_url()))
    await graph_store.connect()
    try:
        yield graph_store
    finally:
        await graph_store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Entity:
    """Duck-typed parser-style entity (matches ``_entity_to_params``)."""

    def __init__(self, workspace_id: uuid.UUID, name: str) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = workspace_id
        self.entity_type = "resource"
        self.name = name
        self.display_name = name.rsplit(".", 1)[-1]
        self.chunk_id: uuid.UUID | None = None
        self.metadata: dict[str, Any] = {"workspace_id": str(workspace_id)}


async def _read(store: Neo4jGraphStore, cypher: str, params: dict[str, Any]) -> list[Any]:
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        return [record async for record in result]


async def _source_checkpoints(
    store: Neo4jGraphStore, workspace_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = await _read(
        store,
        "MATCH (c:StoreCheckpoint {workspace_id: $workspace_id}) "
        "RETURN c.source_id AS source_id, c.version AS version, c.epoch AS epoch",
        {"workspace_id": str(workspace_id)},
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_every_document_of_a_source_reaches_the_graph(store: Neo4jGraphStore) -> None:
    """The gate that would have caught 978 documents collapsing to 15 entities."""
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_count = 8

    for i in range(document_count):
        result = await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[_Entity(workspace_id, f"aws.ec2.instance.{i}")],
            edges=[],
            workspace_id=workspace_id,
            # Every document of a freshly-crawled source carries doc_version 1.
            version=1,
        )
        assert result.applied is True, f"document {i} was dropped"
        assert result.entities_written == 1

    assert await store.count_entities(workspace_id=workspace_id) == document_count


async def test_redelivery_is_rejected_without_losing_prior_documents(
    store: Neo4jGraphStore,
) -> None:
    """JetStream at-least-once redelivery must be a no-op, not a rewrite."""
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_ids = [uuid.uuid4() for _ in range(3)]

    for document_id in document_ids:
        await store.upsert_graph(
            source_id=source_id,
            document_id=document_id,
            entities=[_Entity(workspace_id, f"aws.s3.bucket.{document_id}")],
            edges=[],
            workspace_id=workspace_id,
            version=1,
        )

    before = await store.count_entities(workspace_id=workspace_id)
    assert before == 3

    redelivered = await store.upsert_graph(
        source_id=source_id,
        document_id=document_ids[0],
        entities=[_Entity(workspace_id, "aws.s3.bucket.redelivered")],
        edges=[],
        workspace_id=workspace_id,
        version=1,
    )

    assert redelivered.applied is False
    assert redelivered.skip_reason == "stale_document_version"
    assert redelivered.entities_written == 0
    assert await store.count_entities(workspace_id=workspace_id) == before


async def test_a_document_update_is_applied(store: Neo4jGraphStore) -> None:
    """A real edit bumps doc_version and must overwrite that document's graph."""
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    first = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity(workspace_id, "aws.rds.instance.v1")],
        edges=[],
        workspace_id=workspace_id,
        version=1,
    )
    second = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity(workspace_id, "aws.rds.instance.v2")],
        edges=[],
        workspace_id=workspace_id,
        version=2,
    )

    assert first.applied is True
    assert second.applied is True
    assert await store.count_entities(workspace_id=workspace_id) == 2


async def test_source_watermark_tracks_the_max_document_version(
    store: Neo4jGraphStore,
) -> None:
    """``scripts/dr_verify.py``: Neo4j checkpoint == Postgres max(doc_version)."""
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()

    for version in (3, 1, 7, 2):
        await store.upsert_graph(
            source_id=source_id,
            document_id=uuid.uuid4(),
            entities=[_Entity(workspace_id, f"aws.lambda.fn.{version}")],
            edges=[],
            workspace_id=workspace_id,
            version=version,
        )

    checkpoints = await _source_checkpoints(store, workspace_id)
    assert len(checkpoints) == 1
    assert checkpoints[0]["version"] == 7


async def test_concurrent_writes_to_one_source_keep_a_single_checkpoint(
    store: Neo4jGraphStore,
) -> None:
    """Without the uniqueness constraint each writer forked its own node."""
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    concurrency = 12

    results = await asyncio.gather(
        *(
            store.upsert_graph(
                source_id=source_id,
                document_id=uuid.uuid4(),
                entities=[_Entity(workspace_id, f"aws.eks.node.{i}")],
                edges=[],
                workspace_id=workspace_id,
                version=1,
            )
            for i in range(concurrency)
        )
    )

    assert all(r.applied for r in results)
    checkpoints = await _source_checkpoints(store, workspace_id)
    assert len(checkpoints) == 1, f"checkpoint forked into {len(checkpoints)} nodes"
    assert await store.count_entities(workspace_id=workspace_id) == concurrency


async def test_written_counts_report_persistence_not_submission(
    store: Neo4jGraphStore,
) -> None:
    """``entities_written`` must come from the database, not from ``len()``.

    The bitemporal writer is deliberately inert when an entity's state
    fingerprint is unchanged (``_UPSERT_ENTITY_BITEMPORAL_CYPHER``), which is
    the ordinary case on a re-crawl.  A counter incremented once per loop
    iteration reports 1 for that write — indistinguishable from a write that
    actually persisted something, and exactly the reporting-intent-as-outcome
    defect ``GraphWriteResult`` exists to prevent.

    The document version differs between the two calls, so the per-document
    staleness guard admits both: what changes is only whether the entity
    statement had anything to write.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    def _stable_entity() -> _Entity:
        entity = _Entity(workspace_id, "aws.ec2.instance.stable")
        entity.id = uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}|stable")
        return entity

    first = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_stable_entity()],
        edges=[],
        workspace_id=workspace_id,
        version=1,
    )
    # Byte-identical content at a higher doc_version — the write is applied,
    # the guard is satisfied, and the entity statement changes nothing.
    replay = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_stable_entity()],
        edges=[],
        workspace_id=workspace_id,
        version=2,
    )

    assert first.applied is True
    assert first.entities_written == 1
    assert first.entities_submitted == 1
    assert first.nodes_created > 0

    assert replay.applied is True, "the write was not rejected — only inert"
    assert replay.entities_submitted == 1, "the caller still submitted one entity"
    assert replay.entities_written == 0, "nothing was persisted, so nothing may be reported"
    assert replay.nodes_created == 0
    assert replay.properties_set == 0


async def test_a_newer_epoch_resets_the_version_guard(store: Neo4jGraphStore) -> None:
    """ADR-0017/0018 epoch bump, proven against a real database.

    ``_ADVANCE_*_CHECKPOINT_CYPHER`` depends on Cypher applying SET items in
    order so ``version`` is computed while ``c.epoch`` still holds the previous
    epoch.  That ordering is a property of the server, not of the adapter, so a
    fake transaction cannot establish it.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity(workspace_id, "aws.rds.instance.epoch1")],
        edges=[],
        workspace_id=workspace_id,
        version=9,
        epoch=1,
    )
    # A rebuild restarts document versions from 1.  Without the epoch clause
    # this is rejected as stale (1 <= 9) and the rebuild writes nothing.
    replay = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity(workspace_id, "aws.rds.instance.epoch2")],
        edges=[],
        workspace_id=workspace_id,
        version=1,
        epoch=2,
    )
    assert replay.applied is True, "a newer epoch must supersede the version guard"

    checkpoints = await _source_checkpoints(store, workspace_id)
    assert len(checkpoints) == 1
    # The watermark follows the epoch rather than staying at the old maximum.
    assert checkpoints[0]["version"] == 1
    assert checkpoints[0]["epoch"] == 2

    # ...and the guard is live again inside the new epoch.
    stale_in_new_epoch = await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[_Entity(workspace_id, "aws.rds.instance.epoch2.replay")],
        edges=[],
        workspace_id=workspace_id,
        version=1,
        epoch=2,
    )
    assert stale_in_new_epoch.applied is False
    assert stale_in_new_epoch.skip_reason == "stale_document_version"


async def test_checkpoint_uniqueness_constraints_are_installed(
    store: Neo4jGraphStore,
) -> None:
    rows = await _read(
        store,
        "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties "
        "RETURN name, labelsOrTypes, properties",
        {},
    )
    by_label = {
        str(r["labelsOrTypes"][0]): list(r["properties"]) for r in rows if r["labelsOrTypes"]
    }
    assert by_label.get("StoreCheckpoint") == ["workspace_id", "source_id"]
    assert by_label.get("DocumentCheckpoint") == [
        "workspace_id",
        "source_id",
        "document_id",
    ]


# ---------------------------------------------------------------------------
# Upgrade path: a database that already contains duplicate checkpoints
# ---------------------------------------------------------------------------


async def test_bootstrap_heals_pre_existing_duplicate_checkpoints() -> None:
    """A pre-constraint database must not break ``connect()``.

    ``CREATE CONSTRAINT ... IF NOT EXISTS`` fails outright against violating
    data, so bootstrapping a database that already forked its checkpoints
    would abort startup unless the heal runs first.  Uses its own container so
    the constraint genuinely does not exist when the duplicates are planted.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()

    with _neo4j_container() as container:
        store = Neo4jGraphStore(config=_config(container.get_connection_url()))
        # Plant the duplicates BEFORE any bootstrap, exactly as concurrent
        # writers did against an unconstrained database.
        async with store._driver.session(database="neo4j") as session:
            for version in (1, 4, 2):
                await session.run(
                    "CREATE (c:StoreCheckpoint {workspace_id: $workspace_id, "
                    "source_id: $source_id, version: $version})",
                    {
                        "workspace_id": str(workspace_id),
                        "source_id": str(source_id),
                        "version": version,
                    },
                )

        planted = await _source_checkpoints(store, workspace_id)
        assert len(planted) == 3

        try:
            # Must not raise on the CREATE CONSTRAINT step.
            await store.connect()

            healed = await _source_checkpoints(store, workspace_id)
            assert len(healed) == 1
            # The surviving node keeps the highest observed version: a
            # watermark must never move backwards.
            assert healed[0]["version"] == 4

            # And the constraint is now in force.
            await store.connect()
            assert len(await _source_checkpoints(store, workspace_id)) == 1
        finally:
            await store.close()


async def test_the_heal_batches_and_keeps_the_version_of_the_surviving_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two properties a single unbounded statement could not provide.

    * **Convergence.**  ``_CHECKPOINT_HEAL_BATCH_SIZE`` is forced to 1 here, so
      three forked sources need three collapsing batches plus a fourth that
      finds nothing.  A one-shot heal leaves two of them behind — and the
      ``CREATE CONSTRAINT`` that follows then aborts ``connect()``.
    * **Keeper selection.**  Folding ``max(version)`` and ``max(epoch)``
      independently turned {epoch: 5, version: 2} + {epoch: 3, version: 99}
      into {epoch: 5, version: 99}: a watermark no write ever produced, which
      rejects every legitimate epoch-5 document below version 99.
    """
    monkeypatch.setattr(store_module, "_CHECKPOINT_HEAL_BATCH_SIZE", 1)

    workspace_id = uuid.uuid4()
    #: (source, [(version, epoch), ...]) — the third source is the F3 case.
    forked = {
        uuid.uuid4(): [(1, 1), (4, 1)],
        uuid.uuid4(): [(7, 2), (2, 2), (5, 2)],
        uuid.uuid4(): [(2, 5), (99, 3)],
    }
    source_order = list(forked)
    epoch_mixed_source = source_order[2]

    with _neo4j_container() as container:
        store = Neo4jGraphStore(config=_config(container.get_connection_url()))
        async with store._driver.session(database="neo4j") as session:
            for source_id, rows in forked.items():
                for version, epoch in rows:
                    await session.run(
                        "CREATE (c:StoreCheckpoint {workspace_id: $workspace_id, "
                        "source_id: $source_id, version: $version, epoch: $epoch})",
                        {
                            "workspace_id": str(workspace_id),
                            "source_id": str(source_id),
                            "version": version,
                            "epoch": epoch,
                        },
                    )

        assert len(await _source_checkpoints(store, workspace_id)) == 7

        try:
            await store.connect()

            healed = await _source_checkpoints(store, workspace_id)
            assert len(healed) == len(forked), "the heal did not converge over its batches"

            by_source = {str(row["source_id"]): row for row in healed}
            # Ordinary case: one epoch, keep the highest version.
            assert by_source[str(source_order[0])]["version"] == 4
            assert by_source[str(source_order[1])]["version"] == 7
            # F3: the survivor is an OBSERVED (epoch, version) pair.
            survivor = by_source[str(epoch_mixed_source)]
            assert survivor["epoch"] == 5
            assert survivor["version"] == 2, "a watermark was fabricated across epochs"
        finally:
            await store.close()
