"""Live-Neo4j gate: stub resolution must preserve the relationship it moves.

The defect this module gates
----------------------------

``_RESOLVE_STUBS_CYPHER`` relocated every relationship incident to a stub with
a hard-coded type::

    CREATE (caller)-[r2:calls {workspace_id: $workspace_id}]->(real)
    SET r2 = properties(r), r2.edge_type = 'calls'
    DELETE r

That was written when the only by-name edges in the graph were ``calls`` edges
between code symbols.  The infrastructure extractor emits ``depends_on``,
``in_vpc``, ``in_ou``, ``in_organization`` and ``in_account`` edges to targets
that a *later* document defines, which is precisely the shape that produces a
stub.  Normal crawl order — instance document first, VPC document second —
therefore ran every one of those edges through this statement and silently
relabelled it ``calls``.

``EntityLinker.link_entities`` calls ``resolve_stubs`` unconditionally, and the
pipeline's ``_stage_link`` swallows failures, so nothing surfaced.

Two secondary defects are gated here as well:

* the relocation subquery matched only ``(caller)-[r]->(stub)``, so a stub's
  **outgoing** edges were destroyed by the ``DETACH DELETE`` that followed;
* ``MATCH (real {name: stub.name})`` binds every node carrying that name.
  ``name`` is indexed, not unique, so two real entities sharing a name
  re-created the very fan-out the by-name edge fix removed.

Gate: runs whenever Docker + testcontainers are available; no opt-in env var,
matching ``test_neo4j_multi_document_source.py``.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("testcontainers.neo4j")

from omniscience_index.stores.neo4j.mappers import _stub_entity_id
from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

pytestmark = pytest.mark.asyncio

_NEO4J_PASSWORD = "stub_resolution_password"

#: A VPC id — the canonical "named now, defined by a later document" target.
_VPC_NAME = "vpc-0a1b2c3d4e5f60718"


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


@pytest.fixture(scope="module")
def neo4j_container() -> Any:
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
        yield container


@pytest.fixture
async def store(neo4j_container: Any) -> AsyncIterator[Neo4jGraphStore]:
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
        self.id = uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}|{name}")
        self.workspace_id = workspace_id
        self.entity_type = "aws_live"
        self.name = name
        self.display_name = name
        self.chunk_id: uuid.UUID | None = None
        self.metadata: dict[str, Any] = {"arn": name}


class _NameEdge:
    """Duck-typed edge whose target is named, not resolved."""

    def __init__(self, source: _Entity, target_name: str, edge_type: str) -> None:
        self.source_entity_id = source.id
        self.target_entity_id: uuid.UUID | None = None
        self.target_name = target_name
        self.edge_type = edge_type
        self.metadata: dict[str, Any] = {"relation": edge_type, "asserted_by": source.name}


async def _read(store: Neo4jGraphStore, cypher: str, params: dict[str, Any]) -> list[Any]:
    async with store._driver.session(database=store._config.database) as session:
        result = await session.run(cypher, params)
        return [record async for record in result]


async def _write(store: Neo4jGraphStore, cypher: str, params: dict[str, Any]) -> None:
    async with store._driver.session(database=store._config.database) as session:
        await (await session.run(cypher, params)).consume()


async def _edges(store: Neo4jGraphStore, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every workspace relationship as (source, type, target, edge_type, metadata)."""
    rows = await _read(
        store,
        "MATCH (a:Entity {workspace_id: $workspace_id})-[r]->"
        "(b:Entity {workspace_id: $workspace_id}) "
        "WHERE type(r) <> 'HAD_STATE' "
        "RETURN a.name AS source, type(r) AS rel_type, b.name AS target, "
        "       r.edge_type AS edge_type, r.metadata AS metadata "
        "ORDER BY source, rel_type, target",
        {"workspace_id": str(workspace_id)},
    )
    return [dict(row) for row in rows]


async def _rel_type_counts(store: Neo4jGraphStore, workspace_id: uuid.UUID) -> Counter[str]:
    return Counter(str(row["rel_type"]) for row in await _edges(store, workspace_id))


async def _stub_count(store: Neo4jGraphStore, workspace_id: uuid.UUID) -> int:
    rows = await _read(
        store,
        "MATCH (n:Entity {workspace_id: $workspace_id}) "
        "WHERE coalesce(n.is_stub, false) = true RETURN count(n) AS total",
        {"workspace_id": str(workspace_id)},
    )
    return int(rows[0]["total"])


async def _plant_entity(
    store: Neo4jGraphStore,
    workspace_id: uuid.UUID,
    *,
    name: str,
    is_stub: bool,
    entity_id: uuid.UUID | None = None,
) -> uuid.UUID:
    node_id = entity_id or uuid.uuid4()
    await _write(
        store,
        "CREATE (n:Entity {workspace_id: $workspace_id, id: $id, name: $name, "
        "kind: 'aws_live', is_stub: $is_stub, created_at: $now})",
        {
            "workspace_id": str(workspace_id),
            "id": str(node_id),
            "name": name,
            "is_stub": is_stub,
            "now": "2026-08-15T00:00:00+00:00",
        },
    )
    return node_id


