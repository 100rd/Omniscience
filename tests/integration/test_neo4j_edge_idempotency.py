"""Live-Neo4j gate: ingesting a document twice must not grow the edge set.

The defect this module gates was worth 8898 ``depends_on`` relationships where
1159 were due — roughly eightfold on every edge whose target was not defined by
the document asserting it.  The full suite passed throughout, because no test
ever wrote two edges pointing at the *same unresolved target name*.

Mechanism
---------

``_UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE`` used to open with::

    MERGE (b:Entity {workspace_id: $workspace_id, name: $target_name})

``name`` carries an index but no uniqueness *constraint*, and Neo4j only takes
a cross-transaction lock for MERGE on a constrained key.  So:

1. concurrent refs (``_DISCOVERY_CONCURRENCY == 10``) each created their own
   stub, leaving ten `:Entity` nodes sharing one name; then
2. that MERGE bound *all ten*, so the relationship MERGE below it ran ten
   times and every later write of that edge produced ten relationships.

Step 2 is what made it permanent: the amplification is deterministic, not a
one-off race, so it reproduces on a *sequential* replay once the duplicates
exist.  ``test_a_forked_graph_stops_amplifying_edges`` covers that without
needing concurrency.

Step 1 — the fork — needs concurrency that genuinely overlaps *inside* the
MERGE, which ``asyncio.gather`` over the public ingest path does not deliver:
those transactions serialise on the shared source-checkpoint node long before
they reach the stub write, so each one already sees the previous writer's
committed stub and the pre-fix code passes.  ``test_concurrent_writers_racing_
on_one_target_name_create_one_stub`` therefore stages the overlap explicitly
via ``_merge_under_barrier``; read its docstring before weakening it back to a
plain gather.  ``test_the_public_ingest_path_converges_under_concurrency``
keeps the public path covered, but it is coverage, not the reproducer.

Edges whose target resolves inside the batch never took this path; they go
through ``_UPSERT_EDGE_CYPHER_TEMPLATE``, which MATCHes both endpoints on the
constrained ``(workspace_id, id)`` key.  ``test_in_batch_targets_were_never_
affected`` pins that asymmetry — the reason the live graph's
``in_organization`` and ``in_ou`` counts were exact while ``in_account`` was
multiplied.

Gate: runs whenever Docker + testcontainers are available; no opt-in env var,
matching ``test_neo4j_multi_document_source.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from neo4j.exceptions import Neo4jError

pytest.importorskip("testcontainers.neo4j")

from omniscience_index.stores.neo4j._cypher import _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE
from omniscience_index.stores.neo4j.mappers import _serialise_metadata_param, _stub_entity_id
from omniscience_index.stores.neo4j.store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
)

pytestmark = pytest.mark.asyncio

_NEO4J_PASSWORD = "edge_idempotency_password"

#: The unresolved target every fixture document points at — an account root
#: ARN, exactly the shape that produced 8541 ``in_account`` edges live.
_ACCOUNT_ARN = "arn:aws:iam::000000000000:root"

#: ``_DISCOVERY_CONCURRENCY`` — the number of refs the crawler resolves in
#: parallel, and the number of stub copies the live graph ended up holding.
_DISCOVERY_CONCURRENCY = 10

#: Per-transaction server-side timeout for the barrier writers.  Deliberately
#: far larger than the hold below: the writer that wins the race must still be
#: healthy enough to commit when the hold is lifted.
_MERGE_TX_TIMEOUT_SECONDS = 30.0

#: How long the hold past the MERGE lasts when not every writer has reported.
#:
#: Under the pre-fix Cypher this timer never fires — nothing locks, so all N
#: writers report within milliseconds and the hold is released by consensus.
#: Under the fix the losers block on the winner's uniqueness-constraint lock
#: and cannot report at all, so the hold has to be lifted on a timer; that is
#: what "Neo4j serialises on the constraint" looks like from the client side,
#: and it is the expected behaviour, not an error.
_MERGE_HOLD_GRACE_SECONDS = 3.0

#: Ceiling for the whole staged phase — a safety net so a driver or server
#: change that breaks the staging fails loudly instead of hanging the suite.
_BARRIER_PHASE_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(uri: str, *, stub_heal: bool = False) -> Neo4jStoreConfig:
    return Neo4jStoreConfig(
        uri=uri,
        username="neo4j",
        password=_NEO4J_PASSWORD,
        database="neo4j",
        # One connection per barrier writer plus headroom.  ``_merge_under_
        # barrier`` holds all ``_DISCOVERY_CONCURRENCY`` transactions open at
        # once and only then releases them; a pool sized at or below the writer
        # count makes the last writer block on connection *acquisition* rather
        # than on the uniqueness constraint — a different, and vacuous, reason
        # for it never to report.
        max_connection_pool_size=_DISCOVERY_CONCURRENCY + 1,
        connection_acquisition_timeout_seconds=30.0,
        max_transaction_retry_time_seconds=15.0,
        default_max_depth=3,
        # Production default (Settings.graph_bitemporal == "enabled").
        bitemporal_enabled=True,
        stub_heal_enabled=stub_heal,
    )


@pytest.fixture(scope="module")
def neo4j_container() -> Any:
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
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
        # Deterministic so a replay of the same document upserts, not inserts.
        self.id = uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}|{name}")
        self.workspace_id = workspace_id
        self.entity_type = "aws_live"
        self.name = name
        self.display_name = name.rsplit("/", 1)[-1]
        self.chunk_id: uuid.UUID | None = None
        self.metadata: dict[str, Any] = {"arn": name}


class _NameEdge:
    """Duck-typed edge whose target is named, not resolved (``target_name``)."""

    def __init__(self, source: _Entity, target_name: str, relation: str) -> None:
        self.source_entity_id = source.id
        self.target_entity_id: uuid.UUID | None = None
        self.target_name = target_name
        self.edge_type = "depends_on"
        self.metadata: dict[str, Any] = {
            "asserted_by": source.name,
            "relation": relation,
        }


async def _read(store: Neo4jGraphStore, cypher: str, params: dict[str, Any]) -> list[Any]:
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        return [record async for record in result]


async def _edge_count(store: Neo4jGraphStore, workspace_id: uuid.UUID) -> int:
    rows = await _read(
        store,
        "MATCH ()-[r:depends_on]->() WHERE r.workspace_id = $workspace_id "
        "RETURN count(r) AS total",
        {"workspace_id": str(workspace_id)},
    )
    return int(rows[0]["total"])


async def _node_count_by_name(store: Neo4jGraphStore, workspace_id: uuid.UUID, name: str) -> int:
    rows = await _read(
        store,
        "MATCH (n:Entity {workspace_id: $workspace_id, name: $name}) RETURN count(n) AS total",
        {"workspace_id": str(workspace_id), "name": name},
    )
    return int(rows[0]["total"])


async def _ingest_document(
    store: Neo4jGraphStore,
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    document_id: uuid.UUID,
    resource_name: str,
    version: int,
) -> Any:
    """One inventory document: one resource, one edge to its account root."""
    entity = _Entity(workspace_id, resource_name)
    return await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[entity],
        edges=[_NameEdge(entity, _ACCOUNT_ARN, "in_account")],
        workspace_id=workspace_id,
        version=version,
    )


async def _plant_forked_stubs(
    store: Neo4jGraphStore,
    workspace_id: uuid.UUID,
    *,
    forks: int,
    callers: int = 0,
) -> list[str]:
    """Recreate the pre-fix state: N stubs on one name, each carrying the edges.

    Mirrors what the old writer produced — every caller fanned out across every
    stub copy that existed when it ran.
    """
    caller_names = [f"arn:aws:s3:::caller-{i}" for i in range(callers)]
    async with store._driver.session(database=store._config.database) as session:
        for i in range(forks):
            await session.run(
                "CREATE (n:Entity {workspace_id: $workspace_id, id: $id, name: $name, "
                "is_stub: true, created_at: $now})",
                {
                    "workspace_id": str(workspace_id),
                    "id": str(uuid.uuid4()),
                    "name": _ACCOUNT_ARN,
                    "now": f"2026-08-07T16:28:{37 + i:02d}.000000+00:00",
                },
            )
        for caller in caller_names:
            await session.run(
                "CREATE (n:Entity {workspace_id: $workspace_id, id: $id, name: $name, "
                "is_stub: false, kind: 'aws_live'})",
                {
                    "workspace_id": str(workspace_id),
                    "id": str(uuid.uuid4()),
                    "name": caller,
                },
            )
            await session.run(
                "MATCH (a:Entity {workspace_id: $workspace_id, name: $caller}) "
                "MATCH (b:Entity {workspace_id: $workspace_id, name: $target}) "
                "CREATE (a)-[:depends_on {workspace_id: $workspace_id, "
                "edge_type: 'depends_on', metadata: $metadata}]->(b)",
                {
                    "workspace_id": str(workspace_id),
                    "caller": caller,
                    "target": _ACCOUNT_ARN,
                    "metadata": '{"relation":"in_account"}',
                },
            )
    return caller_names


async def _create_callers(
    store: Neo4jGraphStore,
    workspace_id: uuid.UUID,
    names: list[str],
) -> list[uuid.UUID]:
    """Commit one real (non-stub) source entity per name; return their ids.

    The by-name edge Cypher opens with ``MATCH (a:Entity {workspace_id, id})``,
    so the sources must exist and be committed before the race starts —
    otherwise the statement matches nothing and writes nothing, which would
    make the reproducer vacuously green.
    """
    ids = [uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}|{name}") for name in names]
    async with store._driver.session(database=store._config.database) as session:
        for entity_id, name in zip(ids, names, strict=True):
            await session.run(
                "CREATE (n:Entity {workspace_id: $workspace_id, id: $id, name: $name, "
                "is_stub: false, kind: 'aws_live'})",
                {"workspace_id": str(workspace_id), "id": str(entity_id), "name": name},
            )
    return ids


async def _merge_under_barrier(
    store: Neo4jGraphStore,
    *,
    workspace_id: uuid.UUID,
    target_name: str,
    caller_ids: list[uuid.UUID],
) -> tuple[list[str], int]:
    """Run the by-name upsert in N transactions that provably overlap.

    Three staged phases, each gated on *every* writer arriving:

    1. **open** — each writer begins a transaction and forces a trivial
       statement through it, so "open" means open on the server rather than
       merely constructed in the driver.  Nobody proceeds until all N are.
    2. **merge** — every writer fires the by-name upsert at the same instant.
       Because no writer has committed, each one's target lookup sees a
       database with no stub in it.  This is the state the live crawler was in
       and the state a plain ``asyncio.gather`` never reaches.
    3. **release** — writers commit only after *all* N have reported the
       outcome of phase 2, or after ``_MERGE_HOLD_GRACE_SECONDS`` if some of
       them cannot report because they are blocked.  Holding the winner's
       transaction open past the MERGE is the whole point: it denies the losers
       the shortcut of observing an already-committed stub.

    Under the pre-fix Cypher (MERGE on the unconstrained ``name``) no lock is
    taken, so all N writers create their own stub, all N report immediately,
    the hold is released by consensus and all N commit.  Under the fix (MERGE
    on the constrained ``(workspace_id, id)``) the losers block on the winner's
    index lock; the grace timer lifts the hold, the winner commits, and the
    losers then converge on the node it created.  Either way exactly one node
    must carry the name.

    Returns one outcome per writer — ``"merged"`` (statement ran and committed)
    or ``"conflicted"`` (the constraint serialised it away, an accepted outcome
    rather than a failure) — together with **how many writers had reported at
    the instant the hold was lifted**.

    That second number is the barrier's own health check.  If every writer
    reports before the hold lifts, nothing locked: the writers overlapped in
    wall-clock time but not inside the MERGE, and the whole arrangement has
    quietly degraded into an ``asyncio.gather`` that cannot observe the defect.
    The caller asserts on it so that degradation fails loudly instead of
    passing as a green test that proves nothing.
    """
    cypher = _UPSERT_EDGE_BY_NAME_CYPHER_TEMPLATE.replace("{edge_type}", "depends_on")
    writers = len(caller_ids)
    opened = 0
    reported = 0
    reported_at_release = -1
    all_open = asyncio.Event()
    all_reported = asyncio.Event()
    release = asyncio.Event()

    def _params(caller_id: uuid.UUID) -> dict[str, Any]:
        params: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "source_id_ent": str(caller_id),
            "target_name": target_name,
            "stub_id": str(_stub_entity_id(workspace_id, target_name)),
            "source_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}|source")),
            "edge_type": "depends_on",
            "metadata": {"relation": "in_account", "asserted_by": str(caller_id)},
            "now": datetime.now(UTC).isoformat(),
        }
        _serialise_metadata_param(params)
        return params

    async def _writer(caller_id: uuid.UUID) -> str:
        nonlocal opened, reported
        async with store._driver.session(database=store._config.database) as session:
            tx = await session.begin_transaction(timeout=_MERGE_TX_TIMEOUT_SECONDS)
            try:
                await (await tx.run("RETURN 1 AS ok")).consume()
                opened += 1
                if opened == writers:
                    all_open.set()
                await all_open.wait()

                outcome = "merged"
                try:
                    await (await tx.run(cypher, _params(caller_id))).consume()
                except Neo4jError:
                    outcome = "conflicted"

                reported += 1
                if reported == writers:
                    all_reported.set()
                await release.wait()

                if outcome == "conflicted":
                    return outcome
                try:
                    await tx.commit()
                except Neo4jError:
                    return "conflicted"
                return outcome
            finally:
                with contextlib.suppress(Exception):
                    await tx.close()

    async def _lift_the_hold() -> None:
        """Release the writers once they have all reported — or all stalled."""
        nonlocal reported_at_release
        await all_open.wait()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(all_reported.wait(), timeout=_MERGE_HOLD_GRACE_SECONDS)
        # Sampled BEFORE ``release`` is set: afterwards the blocked writers
        # unblock and the count no longer describes the contended window.
        reported_at_release = reported
        release.set()

    async def _race() -> list[str]:
        holder = asyncio.create_task(_lift_the_hold())
        try:
            return await asyncio.gather(*(_writer(caller_id) for caller_id in caller_ids))
        finally:
            holder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await holder

    outcomes = await asyncio.wait_for(_race(), timeout=_BARRIER_PHASE_TIMEOUT_SECONDS)
    return outcomes, reported_at_release


# ---------------------------------------------------------------------------
# The bar: the same document twice must not grow the edge set
# ---------------------------------------------------------------------------


async def test_reingesting_one_document_does_not_grow_the_edge_set(
    store: Neo4jGraphStore,
) -> None:
    """Two ingests of the same document converge on one edge, not two."""
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    resource = "arn:aws:s3:::bucket-alpha"

    first = await _ingest_document(
        store,
        workspace_id=workspace_id,
        source_id=source_id,
        document_id=document_id,
        resource_name=resource,
        version=1,
    )
    after_first = await _edge_count(store, workspace_id)

    # A re-crawl bumps doc_version; replaying version 1 would be dropped by the
    # per-document staleness guard and prove nothing about the edge writer.
    second = await _ingest_document(
        store,
        workspace_id=workspace_id,
        source_id=source_id,
        document_id=document_id,
        resource_name=resource,
        version=2,
    )

    assert first.applied is True
    assert second.applied is True
    assert after_first == 1
    assert await _edge_count(store, workspace_id) == 1
    assert await _node_count_by_name(store, workspace_id, _ACCOUNT_ARN) == 1


async def test_three_ingests_still_converge_on_one_edge(store: Neo4jGraphStore) -> None:
    """Convergence must hold for any number of replays, not just two."""
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    resource = "arn:aws:s3:::bucket-beta"

    for version in (1, 2, 3):
        result = await _ingest_document(
            store,
            workspace_id=workspace_id,
            source_id=source_id,
            document_id=document_id,
            resource_name=resource,
            version=version,
        )
        assert result.applied is True
        assert await _edge_count(store, workspace_id) == 1, f"grew on ingest {version}"

    assert await _node_count_by_name(store, workspace_id, _ACCOUNT_ARN) == 1


# ---------------------------------------------------------------------------
# The fork: concurrent writers must converge on one stub
# ---------------------------------------------------------------------------


async def test_concurrent_writers_racing_on_one_target_name_create_one_stub(
    store: Neo4jGraphStore,
) -> None:
    """The reproducer.  Ten overlapping writers forked ten stubs in the live graph.

    WHY THE BARRIER — do not weaken this back to ``asyncio.gather`` over
    ``_ingest_document``.  That is what this test used to do, and it passed
    against a faithful reconstruction of the pre-fix writer five times out of
    five: it proved nothing.  The reason is that ``upsert_graph`` writes the
    shared ``:StoreCheckpoint`` for the source before it reaches the by-name
    edge, so ten concurrent ingests of one source serialise on that node's lock
    and effectively run one after another — by the time writer *n* reaches the
    stub MERGE, writer *n-1* has already committed a stub for it to find, and
    even an unconstrained MERGE on ``name`` converges.  The defect only appears
    when the lookups overlap: the live crawler resolved ``_DISCOVERY_CONCURRENCY
    == 10`` refs in parallel against ten *different* sources, so ten
    transactions read "no stub with this name" before any of them committed,
    each created its own, and the graph ended up with ten ``:Entity`` nodes
    sharing one name (and, through the fan-out half of the defect, 8898
    ``depends_on`` edges where 1159 were due).  ``_merge_under_barrier``
    reconstructs exactly that window — all ten transactions open, all ten fire
    the by-name upsert, and nobody commits until every one of them has run —
    which is the only arrangement in which the pre-fix Cypher can be observed
    to fork.  A ``gather`` cannot express "hold the winner open past the MERGE",
    and without that hold the losers get a shortcut the live system never had.
    """
    workspace_id = uuid.uuid4()
    caller_names = [
        f"arn:aws:ec2:eu-central-1:000000000000:instance/i-{i:04d}"
        for i in range(_DISCOVERY_CONCURRENCY)
    ]
    caller_ids = await _create_callers(store, workspace_id, caller_names)

    outcomes, reported_at_release = await _merge_under_barrier(
        store,
        workspace_id=workspace_id,
        target_name=_ACCOUNT_ARN,
        caller_ids=caller_ids,
    )

    assert len(outcomes) == _DISCOVERY_CONCURRENCY
    # The barrier's own health check.  Under the fix the losers block inside
    # the MERGE on the winner's ``entity_workspace_id_unique`` lock and cannot
    # report, so the hold is lifted by the grace timer with a minority
    # reported.  If ALL of them reported, nothing serialised them: the
    # transactions overlapped in wall-clock time but not inside the MERGE, and
    # this test has silently decayed into the ``asyncio.gather`` that its own
    # docstring warns cannot observe the defect.  Assert it rather than let a
    # meaningless green pass.
    assert 0 <= reported_at_release < _DISCOVERY_CONCURRENCY, (
        f"the barrier did not contend: {reported_at_release} of "
        f"{_DISCOVERY_CONCURRENCY} writers reported before the hold lifted, so "
        f"no writer was serialised by the uniqueness constraint"
    )
    # Losing the race is the *fixed* behaviour: Neo4j serialises concurrent
    # creators on ``entity_workspace_id_unique``.  What is never permitted is a
    # second node carrying the same name.
    merged = outcomes.count("merged")
    assert merged >= 1, f"no writer committed at all: {outcomes}"

    stub_copies = await _node_count_by_name(store, workspace_id, _ACCOUNT_ARN)
    assert stub_copies == 1, f"stub forked into {stub_copies} nodes under a real merge overlap"

    # ...and it is the deterministic node, not whichever writer happened to win.
    rows = await _read(
        store,
        "MATCH (n:Entity {workspace_id: $workspace_id, name: $name}) RETURN n.id AS id",
        {"workspace_id": str(workspace_id), "name": _ACCOUNT_ARN},
    )
    assert {str(row["id"]) for row in rows} == {str(_stub_entity_id(workspace_id, _ACCOUNT_ARN))}

    # Every edge that committed landed on that one node — no fan-out.
    assert await _edge_count(store, workspace_id) == merged


async def test_the_public_ingest_path_converges_under_concurrency(
    store: Neo4jGraphStore,
) -> None:
    """Coverage of the public path — NOT the fork reproducer.

    ``upsert_graph`` serialises these ingests on the shared source checkpoint
    (see ``test_concurrent_writers_racing_on_one_target_name_create_one_stub``
    for why that makes them harmless), so this test cannot fail on the pre-fix
    writer.  It is here so the store-level API stays exercised end to end: ten
    documents, one shared unresolved target, one stub and one edge per
    document.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()

    results = await asyncio.gather(
        *(
            _ingest_document(
                store,
                workspace_id=workspace_id,
                source_id=source_id,
                document_id=uuid.uuid4(),
                resource_name=f"arn:aws:ec2:eu-central-1:000000000000:instance/i-{i:04d}",
                version=1,
            )
            for i in range(_DISCOVERY_CONCURRENCY)
        )
    )

    assert all(r.applied for r in results)

    stub_copies = await _node_count_by_name(store, workspace_id, _ACCOUNT_ARN)
    assert stub_copies == 1, f"stub forked into {stub_copies} nodes"

    # One edge per resource — not one per resource per stub copy.
    assert await _edge_count(store, workspace_id) == _DISCOVERY_CONCURRENCY


