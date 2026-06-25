"""AP2 backfill heal — live Neo4j conformance gate (v10-AP3).

This test reproduces the exact drift→heal end-to-end path confirmed dead in
consilium-v10-review.md §v10-AP3:

    Drift scenario:
      1. Source S upserts entity E at version 5.
         → Source checkpoint: {source_id: S, version: 5}
         → Node E.version: 5
      2. Simulate a lost write by directly setting E.version = 3 via raw Cypher.
         → Source checkpoint still: 5 (unchanged — checkpoint is per-source, not per-entity)
         → Node E.version: 3  (stale — this is the drift)
      3. AP2 reconciler detects drift (node v3 < SoT v5) and re-emits the entity
         at version 5 with is_backfill=True through the consumer path (modelled by
         calling upsert_entity with bypass_source_checkpoint=True).
      4. Before the fix: the store sees existing_checkpoint_version(5) >= incoming(5)
         → should_skip=True → returns without writing → node stays stale at v3.
         After the fix: bypass_source_checkpoint=True clears should_skip → CAS runs
         → CAS: incoming(5) > node.version(3) → writes → node healed to v5.

Gate: OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 (set by CI in ci.yml test job).

IMPORTANT: This test MUST FAIL on pre-fix code (where outbox_consumer builds
EntityUpsert without bypass_source_checkpoint) and PASS after the fix.
"""

from __future__ import annotations

import os
import uuid

import pytest

_CONTRACT = pytest.mark.skipif(
    not os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS"),
    reason="set OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 to run live Neo4j contract tests",
)


@_CONTRACT
@pytest.mark.asyncio
async def test_live_ap2_backfill_heals_stale_node_bypassing_source_checkpoint() -> None:
    """v10-AP3 live conformance: drift→backfill→heal end-to-end against live Neo4j.

    Step 1: Write entity E at v5 (source checkpoint advances to 5, node version=5).
    Step 2: Simulate drift — set E.version=3 via raw Cypher (checkpoint still=5).
    Step 3: Apply backfill upsert (bypass_source_checkpoint=True, version=5).
    Step 4: Assert E.version is healed back to 5 (NOT skipped).

    Pre-fix: FAILS because the consumer builds EntityUpsert with
        bypass_source_checkpoint=False (default), so the store sees
        checkpoint(5) >= incoming(5) → skip → node stays at v3.

    Post-fix: PASSES because bypass_source_checkpoint=True clears the skip
        and the CAS (5 > 3) heals the node.

    The test also asserts that a normal non-backfill stale re-delivery (AP1
    invariant) is still rejected:
    Step 5: Write entity F at v7, then attempt to write F at v4 (no backfill).
    Step 6: Assert F.version is still 7 (per-entity CAS monotonic guard holds).
    """
    from omniscience_core.storage.graph import EntityUpsert
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")

    config = Neo4jStoreConfig(
        uri=neo4j_uri,
        username=neo4j_user,
        password=neo4j_pw,
        database="neo4j",
        max_connection_pool_size=5,
        connection_acquisition_timeout_seconds=10.0,
        max_transaction_retry_time_seconds=10.0,
        default_max_depth=3,
        bitemporal_enabled=False,
    )
    store = Neo4jGraphStore(config=config)
    await store.connect()

    ws_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    source_id = uuid.uuid4()

    # AP1 regression guard — separate entity
    entity_id_f = uuid.uuid4()
    source_id_f = uuid.uuid4()

    try:
        # ------------------------------------------------------------------
        # Step 1: Write entity E at version 5.
        # Source checkpoint advances to 5; node E.version=5.
        # ------------------------------------------------------------------
        e5 = EntityUpsert(
            id=entity_id,
            source_id=source_id,
            entity_type="service",
            name="svc.ap2-backfill-heal",
            display_name="svc.ap2-backfill-heal",
            chunk_id=None,
            metadata={},
            version=5,
        )
        await store.upsert_entity(entity=e5, workspace_id=ws_id)

        # Verify initial state: node version == 5
        versions = await store.get_entity_versions(workspace_id=ws_id)
        assert entity_id in versions, f"Entity {entity_id} not found after initial v5 write"
        assert versions[entity_id] == 5, f"Expected initial version=5, got {versions[entity_id]}"

        # ------------------------------------------------------------------
        # Step 2: Simulate drift — directly set node E.version back to 3 via
        # raw Cypher (emulating a lost write or partial failure).
        # The source checkpoint remains at 5.
        # ------------------------------------------------------------------
        async with store._driver.session(database=config.database) as session:
            await session.run(
                "MATCH (n:Entity {workspace_id: $ws, id: $eid}) SET n.version = 3",
                {"ws": str(ws_id), "eid": str(entity_id)},
            )

        # Confirm drift is in place
        versions_after_drift = await store.get_entity_versions(workspace_id=ws_id)
        assert versions_after_drift.get(entity_id) == 3, (
            f"Drift setup failed: expected node version=3 after raw Cypher, "
            f"got {versions_after_drift.get(entity_id)}"
        )

        # ------------------------------------------------------------------
        # Step 3: Apply backfill EntityUpsert (bypass_source_checkpoint=True,
        # version=5) — this models what the outbox consumer now builds when
        # event.is_backfill=True (post v10-AP3 fix).
        # ------------------------------------------------------------------
        e5_backfill = EntityUpsert(
            id=entity_id,
            source_id=source_id,
            entity_type="service",
            name="svc.ap2-backfill-heal",
            display_name="svc.ap2-backfill-heal",
            chunk_id=None,
            metadata={},
            version=5,
            bypass_source_checkpoint=True,  # v10-AP3 fix
        )
        await store.upsert_entity(entity=e5_backfill, workspace_id=ws_id)

        # ------------------------------------------------------------------
        # Step 4: Assert E.version is healed back to 5.
        # Pre-fix: node stays at 3 (skip returned without writing).
        # Post-fix: CAS (5 > 3) fires and writes v5.
        # ------------------------------------------------------------------
        versions_healed = await store.get_entity_versions(workspace_id=ws_id)
        assert entity_id in versions_healed, (
            f"AP2-backfill-heal: entity {entity_id} missing after backfill"
        )
        assert versions_healed[entity_id] == 5, (
            f"AP2-backfill-heal FAILED: node.version={versions_healed[entity_id]} "
            f"after backfill, expected 5.  "
            f"The source-checkpoint skip was NOT bypassed — the heal is a no-op.  "
            f"Check outbox_consumer.py EntityUpsert(bypass_source_checkpoint=event.is_backfill)."
        )

        # ------------------------------------------------------------------
        # Step 5 + 6: AP1 regression guard — normal stale re-delivery must
        # still be rejected by the per-entity CAS monotonic guard.
        # ------------------------------------------------------------------
        # Write entity F at v7
        f7 = EntityUpsert(
            id=entity_id_f,
            source_id=source_id_f,
            entity_type="service",
            name="svc.ap1-regression-guard",
            display_name="svc.ap1-regression-guard",
            chunk_id=None,
            metadata={},
            version=7,
        )
        await store.upsert_entity(entity=f7, workspace_id=ws_id)

        # Attempt stale write at v4 — no bypass, no forced_replay
        f4 = EntityUpsert(
            id=entity_id_f,
            source_id=source_id_f,
            entity_type="service",
            name="svc.ap1-regression-guard",
            display_name="svc.ap1-regression-guard",
            chunk_id=None,
            metadata={},
            version=4,
            bypass_source_checkpoint=False,
        )
        await store.upsert_entity(entity=f4, workspace_id=ws_id)

        versions_ap1 = await store.get_entity_versions(workspace_id=ws_id)
        assert versions_ap1.get(entity_id_f) == 7, (
            f"AP1 regression: stale write at v4 should NOT regress entity F from v7, "
            f"got {versions_ap1.get(entity_id_f)}.  Per-entity CAS guard broken."
        )

    finally:
        await store.close()


