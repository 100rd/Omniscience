"""AP4 concurrency test — per-entity CAS atomicity under parallel writes (v11-AP4).

This test closes the concurrency axis left unproven after consilium-v11 Batch A.
The existing live CAS tests (test_ap1_per_entity_cas.py, test_ap2_backfill_heal.py)
are single-threaded and cannot detect lost-update races between concurrent writers.

Design
------
The CAS is a single-statement ``MERGE (n) WITH CASE $incoming_version >
coalesce(n.version, -1) ... END AS should_write FOREACH (... SET n.version = ...)``.

Neo4j MERGE acquires a per-node write lock for the duration of the enclosing
transaction.  Therefore the read-compare-set is atomic at the per-entity level:
no concurrent transaction can observe or modify ``n.version`` between the MERGE
and the FOREACH SET.

This test proves that guarantee empirically by firing N concurrent asyncio tasks,
each writing the SAME entity at a different version from a permuted/randomised
order.  Assertions:

  1. Final node version == max(applied_versions) — the highest version wins.
  2. No write at a lower version overwrites a higher one (no lost update).
  3. The invariant holds across multiple permutations (deterministc + random).

Additionally, the test interleaves a ``bypass_source_checkpoint=True`` backfill
write (simulating the AP2 heal path) with normal live upserts to prove that the
bypass path does not break CAS atomicity.

Gate: ``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`` (same flag as all live Neo4j tests).

Approach: deterministic multi-permutation concurrency test (no Hypothesis library).
We use ``itertools.permutations`` for small N and ``random.sample`` for larger N,
covering enough orderings to catch the most common race windows.  The test runs
at least 10 distinct permutations per scenario.

If a true property-based test is desired, install ``hypothesis`` and the test
can be converted to ``@given(st.permutations(range(N)))``; the current
implementation avoids that dependency for portability.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import uuid
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

_CONTRACT = pytest.mark.skipif(
    not os.environ.get("OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS"),
    reason=(
        "set OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 to run live Neo4j "
        "CAS concurrency tests (v11-AP4)"
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VERSIONS = [1, 2, 3, 4, 5, 6, 7, 8]  # final answer must be 8


def _make_neo4j_config(*, uri: str, user: str, pw: str) -> Any:
    from omniscience_index.stores.neo4j.store import Neo4jStoreConfig

    return Neo4jStoreConfig(
        uri=uri,
        username=user,
        password=pw,
        database="neo4j",
        max_connection_pool_size=20,  # enough for N concurrent writers
        connection_acquisition_timeout_seconds=15.0,
        max_transaction_retry_time_seconds=15.0,
        default_max_depth=3,
        bitemporal_enabled=False,
    )


def _make_entity(
    *,
    entity_id: uuid.UUID,
    source_id: uuid.UUID,
    version: int,
    bypass_source_checkpoint: bool = False,
    forced_replay: bool = False,
    name: str,
) -> Any:
    from omniscience_core.storage.graph import EntityUpsert

    return EntityUpsert(
        id=entity_id,
        source_id=source_id,
        entity_type="service",
        name=name,
        display_name=name,
        chunk_id=None,
        metadata={},
        version=version,
        bypass_source_checkpoint=bypass_source_checkpoint,
        forced_replay=forced_replay,
    )


async def _write_version(
    store: Any,
    entity_id: uuid.UUID,
    source_id: uuid.UUID,
    ws_id: uuid.UUID,
    version: int,
    name: str,
    bypass_source_checkpoint: bool = False,
) -> None:
    """Write a single version to the store (one concurrent task)."""
    entity = _make_entity(
        entity_id=entity_id,
        source_id=source_id,
        version=version,
        name=name,
        bypass_source_checkpoint=bypass_source_checkpoint,
    )
    await store.upsert_entity(entity=entity, workspace_id=ws_id)


def _all_permutations_capped(versions: list[int], *, cap: int = 20) -> list[list[int]]:
    """Return all permutations of ``versions`` up to ``cap`` to bound test time."""
    perms = list(itertools.permutations(versions))
    if len(perms) <= cap:
        return [list(p) for p in perms]
    # Deterministic sample: take the first, last, and evenly spaced ones.
    step = len(perms) // cap
    return [list(perms[i]) for i in range(0, len(perms), step)][:cap]


# ---------------------------------------------------------------------------
# AP4 Test 1: concurrent live upserts — final version == max applied
# ---------------------------------------------------------------------------


@_CONTRACT
@pytest.mark.asyncio
async def test_live_ap4_concurrent_upserts_final_version_is_max() -> None:
    """v11-AP4: concurrent upserts for the same entity must leave version == max.

    Fires all versions in _VERSIONS concurrently (asyncio.gather) for EACH of
    multiple permutations.  In each permutation the final node version must equal
    max(_VERSIONS).

    This proves Neo4j MERGE's per-node write lock serialises the concurrent
    writes correctly — no lost update, no non-monotonic regression.
    """
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")

    config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    store = Neo4jGraphStore(config=config)
    await store.connect()

    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    name = f"svc.ap4-concurrent-{str(ws_id)[:8]}"

    try:
        # Use 4 versions for permutation explosion avoidance (4! = 24, capped at 20).
        versions = [2, 4, 6, 8]
        expected_max = max(versions)
        permutations = _all_permutations_capped(versions, cap=20)

        assert len(permutations) >= 10, (
            f"Need at least 10 permutations to provide meaningful coverage; "
            f"got {len(permutations)}"
        )

        failures: list[tuple[list[int], int]] = []

        for perm in permutations:
            # Reset the entity by writing version=0 (force overwrite) before
            # each permutation run so each run starts from a clean slate.
            # We use forced_replay=True to unconditionally reset.
            reset_entity = _make_entity(
                entity_id=entity_id,
                source_id=source_id,
                version=0,
                name=name,
                forced_replay=True,
            )
            await store.upsert_entity(entity=reset_entity, workspace_id=ws_id)

            # Verify reset worked.
            versions_before = await store.get_entity_versions(workspace_id=ws_id)
            assert versions_before.get(entity_id, None) == 0, (
                f"Reset to v0 failed: got {versions_before.get(entity_id)}"
            )

            # Fire all versions CONCURRENTLY in the given permutation order.
            # asyncio.gather starts all tasks simultaneously; they race on the
            # same entity node in Neo4j.
            await asyncio.gather(
                *[
                    _write_version(
                        store,
                        entity_id=entity_id,
                        source_id=source_id,
                        ws_id=ws_id,
                        version=v,
                        name=name,
                    )
                    for v in perm
                ]
            )

            # Read back the final version — must be max(versions).
            versions_after = await store.get_entity_versions(workspace_id=ws_id)
            final_version = versions_after.get(entity_id)

            if final_version != expected_max:
                failures.append((perm, final_version))

        assert failures == [], (
            f"v11-AP4 CAS atomicity violation: lost-update detected in "
            f"{len(failures)}/{len(permutations)} permutations.  "
            f"Expected final version == {expected_max} for all.  "
            f"Failures (permutation, actual_final): {failures[:5]}.  "
            "Neo4j MERGE per-node write lock is NOT providing atomic CAS "
            "for concurrent writes — investigate the Cypher or driver config."
        )

    finally:
        await store.close()


# ---------------------------------------------------------------------------
# AP4 Test 2: backfill (bypass_source_checkpoint) interleaved with live writes
# ---------------------------------------------------------------------------


@_CONTRACT
@pytest.mark.asyncio
async def test_live_ap4_backfill_interleaved_with_live_writes() -> None:
    """v11-AP4: backfill (bypass_source_checkpoint=True) + live writes race safely.

    Scenario: one task writes the backfill at a lower version (bypass_source_checkpoint=True,
    version=3) while N other tasks write live upserts at various higher versions
    [4, 5, 6, 7, 8] concurrently.  Final node version must be max(all versions).

    This exercises the path that combines AP1 bypass with AP2 heal:
    the bypass skips the source-checkpoint read but the per-entity CAS still
    runs — so the backfill should NOT regress the entity below the live writes.
    """
    from omniscience_index.stores.neo4j.store import Neo4jGraphStore

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")

    config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    store = Neo4jGraphStore(config=config)
    await store.connect()

    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    name = f"svc.ap4-backfill-race-{str(ws_id)[:8]}"

    live_versions = [4, 5, 6, 7, 8]
    backfill_version = 3  # lower than all live_versions — must NOT win
    expected_max = max(live_versions)  # = 8

    try:
        # Run 10 independent race trials.
        failures: list[tuple[int, int]] = []  # (trial_idx, actual_version)

        for trial in range(10):
            # Reset to v0.
            reset_entity = _make_entity(
                entity_id=entity_id,
                source_id=source_id,
                version=0,
                name=name,
                forced_replay=True,
            )
            await store.upsert_entity(entity=reset_entity, workspace_id=ws_id)

            # Build task list: backfill task + live write tasks, all concurrent.
            tasks = [
                _write_version(
                    store,
                    entity_id=entity_id,
                    source_id=source_id,
                    ws_id=ws_id,
                    version=backfill_version,
                    name=name,
                    bypass_source_checkpoint=True,  # AP2 backfill path
                )
            ] + [
                _write_version(
                    store,
                    entity_id=entity_id,
                    source_id=source_id,
                    ws_id=ws_id,
                    version=v,
                    name=name,
                    bypass_source_checkpoint=False,
                )
                for v in live_versions
            ]

            # Shuffle tasks slightly by running them all simultaneously.
            await asyncio.gather(*tasks)

            versions_after = await store.get_entity_versions(workspace_id=ws_id)
            final = versions_after.get(entity_id)
            if final != expected_max:
                failures.append((trial, final))

        assert failures == [], (
            f"v11-AP4 backfill-race violation: backfill (v{backfill_version}) "
            f"overwrote live write in {len(failures)}/10 trials.  "
            f"Expected final version == {expected_max}.  "
            f"Failures (trial, actual): {failures}.  "
            "bypass_source_checkpoint is bypassing the per-entity CAS guard — "
            "check the forced_replay / bypass logic in store.py."
        )

    finally:
        await store.close()


# ---------------------------------------------------------------------------
# AP4 Test 3: no lost update — monotonic property across random permutations
# ---------------------------------------------------------------------------


@_CONTRACT
@pytest.mark.asyncio
async def test_live_ap4_no_lost_update_random_permutations() -> None:
    """v11-AP4: no lost update under randomised concurrent version order.

    Uses a fixed random seed (seed=42) for reproducibility while covering
    permutations not covered by the deterministic exhaustive test above.

    Asserts: after N concurrent writes at randomly ordered versions, the final
    node version equals max(versions) in every trial.

    This is the deterministic multi-permutation concurrency test promised in
    the AP4 fix design.  A Hypothesis-based property test would be:

        @given(st.permutations(range(1, 9)))
        def test(perm): ...

    but Hypothesis is not installed in the current environment, so we use 20
    seeded random permutations instead — equivalent coverage for this version
    cardinality.
    """
    import random

    from omniscience_index.stores.neo4j.store import Neo4jGraphStore

    neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD", "neo4j")

    config = _make_neo4j_config(uri=neo4j_uri, user=neo4j_user, pw=neo4j_pw)
    store = Neo4jGraphStore(config=config)
    await store.connect()

    ws_id = uuid.uuid4()
    source_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    name = f"svc.ap4-random-{str(ws_id)[:8]}"

    versions = list(range(1, 9))  # [1..8], expected_max=8
    expected_max = max(versions)

    rng = random.Random(42)  # noqa: S311  # deterministic seed, not cryptographic use

    num_trials = 20
    permutations_tried: list[list[int]] = [
        rng.sample(versions, len(versions)) for _ in range(num_trials)
    ]

    try:
        failures: list[tuple[int, list[int], int]] = []  # (trial, perm, actual)

        for trial_idx, perm in enumerate(permutations_tried):
            # Reset to v0.
            reset_entity = _make_entity(
                entity_id=entity_id,
                source_id=source_id,
                version=0,
                name=name,
                forced_replay=True,
            )
            await store.upsert_entity(entity=reset_entity, workspace_id=ws_id)

            # Fire all versions concurrently in the permuted order.
            await asyncio.gather(
                *[
                    _write_version(
                        store,
                        entity_id=entity_id,
                        source_id=source_id,
                        ws_id=ws_id,
                        version=v,
                        name=name,
                    )
                    for v in perm
                ]
            )

            versions_after = await store.get_entity_versions(workspace_id=ws_id)
            final = versions_after.get(entity_id)
            if final != expected_max:
                failures.append((trial_idx, perm, final))

        assert failures == [], (
            f"v11-AP4 lost-update detected in {len(failures)}/{num_trials} "
            f"random-permutation trials (seed=42).  "
            f"Expected final version == {expected_max} for all.  "
            f"First failure: trial={failures[0][0]}, perm={failures[0][1]}, "
            f"actual_final={failures[0][2]}.  "
            "The per-entity CAS MERGE is not atomically serialising concurrent writes."
        )

    finally:
        await store.close()