async def test_stub_identity_is_deterministic_across_processes(
    store: Neo4jGraphStore,
) -> None:
    """The stub id must be derivable, not random.

    A ``uuid4()`` id leaves ``name`` as the only stable key, and ``name`` is
    precisely the key that cannot be constrained — real entities legitimately
    share names across sources.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()

    await _ingest_document(
        store,
        workspace_id=workspace_id,
        source_id=source_id,
        document_id=uuid.uuid4(),
        resource_name="arn:aws:s3:::bucket-gamma",
        version=1,
    )

    rows = await _read(
        store,
        "MATCH (n:Entity {workspace_id: $workspace_id, name: $name}) RETURN n.id AS id",
        {"workspace_id": str(workspace_id), "name": _ACCOUNT_ARN},
    )
    assert len(rows) == 1
    assert str(rows[0]["id"]) == str(_stub_entity_id(workspace_id, _ACCOUNT_ARN))


# ---------------------------------------------------------------------------
# The amplification, reproduced without concurrency
# ---------------------------------------------------------------------------


async def test_a_forked_graph_stops_amplifying_edges(store: Neo4jGraphStore) -> None:
    """Duplicates already in the database must not multiply the next write.

    This is the half that turned a startup race into a permanent eightfold
    inflation: ``MERGE`` binds *every* match, so a name held by N nodes made
    the relationship MERGE below it run N times, on every write, forever.

    The duplicates are planted directly — as concurrent writers left them — so
    the amplification reproduces deterministically.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    forks = 10

    await _plant_forked_stubs(store, workspace_id, forks=forks)
    assert await _node_count_by_name(store, workspace_id, _ACCOUNT_ARN) == forks

    await _ingest_document(
        store,
        workspace_id=workspace_id,
        source_id=source_id,
        document_id=uuid.uuid4(),
        resource_name="arn:aws:s3:::bucket-delta",
        version=1,
    )

    # One logical edge was asserted.  Pre-fix this produced ``forks`` of them.
    assert await _edge_count(store, workspace_id) == 1