async def _plant_edge(
    store: Neo4jGraphStore,
    workspace_id: uuid.UUID,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    rel_type: str,
) -> None:
    await _write(
        store,
        "MATCH (a:Entity {workspace_id: $workspace_id, id: $source}) "
        "MATCH (b:Entity {workspace_id: $workspace_id, id: $target}) "
        f"CREATE (a)-[:`{rel_type}` {{workspace_id: $workspace_id, edge_type: $rel_type, "
        "metadata: $metadata}]->(b)",
        {
            "workspace_id": str(workspace_id),
            "source": str(source_id),
            "target": str(target_id),
            "rel_type": rel_type,
            "metadata": '{"relation":"planted"}',
        },
    )


# ---------------------------------------------------------------------------
# The bar: the relationship type must survive stub resolution
# ---------------------------------------------------------------------------


async def test_resolve_stubs_preserves_the_edge_type_of_a_relocated_edge(
    store: Neo4jGraphStore,
) -> None:
    """A ``depends_on`` edge to a stub must still be ``depends_on`` afterwards.

    This is the ordinary crawl order for the AWS extractor: the instance
    document names its VPC before the VPC document has been ingested, so the
    edge lands on a stub; the VPC document arrives next and turns that name
    into a real entity; ``EntityLinker.link_entities`` then runs
    ``resolve_stubs`` on every subsequent document.
    """
    workspace_id = uuid.uuid4()
    source_id = uuid.uuid4()

    instance = _Entity(workspace_id, "i-0abc123def4567890")
    await store.upsert_graph(
        source_id=source_id,
        document_id=uuid.uuid4(),
        entities=[instance],
        edges=[_NameEdge(instance, _VPC_NAME, "depends_on")],
        workspace_id=workspace_id,
        version=1,
    )
    # The stub exists and is the deterministic node.
    assert await _stub_count(store, workspace_id) == 1

    # The VPC document arrives — the name is now held by a real entity.
    vpc = _Entity(workspace_id, _VPC_NAME)
    await store.upsert_graph(
        source_id=source_id,
        document_id=uuid.uuid4(),
        entities=[vpc],
        edges=[],
        workspace_id=workspace_id,
        version=1,
    )

    resolved = await store.resolve_pending_stubs(workspace_id=workspace_id)
    assert resolved == 1

    edges = await _edges(store, workspace_id)
    assert len(edges) == 1, edges
    assert edges[0]["rel_type"] == "depends_on", (
        f"resolve_stubs retyped the relationship: {edges[0]['rel_type']}"
    )
    assert edges[0]["edge_type"] == "depends_on"
    assert edges[0]["source"] == instance.name
    assert edges[0]["target"] == _VPC_NAME
    # Properties survive the relocation.
    assert "asserted_by" in str(edges[0]["metadata"])
    # ...and the edge now points at the real entity, not a leftover stub.
    assert await _stub_count(store, workspace_id) == 0
    assert (await _rel_type_counts(store, workspace_id))["calls"] == 0


async def test_resolve_stubs_preserves_every_infrastructure_edge_type(
    store: Neo4jGraphStore,
) -> None:
    """All five infra edge types survive, not just the one the fix was written for."""
    workspace_id = uuid.uuid4()
    edge_types = ["depends_on", "in_vpc", "in_ou", "in_organization", "in_account"]

    for edge_type in edge_types:
        target_name = f"target-for-{edge_type}"
        caller = await _plant_entity(
            store, workspace_id, name=f"caller-{edge_type}", is_stub=False
        )
        stub = await _plant_entity(
            store,
            workspace_id,
            name=target_name,
            is_stub=True,
            entity_id=_stub_entity_id(workspace_id, target_name),
        )
        await _plant_edge(
            store, workspace_id, source_id=caller, target_id=stub, rel_type=edge_type
        )
        await _plant_entity(store, workspace_id, name=target_name, is_stub=False)

    assert await store.resolve_pending_stubs(workspace_id=workspace_id) == len(edge_types)

    counts = await _rel_type_counts(store, workspace_id)
    assert counts == Counter(dict.fromkeys(edge_types, 1)), counts
    assert await _stub_count(store, workspace_id) == 0


# ---------------------------------------------------------------------------
# Secondary defect 1 — the stub's outgoing edges were destroyed
# ---------------------------------------------------------------------------


