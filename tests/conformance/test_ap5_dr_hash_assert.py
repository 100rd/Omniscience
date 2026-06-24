"""AP5 conformance tests — DR-drill rebuild-from-SoT + hash-assert.

AP5 invariants:
1. ``rebuild_all_projections.py --recompute-embeddings`` re-embeds from
   ``chunk.text`` (ignores stored ``chunk.embedding``).  The zero-vector
   bypass in ``seed_dr_drill.py`` must be removed.
2. ``dr_verify.py`` includes hash-equivalence assertion: the vector/content
   digest of rebuilt chunks must match a reference digest derived from SoT text,
   not just count-equality.
3. The DR drill must run on PR (3-doc smoke) AND nightly (representative volume).

Implementation notes (for the implementer):
- Add ``--recompute-embeddings`` flag to ``rebuild_all_projections.py`` that
  ignores ``chunk.embedding`` and recomputes from ``chunk.text`` via the live
  embedding provider.
- Remove the zero-vector pre-load from ``seed_dr_drill.py``.
- Add hash-equivalence assertion to ``dr_verify.py``.
- Add PR-smoke step in conformance.yml AP5 job (3 docs, fast).
- Keep the nightly full-volume drill in ``dr-drill.yml``.

These tests are STUBS — they encode the intended invariant and are
marked xfail until AP5 is implemented in Batch E.  When the feature
lands: remove xfail, make tests pass.

Note: the AP5 conformance job requires a Postgres service (postgres:16-alpine)
for the rebuild path.  See conformance.yml ap5-dr-smoke job definition.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(reason="implemented in v9 Batch E", strict=False)
def test_recompute_embeddings_flag_exists() -> None:
    """AP5: rebuild_all_projections.py supports --recompute-embeddings flag.

    When passed, the rebuild must ignore chunk.embedding and recompute
    from chunk.text via the live embedding provider.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rebuild_all_projections",
        "scripts/rebuild_all_projections.py",
    )
    assert spec is not None
    raise NotImplementedError("AP5 --recompute-embeddings not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch E", strict=False)
def test_seed_dr_drill_does_not_use_zero_vectors() -> None:
    """AP5: seed_dr_drill.py must not pre-load zero-vector embeddings.

    Zero-vector bypass hides the embedding path from the drill.
    The seed script must use real text and the live provider.
    """
    raise NotImplementedError("AP5 zero-vector removal not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch E", strict=False)
def test_dr_verify_includes_hash_equivalence() -> None:
    """AP5: dr_verify.py must assert vector/content hash equivalence.

    Count-equality alone is insufficient — the verify script must assert
    that rebuilt embeddings match a reference digest from SoT text.
    """
    raise NotImplementedError("AP5 hash-assert not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch E", strict=False)
def test_dr_smoke_three_docs_passes() -> None:
    """AP5: 3-doc PR smoke drill completes within the RTO budget.

    The smoke drill (3 source docs) must:
    1. Seed the 3 docs.
    2. Run rebuild-from-SoT (--recompute-embeddings).
    3. Verify hash equivalence.
    4. Complete within 120 seconds.

    Requires: Postgres service (postgres:16-alpine) in the CI job.
    """
    raise NotImplementedError("AP5 PR-smoke drill not yet implemented")