@_CONTRACT
@pytest.mark.asyncio
async def test_live_ap2_backfill_does_not_regress_node_ahead_of_backfill() -> None:
    """v10-AP3 safety: bypass_source_checkpoint must NOT regress a node already ahead.

    Scenario: node is at v7 (already correctly healed or advanced by a concurrent
    write).  Backfill arrives at v5 with bypass_source_checkpoint=True.
    The per-entity CAS (5 > 7 → False) must block the write.
    Node must remain at v7.

    This validates that bypass_source_checkpoint is safer than forced_replay:
    it bypasses only the coarse source-checkpoint skip, not the fine-grained
    per-entity CAS monotonic guard.
    """
    from omniscience_core.storage.graph import EntityUpsert
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")

    config = Neo4jStoreConfig(
        uri=neo4j_uri,
        username=neo4j_user,
        password=neo4j_pw,
        database="neo4j",
        max_connection_pool_size=5,
        connection_acquisition_timeout_seconds=10.0,
        max_transaction_retry_time_seconds=10.0,
        default_max_depth=3,
        bitemporal_enabled=False,
    )
    store = Neo4jGraphStore(config=config)
    await store.connect()

    ws_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    source_id = uuid.uuid4()

    try:
        # Write entity at v7 (current state, ahead of any backfill)
        e7 = EntityUpsert(
            id=entity_id,
            source_id=source_id,
            entity_type="service",
            name="svc.ap2-no-regress",
            display_name="svc.ap2-no-regress",
            chunk_id=None,
            metadata={},
            version=7,
        )
        await store.upsert_entity(entity=e7, workspace_id=ws_id)

        # Backfill at v5 with bypass_source_checkpoint — should NOT write
        e5_backfill = EntityUpsert(
            id=entity_id,
            source_id=source_id,
            entity_type="service",
            name="svc.ap2-no-regress",
            display_name="svc.ap2-no-regress",
            chunk_id=None,
            metadata={},
            version=5,
            bypass_source_checkpoint=True,
        )
        await store.upsert_entity(entity=e5_backfill, workspace_id=ws_id)

        versions = await store.get_entity_versions(workspace_id=ws_id)
        assert versions.get(entity_id) == 7, (
            f"AP2 safety violation: bypass_source_checkpoint=True at v5 regressed "
            f"node from v7 to {versions.get(entity_id)}.  "
            f"The per-entity CAS guard is not protecting against stale backfills."
        )

    finally:
        await store.close()