# ---------------------------------------------------------------------------
# The asymmetry: in-batch targets were never affected
# ---------------------------------------------------------------------------


async def test_in_batch_targets_were_never_affected(store: Neo4jGraphStore) -> None:
    """Edges resolved inside the batch use the id-keyed MERGE and were exact.

    This is why the live graph's ``in_organization`` (15) and ``in_ou`` (9)
    matched the offline replay exactly while ``in_account`` was multiplied: an
    organization document defines both endpoints, so ``_edge_to_params``
    resolves the target through ``name_to_id`` and the write MATCHes on
    ``(workspace_id, id)`` — a constrained, single-row key.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()

    org = _Entity(workspace_id, "arn:aws:organizations::000000000000:organization/o-test")
    account = _Entity(workspace_id, "arn:aws:organizations::000000000000:account/o-test/1")
    edge = _NameEdge(account, org.name, "in_organization")

    for version in (1, 2, 3):
        result = await store.upsert_graph(
            source_id=source_id,
            document_id=document_id,
            # Both endpoints present -> resolved via name_to_id, no stub.
            entities=[org, account],
            edges=[edge],
            workspace_id=workspace_id,
            version=version,
        )
        assert result.applied is True

    assert await _edge_count(store, workspace_id) == 1
    rows = await _read(
        store,
        "MATCH (n:Entity {workspace_id: $workspace_id}) "
        "WHERE coalesce(n.is_stub, false) = true RETURN count(n) AS total",
        {"workspace_id": str(workspace_id)},
    )
    assert int(rows[0]["total"]) == 0, "an in-batch target must not create a stub"


# ---------------------------------------------------------------------------
# Bitemporal history must survive the idempotency fix
# ---------------------------------------------------------------------------


async def test_idempotency_does_not_destroy_entity_history(store: Neo4jGraphStore) -> None:
    """A genuine change must still version; only no-op replays are inert.

    The fix must not be mistaken for "collapse everything": ADR-0008's
    ``:EntityState`` chain is the axis the store exists to protect.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    resource = "arn:aws:rds:eu-central-1:000000000000:db/prod"

    entity = _Entity(workspace_id, resource)
    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[entity],
        edges=[_NameEdge(entity, _ACCOUNT_ARN, "in_account")],
        workspace_id=workspace_id,
        version=1,
    )

    changed = _Entity(workspace_id, resource)
    changed.metadata = {"arn": resource, "instance_class": "db.r6g.2xlarge"}
    await store.upsert_graph(
        source_id=source_id,
        document_id=document_id,
        entities=[changed],
        edges=[_NameEdge(changed, _ACCOUNT_ARN, "in_account")],
        workspace_id=workspace_id,
        version=2,
    )

    states = await _read(
        store,
        "MATCH (n:Entity {workspace_id: $workspace_id, id: $id})-[:HAD_STATE]->"
        "(s:EntityState) RETURN count(s) AS total",
        {"workspace_id": str(workspace_id), "id": str(entity.id)},
    )
    assert int(states[0]["total"]) == 2, "a real state change must still be versioned"

    # ...while the edge set stayed at one.
    assert await _edge_count(store, workspace_id) == 1


