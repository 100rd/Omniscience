"""AP5 conformance tests — DR-drill rebuild-from-SoT + hash-assert.

AP5 invariants:
1. ``rebuild_all_projections.py --recompute-embeddings`` re-embeds from
   ``chunk.text`` (ignores stored ``chunk.embedding``).  The zero-vector
   bypass in ``seed_dr_drill.py`` has been removed (AP5 Batch D).
2. ``dr_verify.py`` includes hash-equivalence assertion: the vector/content
   digest of rebuilt chunks must match a reference digest derived from SoT text.
3. The DR drill must run on PR (3-doc smoke) AND nightly (representative volume).

Test scope
----------
These tests are mock-only (no containers) except where explicitly marked
with ``pytest.mark.integration``.  The conformance.yml ap5-dr-smoke job
provides the Postgres service for the integration smoke test.

The hash-equivalence test uses deterministic fake embeddings so it is
dependency-free and verifies the dr_verify logic without a real embedding
provider.
"""

from __future__ import annotations

import ast
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Make scripts/ importable
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from dr_verify import (  # noqa: E402  (after sys.path insert)
    ChunkHashRecord,
    compute_text_hash,
    compute_vector_digest,
    verify_hash_equivalence,
)

# ---------------------------------------------------------------------------
# AP5 test 1: --recompute-embeddings flag exists and is parseable
# ---------------------------------------------------------------------------


def test_recompute_embeddings_flag_exists() -> None:
    """AP5: rebuild_all_projections.py supports --recompute-embeddings flag.

    When passed, the rebuild must ignore chunk.embedding and recompute
    from chunk.text via the live embedding provider.
    """
    source = (_SCRIPTS_DIR / "rebuild_all_projections.py").read_text(encoding="utf-8")
    assert "--recompute-embeddings" in source, (
        "rebuild_all_projections.py must contain the --recompute-embeddings flag"
    )
    assert "RECOMPUTE_EMBEDDINGS_FLAG" in source, (
        "rebuild_all_projections.py must export RECOMPUTE_EMBEDDINGS_FLAG constant"
    )
    # Verify the flag parsing handles it.
    assert "recompute_embeddings" in source, (
        "rebuild_all_projections.py must parse and use the recompute_embeddings variable"
    )


# ---------------------------------------------------------------------------
# AP5 test 2: seed_dr_drill does NOT use zero vectors
# ---------------------------------------------------------------------------

#: Node types that may have a docstring as their first body statement.
_DOCSTRING_CONTAINERS = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


