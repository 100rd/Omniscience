"""AP2 conformance tests — bounded re-emit, tombstone heal, edge re-emit.

AP2 invariants:
1. Per-tick re-emit is capped at ``REEMIT_TICK_CAP`` entities (no unbounded
   storms).  ``reconcile_worker.py`` must export ``REEMIT_TICK_CAP: int``.
2. Neo4j orphan detection is active (``ENABLE_NEO4J_ORPHAN_REEMIT`` on by
   default or replaced by real re-projection); orphan heal is not detect-only.
3. Edges are re-emitted on drift (``get_edge_versions``/``get_edge_content_hashes``
   on both stores, causal order: entities before edges in backfill).
4. Entity re-emit cursor advances monotonically — a single run does not
   fetch ALL drifted entities at once.

Implementation notes (for the implementer):
- Export ``REEMIT_TICK_CAP: int`` from ``reconcile_worker.py``.
- Cap/cursor ``_reemit_entities_for_missing_sources`` and
  ``_check_entity_drift`` to ``min(N, REEMIT_TICK_CAP)`` per tick.
- Active Neo4j orphan heal: end-date orphans rather than only logging.
- Add edge drift detection and re-emit.
- Causal order: entities-before-edges in the backfill queue.

These tests are STUBS — they encode the intended invariant and are
marked xfail until AP2 is implemented in Batch C.  When the feature
lands: remove xfail, make tests pass.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(reason="implemented in v9 Batch C", strict=False)
def test_reemit_tick_cap_is_exported() -> None:
    """AP2: reconcile_worker exports REEMIT_TICK_CAP as a positive integer."""
    from omniscience_index.reconcile_worker import REEMIT_TICK_CAP

    assert isinstance(REEMIT_TICK_CAP, int) and REEMIT_TICK_CAP > 0
    raise NotImplementedError("AP2 bounded re-emit not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch C", strict=False)
def test_reemit_is_bounded_per_tick() -> None:
    """AP2: a single reconcile tick emits at most REEMIT_TICK_CAP entities.

    Even when N > REEMIT_TICK_CAP entities are drifted, only REEMIT_TICK_CAP
    are emitted per run.  The remainder is deferred to the next tick.
    """
    raise NotImplementedError("AP2 bounded re-emit not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch C", strict=False)
def test_neo4j_orphan_heal_is_active() -> None:
    """AP2: Neo4j orphans are actively end-dated (not just logged).

    An orphan node (entity in Neo4j without a corresponding active
    Postgres source) must have its valid_to set to now (end-dated),
    not merely logged as orphan_detected.
    """
    raise NotImplementedError("AP2 Neo4j tombstone heal not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch C", strict=False)
def test_edge_drift_detected_and_reemitted() -> None:
    """AP2: edge version drift triggers re-emit in causal order (entities first).

    When an edge's version in Neo4j diverges from Postgres SoT,
    the edge is re-emitted.  The re-emit order guarantees entities
    are emitted before their edges.
    """
    raise NotImplementedError("AP2 edge re-emit not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch C", strict=False)
def test_reemit_cursor_advances_monotonically() -> None:
    """AP2: re-emit uses a cursor so a single tick does not fetch all drifted entities.

    After capping at REEMIT_TICK_CAP, the cursor position must advance so
    subsequent ticks continue from where the previous one left off, covering
    all drifted entities eventually without a single unbounded fetch.
    """
    raise NotImplementedError("AP2 cursor-based re-emit not yet implemented")