# ---------------------------------------------------------------------------
# Upgrade path: a graph already forked by the pre-fix writer
# ---------------------------------------------------------------------------


async def test_stub_heal_is_opt_in() -> None:
    """A restart must not silently rewrite durable graph data.

    The checkpoint heal is unconditional because ``CREATE CONSTRAINT`` would
    otherwise abort ``connect()``.  Nothing forces this one, and it deletes
    graph data, so the default must leave a forked graph untouched.
    """
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    workspace_id = uuid.uuid4()
    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
        store = Neo4jGraphStore(config=_config(container.get_connection_url()))
        await store.connect()
        try:
            await _plant_forked_stubs(store, workspace_id, forks=4)
            assert await _node_count_by_name(store, workspace_id, _ACCOUNT_ARN) == 4

            await store.connect()  # default config: heal disabled
            assert await _node_count_by_name(store, workspace_id, _ACCOUNT_ARN) == 4
        finally:
            await store.close()


async def test_stub_heal_keeps_stubs_when_a_relationship_type_cannot_be_rendered() -> None:
    """The data-loss-prevention branch: refuse to delete what cannot be moved.

    Relationship type cannot be a Cypher parameter, so the heal renders one
    relocation statement per type and only for types that pass
    ``_EDGE_TYPE_REGEX``.  A type it cannot render is a type it cannot
    relocate — and ``DETACH DELETE`` on the stub would then destroy that edge
    outright.  ``_heal_duplicate_stubs`` returns before the delete in that
    case, which until now nothing exercised: the branch that exists purely to
    prevent data loss was the one branch with no test.

    Neo4j accepts a backtick-quoted type the allowlist rejects, so the
    condition is reachable by any writer that bypasses the adapter.
    """
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    workspace_id = uuid.uuid4()
    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
        store = Neo4jGraphStore(config=_config(container.get_connection_url()))
        await store.connect()
        try:
            callers = await _plant_forked_stubs(store, workspace_id, forks=3, callers=1)
            async with store._driver.session(database=store._config.database) as session:
                await session.run(
                    "MATCH (a:Entity {workspace_id: $workspace_id, name: $caller}) "
                    "MATCH (b:Entity {workspace_id: $workspace_id, name: $target}) "
                    "WITH a, b LIMIT 1 "
                    "CREATE (a)-[:`bad-type` {workspace_id: $workspace_id, "
                    "edge_type: 'bad-type'}]->(b)",
                    {
                        "workspace_id": str(workspace_id),
                        "caller": callers[0],
                        "target": _ACCOUNT_ARN,
                    },
                )
        finally:
            await store.close()

        healing = Neo4jGraphStore(config=_config(container.get_connection_url(), stub_heal=True))
        try:
            await healing.connect()

            # The heal ran, found a type it could not render, and stopped.
            assert await _node_count_by_name(healing, workspace_id, _ACCOUNT_ARN) == 3, (
                "stubs were deleted even though one of their edges could not be relocated"
            )
            surviving = await _read(
                healing,
                "MATCH ()-[r:`bad-type`]->() WHERE r.workspace_id = $workspace_id "
                "RETURN count(r) AS total",
                {"workspace_id": str(workspace_id)},
            )
            assert int(surviving[0]["total"]) == 1, "the unrelocatable edge was destroyed"
        finally:
            await healing.close()


