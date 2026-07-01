"""Tests for the source-level security review bundle (review action point:
attach logic-bearing source in later rounds and complete a source-level
security review).

Contract under test:

Consilium round 1 embedded 24 priority files that were all manifests/config,
so the security-critical surface — token hash/verify, per-tool scope and
workspace/ACL enforcement on the hybrid AND GraphRAG retrieval paths,
tsvector/BM25 + Cypher/SQL query construction, and the as_of vs
ACL/tombstone interaction — was unverifiable from that snapshot.

Acceptance criteria encoded here:

1. ``docs/reviews/consilium-priority-files.txt`` exists: a manifest of
   repo-relative paths (one per line, ``#`` comments allowed) that later
   review rounds embed as priority files.
2. Every path in the manifest exists in the repository — a manifest pointing
   at phantom files re-creates the "unverifiable snapshot" problem.
3. The manifest is dominated by logic-bearing production source, not
   manifests/config: at least ``MIN_LOGIC_BEARING`` non-test source files,
   and logic-bearing entries form a strict majority.
4. Every named security surface is covered by at least one manifest entry
   drawn from that surface's known implementation modules.
5. ``docs/reviews/source-level-security-review.md`` exists and is a real
   review: substantial, citing at least one implementation module per
   security surface, with no citations of nonexistent source paths, and
   explicitly addressing the as_of/tombstone interaction.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

MANIFEST_PATH = REPO_ROOT / "docs" / "reviews" / "consilium-priority-files.txt"
REVIEW_DOC_PATH = REPO_ROOT / "docs" / "reviews" / "source-level-security-review.md"

# File suffixes that count as logic-bearing source. Manifests, lockfiles,
# YAML/TOML config, docs and Dockerfiles explicitly do not.
LOGIC_BEARING_SUFFIXES = {".py", ".ts", ".tsx", ".sql"}

# The round-1 failure mode was 24/24 config files; require a real source core.
MIN_LOGIC_BEARING = 10

# Each security surface named in the review, mapped to the modules that
# implement it today. The manifest must include at least one path per
# surface, and the review document must cite at least one path per surface.
SECURITY_SURFACES: dict[str, list[str]] = {
    "token-hash-verify": [
        "packages/core/src/omniscience_core/auth/tokens.py",
    ],
    "per-tool-scope-enforcement": [
        "packages/core/src/omniscience_core/auth/scopes.py",
        "apps/server/src/omniscience_server/mcp/server.py",
    ],
    "workspace-acl-enforcement": [
        "packages/core/src/omniscience_core/auth/workspace.py",
        "packages/core/src/omniscience_core/auth/middleware.py",
    ],
    "hybrid-retrieval-path": [
        "packages/index/src/omniscience_index/stores/qdrant_store.py",
        "packages/index/src/omniscience_index/stores/qdrant_filters.py",
    ],
    "graphrag-retrieval-path": [
        "packages/retrieval/src/omniscience_retrieval/graph_rag.py",
        "packages/retrieval/src/omniscience_retrieval/graph_query.py",
    ],
    "tsvector-bm25-sql-construction": [
        "packages/index/src/omniscience_index/stores/qdrant_store.py",
        "packages/index/src/omniscience_index/stores/postgres_only_store.py",
        "packages/core/src/omniscience_core/storage/vector.py",
    ],
    "cypher-construction": [
        "packages/index/src/omniscience_index/stores/neo4j/_cypher.py",
        "packages/index/src/omniscience_index/stores/neo4j/store.py",
        "packages/index/src/omniscience_index/stores/neo4j_store.py",
    ],
    "as-of-acl-tombstone-interaction": [
        "packages/retrieval/src/omniscience_retrieval/graph_rag.py",
        "packages/core/src/omniscience_core/storage/graph.py",
        "packages/core/src/omniscience_core/storage/vector.py",
        "packages/index/src/omniscience_index/writer.py",
    ],
}


def _manifest_entries() -> list[str]:
    """Repo-relative paths listed in the priority-files manifest."""
    entries = []
    for raw_line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def _is_logic_bearing(path: str) -> bool:
    """Non-test production source with an executable-logic suffix."""
    if path.startswith("tests/") or "/tests/" in path:
        return False
    return Path(path).suffix in LOGIC_BEARING_SUFFIXES


def _cited_source_paths(text: str) -> set[str]:
    """Repo-relative packages/ and apps/ source paths cited in a document."""
    return set(re.findall(r"(?:packages|apps)/[\w./-]+\.(?:py|ts|tsx|sql)", text))


@pytest.mark.parametrize(
    "surface,candidates",
    sorted(SECURITY_SURFACES.items()),
    ids=sorted(SECURITY_SURFACES),
)
def test_surface_candidate_modules_exist(surface, candidates):
    """Self-check: this guard must reference real modules, so a failure in
    the coverage tests below always means the manifest/review is wrong, not
    that this test file rotted."""
    missing = [c for c in candidates if not (REPO_ROOT / c).is_file()]
    assert not missing, (
        f"security surface {surface!r} references modules that no longer "
        f"exist: {missing} — update SECURITY_SURFACES to the moved paths"
    )


def test_priority_files_manifest_exists():
    assert MANIFEST_PATH.is_file(), (
        "docs/reviews/consilium-priority-files.txt is missing: later review "
        "rounds need a manifest of logic-bearing priority files to embed "
        "(round 1 embedded only manifests/config, leaving the security "
        "surface unverifiable)"
    )


def test_manifest_is_not_empty():
    assert MANIFEST_PATH.is_file(), "manifest missing (see test above)"
    assert _manifest_entries(), (
        "consilium-priority-files.txt exists but lists no files — the "
        "manifest must name the source files to embed in later rounds"
    )


def test_manifest_paths_exist_in_repo():
    assert MANIFEST_PATH.is_file(), "manifest missing (see test above)"
    missing = [p for p in _manifest_entries() if not (REPO_ROOT / p).is_file()]
    assert not missing, (
        f"consilium-priority-files.txt lists paths that do not exist: "
        f"{missing} — every entry must be a repo-relative path to a real file"
    )


def test_manifest_has_enough_logic_bearing_source():
    assert MANIFEST_PATH.is_file(), "manifest missing (see test above)"
    logic = [p for p in _manifest_entries() if _is_logic_bearing(p)]
    assert len(logic) >= MIN_LOGIC_BEARING, (
        f"consilium-priority-files.txt names only {len(logic)} logic-bearing "
        f"production source files (need >= {MIN_LOGIC_BEARING}); round 1 "
        "failed precisely because the bundle was all manifests/config"
    )


def test_manifest_is_majority_logic_bearing_source():
    assert MANIFEST_PATH.is_file(), "manifest missing (see test above)"
    entries = _manifest_entries()
    assert entries, "manifest must not be empty (see test above)"
    logic = [p for p in entries if _is_logic_bearing(p)]
    assert len(logic) * 2 > len(entries), (
        f"only {len(logic)}/{len(entries)} manifest entries are "
        "logic-bearing production source — config/manifest files must not "
        "dominate the review bundle again"
    )


@pytest.mark.parametrize(
    "surface,candidates",
    sorted(SECURITY_SURFACES.items()),
    ids=sorted(SECURITY_SURFACES),
)
def test_manifest_covers_security_surface(surface, candidates):
    """Each security-critical surface from the review must be represented in
    the embedded bundle by at least one of its implementation modules."""
    assert MANIFEST_PATH.is_file(), "manifest missing (see test above)"
    entries = set(_manifest_entries())
    assert entries & set(candidates), (
        f"security surface {surface!r} is not covered: "
        f"consilium-priority-files.txt must include at least one of "
        f"{candidates}"
    )


def test_security_review_document_exists():
    assert REVIEW_DOC_PATH.is_file(), (
        "docs/reviews/source-level-security-review.md is missing: the "
        "action point requires a completed source-level security review of "
        "the token/scope/ACL/query-construction surface, not just a bundle "
        "manifest"
    )


def test_security_review_is_substantial():
    assert REVIEW_DOC_PATH.is_file(), "review document missing (see test above)"
    text = REVIEW_DOC_PATH.read_text(encoding="utf-8")
    assert len(text) >= 2000, (
        f"source-level-security-review.md is only {len(text)} characters — "
        "a placeholder cannot discharge the 'secure by design' claim; write "
        "findings per surface with file-level citations"
    )


@pytest.mark.parametrize(
    "surface,candidates",
    sorted(SECURITY_SURFACES.items()),
    ids=sorted(SECURITY_SURFACES),
)
def test_security_review_cites_each_surface(surface, candidates):
    """The review must cite at least one implementation module per surface —
    a review that never opens the code it clears is the round-1 failure
    restated in prose."""
    assert REVIEW_DOC_PATH.is_file(), "review document missing (see test above)"
    text = REVIEW_DOC_PATH.read_text(encoding="utf-8")
    assert any(candidate in text for candidate in candidates), (
        f"source-level-security-review.md never cites an implementation "
        f"module for surface {surface!r}: cite at least one of {candidates}"
    )


def test_security_review_citations_are_real_paths():
    """Every source path the review cites must exist — phantom citations
    would make the sign-off unauditable."""
    assert REVIEW_DOC_PATH.is_file(), "review document missing (see test above)"
    text = REVIEW_DOC_PATH.read_text(encoding="utf-8")
    cited = _cited_source_paths(text)
    assert cited, (
        "source-level-security-review.md cites no repo source paths at all "
        "(expected repo-relative packages/... or apps/... citations)"
    )
    missing = sorted(p for p in cited if not (REPO_ROOT / p).is_file())
    assert not missing, (
        f"source-level-security-review.md cites nonexistent source paths: "
        f"{missing}"
    )


def test_security_review_addresses_as_of_tombstone_interaction():
    """The as_of vs ACL/tombstone interaction is the one surface the review
    called out by name; the document must address it explicitly."""
    assert REVIEW_DOC_PATH.is_file(), "review document missing (see test above)"
    text = REVIEW_DOC_PATH.read_text(encoding="utf-8")
    assert "as_of" in text and "tombstone" in text.lower(), (
        "source-level-security-review.md must explicitly analyse the "
        "as_of vs ACL/tombstone interaction (time-travel reads must not "
        "resurrect tombstoned or ACL-revoked evidence)"
    )
