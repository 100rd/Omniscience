"""AP1 conformance tests — per-entity conditional-apply (CAS).

AP1 invariant: entity writes in Neo4j must be guarded by a per-entity
CAS predicate (``incoming_version > coalesce(n.version, -1)``), NOT just
a coarser per-source checkpoint.  Out-of-order delivery of v3 then v2
for the same entity must result in the entity staying at v3 (v2 is a
no-op) regardless of source-level checkpoint state.

Implementation: ``neo4j/_cypher.py`` must define
``_UPSERT_ENTITY_CAS_CYPHER`` and ``_UPSERT_ENTITY_BITEMPORAL_CAS_CYPHER``
with a ``CASE WHEN $forced THEN TRUE ELSE $incoming_version >
coalesce(n.version, -1) END AS should_write`` guard and return
``should_write AS applied``.  ``neo4j/store.py::upsert_entity._run``
must use these CAS Cyphers when ``version`` is not None.

These tests are STUBS — they encode the intended invariant and are
marked xfail until AP1 is implemented in Batch B.  When the feature
lands: remove xfail, make tests pass.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Stub tests — xfail until AP1 CAS is implemented (Batch B)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="implemented in v9 Batch B", strict=False)
def test_out_of_order_v3_then_v2_rejected() -> None:
    """AP1: delivering v3 then v2 for the same entity leaves entity at v3.

    Scenario:
      1. Upsert entity E at version=3 → applied=True, entity.version=3.
      2. Upsert entity E at version=2 (stale re-delivery) → applied=False.
      3. entity.version must still be 3 after step 2.

    Requires: CAS Cypher with ``$incoming_version > coalesce(n.version,-1)``.
    """
    # Import here to avoid import errors before implementation exists.
    from omniscience_index.stores.neo4j._cypher import _UPSERT_ENTITY_CAS_CYPHER  # noqa: F401

    raise NotImplementedError("AP1 CAS not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch B", strict=False)
def test_same_version_is_idempotent() -> None:
    """AP1: re-delivering the same version is idempotent (no write, no error).

    Scenario:
      1. Upsert entity E at version=5 → applied=True.
      2. Upsert entity E at version=5 again → applied=False (no-op).
      3. entity state unchanged.
    """
    raise NotImplementedError("AP1 CAS not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch B", strict=False)
def test_monotonic_version_advance_is_applied() -> None:
    """AP1: version advances monotonically — each higher version IS applied.

    Scenario:
      1. Upsert at v1 → applied.
      2. Upsert at v2 → applied.
      3. Upsert at v3 → applied.
      Entity must be at v3 at the end.
    """
    raise NotImplementedError("AP1 CAS not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch B", strict=False)
def test_forced_write_bypasses_cas() -> None:
    """AP1: $forced=True bypasses the CAS guard (used for admin re-projection).

    Even with $incoming_version < current n.version, a forced write is applied.
    """
    raise NotImplementedError("AP1 CAS not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch B", strict=False)
def test_version_none_is_unconditional() -> None:
    """AP1: version=None path is unconditional (legacy/test compatibility).

    When no version is supplied, the write proceeds without a CAS guard.
    This preserves backward compatibility with callers that do not track versions.
    """
    raise NotImplementedError("AP1 CAS not yet implemented")