async def test_stub_heal_collapses_duplicates_without_losing_edges() -> None:
    """The opt-in heal converges a forked graph and keeps every distinct edge."""
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    workspace_id = uuid.uuid4()
    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
        store = Neo4jGraphStore(config=_config(container.get_connection_url()))
        await store.connect()
        try:
            callers = await _plant_forked_stubs(store, workspace_id, forks=5, callers=3)
            # 3 callers x 5 stub copies — the live fan-out, in miniature.
            assert await _edge_count(store, workspace_id) == len(callers) * 5
        finally:
            await store.close()

        healing = Neo4jGraphStore(config=_config(container.get_connection_url(), stub_heal=True))
        try:
            await healing.connect()

            assert await _node_count_by_name(healing, workspace_id, _ACCOUNT_ARN) == 1
            # One edge per caller survives — deduplicated, not deleted.
            assert await _edge_count(healing, workspace_id) == len(callers)

            surviving = await _read(
                healing,
                "MATCH (a:Entity {workspace_id: $workspace_id})-[r:depends_on]->"
                "(b:Entity {workspace_id: $workspace_id, name: $name}) "
                "RETURN a.name AS caller, r.metadata AS metadata ORDER BY caller",
                {"workspace_id": str(workspace_id), "name": _ACCOUNT_ARN},
            )
            assert [str(row["caller"]) for row in surviving] == sorted(callers)
            # Relocation preserves relationship properties.
            assert all(row["metadata"] for row in surviving)

            # Idempotent: a second healing bootstrap is a no-op.
            await healing.connect()
            assert await _node_count_by_name(healing, workspace_id, _ACCOUNT_ARN) == 1
            assert await _edge_count(healing, workspace_id) == len(callers)
        finally:
            await healing.close()
