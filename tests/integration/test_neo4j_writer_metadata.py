"""End-to-end regression test for :class:`Neo4jGraphStore` metadata writes (issue #226).

Background
----------

After PRs #222 and #227 landed, the writer path against a real Neo4j 5.x
container failed on every ``upsert_entity`` / ``upsert_edge`` call::

    neo4j.exceptions.CypherTypeError:
      {neo4j_code: Neo.ClientError.Statement.TypeError}
      {message: Property values can only be of primitive types or arrays
       thereof. Encountered: Map{}.}

The bug was the writer binding ``metadata`` as a Python ``dict``; the
Bolt driver serialised it as a Cypher Map, which Neo4j 5.x rejects as a
property value (even an empty ``Map{}``).  The fix is to JSON-encode
``metadata`` at every write site — see
``packages/index/src/omniscience_index/stores/neo4j_store.py::
_serialise_metadata_param``.

Why this regression slipped through CI
--------------------------------------

The parametric writer suite at ``tests/test_graph_store_contract.py``
exercises the writer end-to-end against a Neo4j testcontainer, but it
was gated behind ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`` which CI
does NOT set.  As of this PR that gate is gone — the writer contract
tests now run unconditionally whenever Docker + ``testcontainers-neo4j``
are present, matching the pattern from
``tests/integration/test_neo4j_bootstrap_schema.py`` (PR #227).

Scope of this test
------------------

This file is the targeted regression fence for issue #226 — it does the
*minimum* round-trip needed to prove the bug is fixed and won't recur:

1. Spin up a fresh ``neo4j:5.19-community`` testcontainer.
2. Call ``Neo4jGraphStore.upsert_entity`` with a non-empty ``metadata``
   dict containing strings, ints, and a nested dict / list.  This is
   the call that raised the ``CypherTypeError`` before the fix.
3. Call ``Neo4jGraphStore.upsert_edge`` between two such entities, also
   with non-empty ``metadata``.
4. Read both properties back via raw Cypher and assert the JSON round-
   trips losslessly through
   :func:`omniscience_index.stores.neo4j_store._deserialise_metadata`.

Gate
----

Runs on every PR — the only skip is "Docker / testcontainers
unavailable", mirroring the post-#227 pattern.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("testcontainers.neo4j")

from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert
from omniscience_index.stores.neo4j_store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
    _deserialise_metadata,
    _run_read_stmt,
)


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker is not reachable; Neo4j contract tests need testcontainers.",
    ),
]


_NEO4J_PASSWORD = "issue_226_password"
_WORKSPACE_ID = uuid.UUID("26000000-0000-0000-0000-000000000226")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def neo4j_store() -> AsyncIterator[Neo4jGraphStore]:
    """Spin up a fresh Neo4j 5.19-community container and connect to it.

    Per-test container — the writer is destructive and we want the
    metadata round-trip to be observed against a *brand-new* property,
    not one carried over from a previous test's MERGE.
    """
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
        config = Neo4jStoreConfig(
            uri=container.get_connection_url(),
            username="neo4j",
            password=_NEO4J_PASSWORD,
            database="neo4j",
            max_connection_pool_size=5,
            connection_acquisition_timeout_seconds=30.0,
            max_transaction_retry_time_seconds=15.0,
            default_max_depth=3,
        )
        store = Neo4jGraphStore(config=config)
        await store.connect()
        try:
            yield store
        finally:
            await store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_entity_metadata(
    store: Neo4jGraphStore,
    *,
    entity_id: uuid.UUID,
) -> dict[str, object]:
    """Read the raw ``metadata`` property off the :Entity identity mirror."""
    cypher = (
        "MATCH (n:Entity {workspace_id: $workspace_id, id: $id}) RETURN n.metadata AS metadata"
    )
    params = {"workspace_id": str(_WORKSPACE_ID), "id": str(entity_id)}
    async with store._driver.session(database=store._config.database) as session:
        rows = await session.execute_read(_run_read_stmt, cypher, params)
    assert rows, f"no entity row found for id={entity_id}"
    raw = rows[0]["metadata"]
    # The persisted shape must be a JSON string after the #226 fix —
    # the property value MUST NOT be a Map (which would have failed at
    # write time anyway).
    assert isinstance(raw, str), (
        f"metadata stored shape is {type(raw).__name__}, expected str — issue #226 fix regressed"
    )
    return _deserialise_metadata(raw)


async def _read_edge_metadata(
    store: Neo4jGraphStore,
    *,
    source_entity_id: uuid.UUID,
    target_entity_id: uuid.UUID,
    edge_type: str,
) -> dict[str, object]:
    """Read the raw ``metadata`` property off the relationship."""
    cypher = (
        "MATCH (a:Entity {workspace_id: $workspace_id, id: $src})"
        f"-[r:`{edge_type}`]->"
        "(b:Entity {workspace_id: $workspace_id, id: $tgt}) "
        "RETURN r.metadata AS metadata"
    )
    params = {
        "workspace_id": str(_WORKSPACE_ID),
        "src": str(source_entity_id),
        "tgt": str(target_entity_id),
    }
    async with store._driver.session(database=store._config.database) as session:
        rows = await session.execute_read(_run_read_stmt, cypher, params)
    assert rows, "no edge row found"
    raw = rows[0]["metadata"]
    assert isinstance(raw, str), (
        f"edge metadata stored shape is {type(raw).__name__}, expected str — "
        "issue #226 fix regressed"
    )
    return _deserialise_metadata(raw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_upsert_entity_with_non_empty_metadata_round_trips(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """The headline regression — non-empty metadata MUST NOT raise.

    Pre-fix this call raised ``CypherTypeError: ... Encountered: Map{}.``
    on every invocation regardless of metadata content.  Post-fix it
    persists as a JSON string and round-trips losslessly.
    """
    entity = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.checkout",
        display_name="Checkout Service",
        chunk_id=None,
        metadata={
            "team": "payments",
            "tier": 1,
            "tags": ["prod", "us-east-1"],
            "owner": {"email": "owner@example.com", "oncall": True},
        },
    )

    await neo4j_store.upsert_entity(entity=entity, workspace_id=_WORKSPACE_ID)

    decoded = await _read_entity_metadata(neo4j_store, entity_id=entity.id)
    assert decoded == {
        "team": "payments",
        "tier": 1,
        "tags": ["prod", "us-east-1"],
        "owner": {"email": "owner@example.com", "oncall": True},
    }


async def test_upsert_entity_with_empty_metadata_persists_empty_dict(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """Empty metadata (the default) persists as ``\"{}\"`` and decodes to ``{}``.

    Pre-fix this STILL raised because the Bolt driver serialised an
    empty Python ``dict`` as ``Map{}`` and Neo4j 5.x rejects Map regardless
    of arity.  Post-fix the property is the string ``\"{}\"``.
    """
    entity = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.bare",
        display_name="Bare Service",
        chunk_id=None,
        metadata={},
    )

    await neo4j_store.upsert_entity(entity=entity, workspace_id=_WORKSPACE_ID)

    decoded = await _read_entity_metadata(neo4j_store, entity_id=entity.id)
    assert decoded == {}


async def test_upsert_edge_with_non_empty_metadata_round_trips(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """Edges carry metadata too — same fix, same round-trip contract."""
    src = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.api",
        display_name="API",
        chunk_id=None,
    )
    tgt = EntityUpsert(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type="service",
        name="svc.db",
        display_name="DB",
        chunk_id=None,
    )
    await neo4j_store.upsert_entity(entity=src, workspace_id=_WORKSPACE_ID)
    await neo4j_store.upsert_entity(entity=tgt, workspace_id=_WORKSPACE_ID)

    edge = EdgeUpsert(
        source_entity_id=src.id,
        target_entity_id=tgt.id,
        edge_type="calls",
        metadata={
            "source_id": "ingestion-run-001",
            "confidence": 0.92,
            "evidence": ["span-1", "span-2"],
        },
    )

    await neo4j_store.upsert_edge(edge=edge, workspace_id=_WORKSPACE_ID)

    decoded = await _read_edge_metadata(
        neo4j_store,
        source_entity_id=src.id,
        target_entity_id=tgt.id,
        edge_type="calls",
    )
    assert decoded == {
        "source_id": "ingestion-run-001",
        "confidence": 0.92,
        "evidence": ["span-1", "span-2"],
    }


async def test_upsert_entity_is_idempotent_on_metadata_change(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """A second upsert with mutated metadata overwrites the stored JSON.

    Guards against an accidental regression where the serialiser is
    only invoked on the ``ON CREATE`` branch and not on ``ON MATCH``.
    """
    entity_id = uuid.uuid4()
    source_id = uuid.uuid4()
    first = EntityUpsert(
        id=entity_id,
        source_id=source_id,
        entity_type="service",
        name="svc.upgrade",
        display_name="Upgrade Service",
        chunk_id=None,
        metadata={"version": "v1"},
    )
    await neo4j_store.upsert_entity(entity=first, workspace_id=_WORKSPACE_ID)

    second = EntityUpsert(
        id=entity_id,
        source_id=source_id,
        entity_type="service",
        name="svc.upgrade",
        display_name="Upgrade Service",
        chunk_id=None,
        metadata={"version": "v2", "rollout": "canary"},
    )
    await neo4j_store.upsert_entity(entity=second, workspace_id=_WORKSPACE_ID)

    decoded = await _read_entity_metadata(neo4j_store, entity_id=entity_id)
    assert decoded == {"version": "v2", "rollout": "canary"}