async def test_resolve_stubs_relocates_the_stubs_outgoing_edges(
    store: Neo4jGraphStore,
) -> None:
    """``DETACH DELETE stub`` destroyed anything the stub pointed at."""
    workspace_id = uuid.uuid4()

    stub = await _plant_entity(
        store,
        workspace_id,
        name=_VPC_NAME,
        is_stub=True,
        entity_id=_stub_entity_id(workspace_id, _VPC_NAME),
    )
    downstream = await _plant_entity(store, workspace_id, name="subnet-0f1e2d3c", is_stub=False)
    await _plant_edge(
        store, workspace_id, source_id=stub, target_id=downstream, rel_type="depends_on"
    )
    real = await _plant_entity(store, workspace_id, name=_VPC_NAME, is_stub=False)

    assert await store.resolve_pending_stubs(workspace_id=workspace_id) == 1

    edges = await _edges(store, workspace_id)
    assert len(edges) == 1, f"the stub's outgoing edge was lost: {edges}"
    assert edges[0]["rel_type"] == "depends_on"
    assert edges[0]["target"] == "subnet-0f1e2d3c"

    rows = await _read(
        store,
        "MATCH (a:Entity {workspace_id: $workspace_id, id: $real})-[]->(b) RETURN count(b) AS n",
        {"workspace_id": str(workspace_id), "real": str(real)},
    )
    assert int(rows[0]["n"]) >= 1, "the relocated edge must start at the real entity"


# ---------------------------------------------------------------------------
# Secondary defect 2 — several reals sharing a name re-created the fan-out
# ---------------------------------------------------------------------------


async def test_resolve_stubs_does_not_fan_out_when_a_name_is_held_by_two_reals(
    store: Neo4jGraphStore,
) -> None:
    """``name`` is indexed, not unique — the match must be reduced to one node."""
    workspace_id = uuid.uuid4()

    caller = await _plant_entity(store, workspace_id, name="i-0deadbeef", is_stub=False)
    stub = await _plant_entity(
        store,
        workspace_id,
        name=_VPC_NAME,
        is_stub=True,
        entity_id=_stub_entity_id(workspace_id, _VPC_NAME),
    )
    await _plant_edge(store, workspace_id, source_id=caller, target_id=stub, rel_type="depends_on")
    # Two real entities legitimately share a name across sources.
    await _plant_entity(store, workspace_id, name=_VPC_NAME, is_stub=False)
    await _plant_entity(store, workspace_id, name=_VPC_NAME, is_stub=False)

    await store.resolve_pending_stubs(workspace_id=workspace_id)

    edges = await _edges(store, workspace_id)
    assert len(edges) == 1, f"one asserted edge fanned out into {len(edges)}"
    assert edges[0]["rel_type"] == "depends_on"


async def test_resolve_stubs_is_idempotent(store: Neo4jGraphStore) -> None:
    """A second pass finds nothing and changes nothing."""
    workspace_id = uuid.uuid4()

    caller = await _plant_entity(store, workspace_id, name="i-0idem", is_stub=False)
    stub = await _plant_entity(
        store,
        workspace_id,
        name=_VPC_NAME,
        is_stub=True,
        entity_id=_stub_entity_id(workspace_id, _VPC_NAME),
    )
    await _plant_edge(store, workspace_id, source_id=caller, target_id=stub, rel_type="in_vpc")
    await _plant_entity(store, workspace_id, name=_VPC_NAME, is_stub=False)

    assert await store.resolve_pending_stubs(workspace_id=workspace_id) == 1
    before = await _edges(store, workspace_id)
    assert await store.resolve_pending_stubs(workspace_id=workspace_id) == 0
    assert await _edges(store, workspace_id) == before


# ---------------------------------------------------------------------------
# Fail-closed: an unrenderable relationship type must not cost data
# ---------------------------------------------------------------------------


async def test_resolve_stubs_keeps_stubs_when_an_edge_type_fails_validation(
    store: Neo4jGraphStore,
) -> None:
    """Relationship type cannot be parameterised, so it is rendered — under a gate.

    A type the allowlist regex rejects cannot be relocated.  Deleting the stub
    anyway would destroy that edge, so the pass must stop and leave the graph
    exactly as it found it.
    """
    workspace_id = uuid.uuid4()

    caller = await _plant_entity(store, workspace_id, name="i-0badtype", is_stub=False)
    stub = await _plant_entity(
        store,
        workspace_id,
        name=_VPC_NAME,
        is_stub=True,
        entity_id=_stub_entity_id(workspace_id, _VPC_NAME),
    )
    # Backtick-quoted: Neo4j accepts a type the allowlist regex must reject.
    await _plant_edge(store, workspace_id, source_id=caller, target_id=stub, rel_type="bad-type")
    await _plant_entity(store, workspace_id, name=_VPC_NAME, is_stub=False)

    assert await store.resolve_pending_stubs(workspace_id=workspace_id) == 0
    assert await _stub_count(store, workspace_id) == 1, "the stub was deleted with its edge"
    counts = await _rel_type_counts(store, workspace_id)
    assert counts["bad-type"] == 1, "the unrelocatable edge was destroyed"