def _extract_non_docstring_code(source: str) -> str:
    """Return source with docstring lines replaced by blank lines.

    Uses ast to identify module/function/class docstrings and strips them
    so that assertions against the code content are not confused by
    historical docstring references.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    # Collect line ranges of docstring nodes (first stmt Expr(Constant) in
    # modules, functions, and classes).
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_CONTAINERS) and (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            ds_node = node.body[0]
            # Mark every line in the docstring node as "to be blanked".
            for lineno in range(ds_node.lineno, ds_node.end_lineno + 1):
                docstring_lines.add(lineno)

    lines = source.splitlines(keepends=True)
    stripped = []
    for i, line in enumerate(lines, start=1):
        if i in docstring_lines:
            stripped.append("\n")
        else:
            stripped.append(line)
    return "".join(stripped)


def test_seed_dr_drill_does_not_use_zero_vectors() -> None:
    """AP5: seed_dr_drill.py must not pre-load zero-vector embeddings.

    Zero-vector bypass hides the embedding path from the drill.
    The seed script must NOT store a pre-computed zero embedding —
    it must store no embedding (None) so the rebuild path is exercised.

    We check the non-docstring portion of the file to avoid false positives
    from historical documentation that describes the removed pattern.
    """
    seed_source = (_SCRIPTS_DIR / "seed_dr_drill.py").read_text(encoding="utf-8")

    # Strip docstrings before checking so historical references in docs
    # don't trigger false positives.
    code_only = _extract_non_docstring_code(seed_source)

    # The old zero-vector pattern must not be present in live code.
    assert "ZERO_EMBEDDING" not in code_only, (
        "seed_dr_drill.py must not define ZERO_EMBEDDING in live code "
        "(AP5 zero-vector bypass removed)"
    )
    # Check for actual assignment of a zero-vector list literal (e.g. `= [0.0] * N`).
    # The docstring may mention this pattern; the code must not.
    assert "= [0.0] *" not in code_only and "=[0.0]*" not in code_only, (
        "seed_dr_drill.py must not assign a zero-vector embedding in live code "
        "(AP5 bypass removed)"
    )
    # The new pattern: embedding=None for chunks.
    assert "embedding=None" in seed_source, (
        "seed_dr_drill.py must seed chunks with embedding=None so the rebuild "
        "recomputes from SoT text (AP5 rebuild-from-SoT requirement)"
    )


# ---------------------------------------------------------------------------
# AP5 test 3: dr_verify includes hash-equivalence assertion
# ---------------------------------------------------------------------------


def test_dr_verify_includes_hash_equivalence() -> None:
    """AP5: dr_verify.py must assert vector/content hash equivalence.

    Count-equality alone is insufficient — the verify module must export
    verify_hash_equivalence, ChunkHashRecord, compute_vector_digest, and
    compute_text_hash so callers can assert rebuild-from-SoT correctness.
    """
    # All required symbols are importable from dr_verify.
    assert callable(verify_hash_equivalence), "verify_hash_equivalence must be callable"
    assert ChunkHashRecord is not None, "ChunkHashRecord must be defined"
    assert callable(compute_vector_digest), "compute_vector_digest must be callable"
    assert callable(compute_text_hash), "compute_text_hash must be callable"

    # verify_hash_equivalence returns a list of mismatches (empty = pass).
    result = verify_hash_equivalence(stored=[], recomputed=[])
    assert isinstance(result, list), "verify_hash_equivalence must return a list"
    assert result == [], "Empty inputs must produce no mismatches"


# ---------------------------------------------------------------------------
# AP5 test 4: hash-equivalence logic is correct (mock-only)
# ---------------------------------------------------------------------------


def test_dr_smoke_hash_equivalence_logic() -> None:
    """AP5: verify_hash_equivalence correctly detects vector mismatches.

    This test exercises the hash-equivalence logic with deterministic fake
    embeddings so no Postgres or Qdrant service is required.

    Scenario: 3 chunks seeded with known texts.  Two paths produce records:
    1. 'stored' — simulates reading vectors back from the rebuilt Qdrant.
    2. 'recomputed' — simulates calling the embedding provider on SoT text.

    When both paths use the same provider (same text -> same vector),
    verify_hash_equivalence must return no mismatches.
    When a stored vector does NOT match the recomputed one (e.g. a stale
    zero-vector was stored), a mismatch must be reported.
    """
    import struct

    def _fake_embed(text: str) -> list[float]:
        """Deterministic fake embedding: SHA-256 of text -> 8 floats."""
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Take first 32 bytes (8 x float32) for a stable 8-dim vector.
        return list(struct.unpack("<8f", digest[:32]))

    chunk_ids = [uuid.uuid4() for _ in range(3)]
    texts = [
        "Chunk text for document 1, chunk 0: database migration strategy.",
        "Chunk text for document 1, chunk 1: kubernetes deployment policy.",
        "Chunk text for document 1, chunk 2: observability and alerting.",
    ]

    # Simulate stored records (rebuild recomputed from SoT text).
    stored = [
        ChunkHashRecord(
            chunk_id=cid,
            text_hash=compute_text_hash(txt),
            vector_digest=compute_vector_digest(_fake_embed(txt)),
        )
        for cid, txt in zip(chunk_ids, texts, strict=True)
    ]

    # Simulate recomputed records (fresh call to embedding provider on SoT text).
    recomputed = [
        ChunkHashRecord(
            chunk_id=cid,
            text_hash=compute_text_hash(txt),
            vector_digest=compute_vector_digest(_fake_embed(txt)),
        )
        for cid, txt in zip(chunk_ids, texts, strict=True)
    ]

    # --- Happy path: no mismatches ---
    mismatches = verify_hash_equivalence(stored=stored, recomputed=recomputed)
    assert mismatches == [], (
        f"Same provider, same text -> no mismatches expected; got: {mismatches}"
    )

    # --- Stale bypass: stored has a zero vector, recomputed has real vector ---
    stale_vector = [0.0] * 8
    stale_stored = list(stored)
    stale_stored[0] = ChunkHashRecord(
        chunk_id=chunk_ids[0],
        text_hash=compute_text_hash(texts[0]),
        vector_digest=compute_vector_digest(stale_vector),  # stale zero vector!
    )
    mismatches_stale = verify_hash_equivalence(stored=stale_stored, recomputed=recomputed)
    assert len(mismatches_stale) == 1, (
        f"One stale zero-vector should produce exactly one mismatch; got: {mismatches_stale}"
    )
    assert "vector_digest mismatch" in mismatches_stale[0], (
        f"Mismatch message should describe vector_digest mismatch; got: {mismatches_stale[0]}"
    )

    # --- Missing chunk: stored is missing one chunk ---
    missing_stored = stored[1:]  # drop first chunk
    mismatches_missing = verify_hash_equivalence(stored=missing_stored, recomputed=recomputed)
    assert any("missing from stored" in m for m in mismatches_missing), (
        f"Missing stored chunk must be reported; got: {mismatches_missing}"
    )
