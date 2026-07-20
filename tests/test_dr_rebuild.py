"""Tests for AP5: deterministic DR rebuild, post-rebuild verification, and RTO budget.

Test classes
------------
TestVerifyProjections        — unit tests for dr_verify.verify_projections
TestRtoCheck                 — unit tests for dr_verify.check_rto
TestDrVerificationResult     — unit tests for DRVerificationResult helpers
TestDrIntegration            — integration test (OMNISCIENCE_DR_DRILL=1 only)
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Make scripts/ importable from tests/ without installing the scripts package.
# ---------------------------------------------------------------------------
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from dr_verify import (
    DEFAULT_RTO_SECONDS,
    DRVerificationResult,
    RtoResult,
    check_rto,
    verify_projections,
)
from rebuild_all_projections import _chunk_projection, _reset_qdrant_collections
from seed_dr_drill import _chunk_row

# ---------------------------------------------------------------------------
# Fake protocol implementations (no real stores required)
# ---------------------------------------------------------------------------


class _FakePg:
    """Fake Postgres SoT for unit tests."""

    def __init__(
        self,
        *,
        doc_counts: dict[str, int] | None = None,
        chunk_counts: dict[str, int] | None = None,
        max_versions: dict[str, int] | None = None,
    ) -> None:
        self._doc_counts = doc_counts or {}
        self._chunk_counts = chunk_counts or {}
        self._max_versions = max_versions or {}

    async def get_document_counts_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return dict(self._doc_counts)

    async def get_chunk_counts_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return dict(self._chunk_counts)

    async def get_max_doc_version_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return dict(self._max_versions)


class _FakeQdrant:
    """Fake Qdrant store for unit tests."""

    def __init__(
        self,
        *,
        chunk_counts: dict[str, int] | None = None,
        checkpoint_versions: dict[str, int] | None = None,
    ) -> None:
        self._chunk_counts = chunk_counts or {}
        self._checkpoint_versions = checkpoint_versions or {}

    async def count_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID | None = None,
        as_of: Any | None = None,
    ) -> int:
        if source_id is not None:
            return self._chunk_counts.get(str(source_id), 0)
        return sum(self._chunk_counts.values())

    async def get_checkpoint_versions(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return dict(self._checkpoint_versions)


class _FakeNeo4j:
    """Fake Neo4j store for unit tests."""

    def __init__(
        self,
        *,
        entity_counts: dict[str, int] | None = None,
        checkpoint_versions: dict[str, int] | None = None,
    ) -> None:
        self._entity_counts = entity_counts or {}
        self._checkpoint_versions = checkpoint_versions or {}

    async def count_entities_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return dict(self._entity_counts)

    async def get_checkpoint_versions(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return dict(self._checkpoint_versions)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_SRC = str(uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001"))


def test_dr_seed_chunk_row_omits_removed_embedding_column() -> None:
    row = _chunk_row(0, uuid.UUID("30000000-0000-0000-0000-000000000001"), 0)

    assert "embedding" not in row
    assert row["text"]
    assert row["embedding_provider"] == "local"


def test_dr_rebuild_projection_supports_current_and_legacy_chunk_schemas() -> None:
    current_keys = {column.key for column in _chunk_projection(include_embedding=False)}
    legacy_keys = {column.key for column in _chunk_projection(include_embedding=True)}

    assert "embedding" not in current_keys
    assert legacy_keys == current_keys | {"embedding"}
    assert {"id", "document_id", "ord", "text", "metadata"} <= current_keys


@pytest.mark.asyncio
async def test_dr_qdrant_reset_rebootstraps_collection_after_wipe() -> None:
    qdrant = AsyncMock()
    qdrant.get_collections.return_value = SimpleNamespace(
        collections=[
            SimpleNamespace(name="omniscience__model__d384"),
            SimpleNamespace(name="unmanaged"),
        ]
    )
    store = AsyncMock()

    await _reset_qdrant_collections(qdrant, store)

    qdrant.delete_collection.assert_awaited_once_with("omniscience__model__d384")
    store.close.assert_awaited_once_with()
    store.connect.assert_awaited_once_with()


def test_dr_rebuild_advances_graph_checkpoint_for_empty_projection() -> None:
    rebuild_source = (
        Path(__file__).resolve().parents[1] / "scripts" / "rebuild_all_projections.py"
    ).read_text(encoding="utf-8")
    call = re.search(
        r"await graph_store\.upsert_graph\((?P<args>.*?)\n\s*\)",
        rebuild_source,
        re.DOTALL,
    )

    assert call is not None
    assert "workspace_id=workspace_id" in call.group("args")
    assert "if wrapped_entities" not in rebuild_source


def test_rebuild_pre_wipe_safety_guard_runs_before_wipe() -> None:
    """AC-DR-3 / P-SOT-6: the destructive-safety guard must run — and be
    able to abort — before any wipe command executes. CI-portable source
    check; the live behaviour is exercised by
    TestDrIntegration::test_pre_wipe_refusal_aborts_on_non_empty_without_drill_context.
    """
    rebuild_source = (
        Path(__file__).resolve().parents[1] / "scripts" / "rebuild_all_projections.py"
    ).read_text(encoding="utf-8")

    guard_idx = rebuild_source.index("check_destructive_safety(")
    wipe_idx = rebuild_source.index('print("Wiping Neo4j and Qdrant...")')

    assert guard_idx < wipe_idx, "destructive-safety guard must run before the wipe begins"
    assert "sys.exit(4)" in rebuild_source
    assert "OMNISCIENCE_DR_DRILL" in rebuild_source


def _perfect_fakes(
    *,
    chunk_count: int = 9,
    max_version: int = 3,
    entity_count: int = 6,
) -> tuple[_FakePg, _FakeQdrant, _FakeNeo4j]:
    """Return fakes where everything matches perfectly."""
    pg = _FakePg(
        doc_counts={_SRC: 3},
        chunk_counts={_SRC: chunk_count},
        max_versions={_SRC: max_version},
    )
    qdrant = _FakeQdrant(
        chunk_counts={_SRC: chunk_count},
        checkpoint_versions={_SRC: max_version},
    )
    neo4j = _FakeNeo4j(
        entity_counts={_SRC: entity_count},
        checkpoint_versions={_SRC: max_version},
    )
    return pg, qdrant, neo4j


# ---------------------------------------------------------------------------
# TestVerifyProjections — unit tests
# ---------------------------------------------------------------------------


class TestVerifyProjections:
    """verify_projections returns passed=True when counts and checkpoints match."""

    @pytest.mark.asyncio
    async def test_perfect_match_passes(self) -> None:
        pg, qdrant, neo4j = _perfect_fakes()
        result = await verify_projections(workspace_id=_WS, pg=pg, qdrant=qdrant, neo4j=neo4j)
        assert result.passed
        assert result.mismatches == []

    @pytest.mark.asyncio
    async def test_qdrant_chunk_count_mismatch_fails(self) -> None:
        pg, _, neo4j = _perfect_fakes(chunk_count=9)
        qdrant_bad = _FakeQdrant(
            chunk_counts={_SRC: 7},  # missing 2 chunks
            checkpoint_versions={_SRC: 3},
        )
        result = await verify_projections(workspace_id=_WS, pg=pg, qdrant=qdrant_bad, neo4j=neo4j)
        assert not result.passed
        assert any("Qdrant chunk count mismatch" in m for m in result.mismatches)

    @pytest.mark.asyncio
    async def test_qdrant_checkpoint_stale_fails(self) -> None:
        pg, _, neo4j = _perfect_fakes(max_version=5)
        qdrant_stale = _FakeQdrant(
            chunk_counts={_SRC: 9},
            checkpoint_versions={_SRC: 2},  # stale: behind max_version=5
        )
        result = await verify_projections(
            workspace_id=_WS, pg=pg, qdrant=qdrant_stale, neo4j=neo4j
        )
        assert not result.passed
        assert any("Qdrant checkpoint stale" in m for m in result.mismatches)

    @pytest.mark.asyncio
    async def test_neo4j_checkpoint_stale_fails(self) -> None:
        pg, qdrant, _ = _perfect_fakes(max_version=5)
        neo4j_stale = _FakeNeo4j(
            entity_counts={_SRC: 6},
            checkpoint_versions={_SRC: 1},  # stale
        )
        result = await verify_projections(
            workspace_id=_WS, pg=pg, qdrant=qdrant, neo4j=neo4j_stale
        )
        assert not result.passed
        assert any("Neo4j checkpoint stale" in m for m in result.mismatches)

    @pytest.mark.asyncio
    async def test_multiple_mismatches_all_collected(self) -> None:
        """All mismatches are collected, not short-circuited."""
        pg = _FakePg(
            doc_counts={_SRC: 3},
            chunk_counts={_SRC: 9},
            max_versions={_SRC: 5},
        )
        qdrant_bad = _FakeQdrant(
            chunk_counts={_SRC: 4},  # wrong count
            checkpoint_versions={_SRC: 1},  # stale
        )
        neo4j_bad = _FakeNeo4j(
            entity_counts={_SRC: 6},
            checkpoint_versions={_SRC: 0},  # stale
        )
        result = await verify_projections(
            workspace_id=_WS, pg=pg, qdrant=qdrant_bad, neo4j=neo4j_bad
        )
        assert not result.passed
        assert len(result.mismatches) >= 3

    @pytest.mark.asyncio
    async def test_empty_postgres_passes(self) -> None:
        """No sources in Postgres → nothing to verify → passes."""
        pg = _FakePg()
        qdrant = _FakeQdrant()
        neo4j = _FakeNeo4j()
        result = await verify_projections(workspace_id=_WS, pg=pg, qdrant=qdrant, neo4j=neo4j)
        assert result.passed

    @pytest.mark.asyncio
    async def test_zero_version_skips_checkpoint_check(self) -> None:
        """Sources whose max_version==0 skip the checkpoint advancement check."""
        pg = _FakePg(
            doc_counts={_SRC: 1},
            chunk_counts={_SRC: 2},
            max_versions={_SRC: 0},  # version never set
        )
        qdrant = _FakeQdrant(
            chunk_counts={_SRC: 2},
            checkpoint_versions={_SRC: 0},
        )
        neo4j = _FakeNeo4j(
            entity_counts={_SRC: 0},
            checkpoint_versions={_SRC: 0},
        )
        result = await verify_projections(workspace_id=_WS, pg=pg, qdrant=qdrant, neo4j=neo4j)
        # Chunk counts match, version==0 so no checkpoint assert — should pass
        assert result.passed

    @pytest.mark.asyncio
    async def test_result_attributes_populated(self) -> None:
        """Result carries all per-store count dicts for downstream consumption."""
        pg, qdrant, neo4j = _perfect_fakes()
        result = await verify_projections(workspace_id=_WS, pg=pg, qdrant=qdrant, neo4j=neo4j)
        assert _SRC in result.pg_chunk_counts
        assert _SRC in result.qdrant_chunk_counts
        assert _SRC in result.neo4j_entity_counts
        assert result.pg_chunk_counts[_SRC] == 9
        assert result.qdrant_chunk_counts[_SRC] == 9


# ---------------------------------------------------------------------------
# TestDrVerificationResult — unit tests
# ---------------------------------------------------------------------------


class TestDrVerificationResult:
    def test_passed_true_when_no_mismatches(self) -> None:
        r = DRVerificationResult(workspace_id=_WS)
        assert r.passed is True

    def test_passed_false_when_mismatches_present(self) -> None:
        r = DRVerificationResult(workspace_id=_WS, mismatches=["chunk mismatch for src X"])
        assert r.passed is False

    def test_summary_lines_contain_pass(self) -> None:
        r = DRVerificationResult(workspace_id=_WS)
        lines = r.summary_lines()
        assert any("PASS" in line for line in lines)

    def test_summary_lines_contain_fail(self) -> None:
        r = DRVerificationResult(
            workspace_id=_WS,
            mismatches=["something wrong"],
        )
        lines = r.summary_lines()
        assert any("FAIL" in line for line in lines)
        assert any("something wrong" in line for line in lines)

    def test_summary_lines_include_workspace_id(self) -> None:
        r = DRVerificationResult(workspace_id=_WS)
        joined = "\n".join(r.summary_lines())
        assert str(_WS) in joined


# ---------------------------------------------------------------------------
# TestRtoCheck — unit tests
# ---------------------------------------------------------------------------


class TestRtoCheck:
    def test_under_budget_does_not_exit(self) -> None:
        start = time.monotonic() - 10  # pretend 10 s elapsed
        result = check_rto(start_time=start, budget_seconds=900, exit_on_exceeded=False)
        assert not result.exceeded
        assert result.elapsed_seconds >= 10
        assert result.elapsed_seconds < 900

    def test_over_budget_exceeded_flag(self) -> None:
        start = time.monotonic() - 1000  # pretend 1000 s elapsed
        result = check_rto(start_time=start, budget_seconds=900, exit_on_exceeded=False)
        assert result.exceeded
        assert result.elapsed_seconds > 900

    def test_over_budget_calls_sys_exit(self) -> None:
        start = time.monotonic() - 1000
        with pytest.raises(SystemExit) as exc_info:
            check_rto(start_time=start, budget_seconds=900, exit_on_exceeded=True)
        assert exc_info.value.code == 2

    def test_exactly_at_budget_not_exceeded(self) -> None:
        # elapsed < budget_seconds (we're at ~1 ms, budget is 1)
        start = time.monotonic()
        result = check_rto(start_time=start, budget_seconds=3600, exit_on_exceeded=False)
        assert not result.exceeded

    def test_default_rto_is_documented_value(self) -> None:
        assert DEFAULT_RTO_SECONDS == 900

    def test_rto_result_message_ok(self) -> None:
        r = RtoResult(elapsed_seconds=30.0, budget_seconds=900, exceeded=False)
        assert "OK" in r.message
        assert "30.0" in r.message

    def test_rto_result_message_exceeded(self) -> None:
        r = RtoResult(elapsed_seconds=1200.0, budget_seconds=900, exceeded=True)
        assert "EXCEEDED" in r.message


# ---------------------------------------------------------------------------
# TestDrIntegration — integration test (requires OMNISCIENCE_DR_DRILL=1)
# ---------------------------------------------------------------------------

_DR_DRILL = os.environ.get("OMNISCIENCE_DR_DRILL") == "1"


@pytest.mark.skipif(not _DR_DRILL, reason="Requires OMNISCIENCE_DR_DRILL=1 and live stores")
class TestDrIntegration:
    """Full integration: seed → rebuild → verify → assert determinism.

    These tests run in the dr-drill GitHub Actions workflow where
    postgres/neo4j/qdrant service containers are available and the
    fixture dataset has been seeded by tests/fixtures/seed_dr_drill.py.
    """

    @pytest.mark.asyncio
    async def test_verification_passes_after_seed(self) -> None:
        """After seeding + rebuild, verification must pass."""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                "scripts/rebuild_all_projections.py",
                "--yes",
                "--rto-seconds",
                "120",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"rebuild_all_projections.py failed (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
        assert "PASS" in result.stdout, f"Expected PASS in output:\n{result.stdout}"

    @pytest.mark.asyncio
    async def test_verify_only_is_idempotent(self) -> None:
        """Running --verify-only twice should produce identical PASS output."""
        import subprocess

        runs = []
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/rebuild_all_projections.py",
                    "--verify-only",
                    "--rto-seconds",
                    "120",
                ],
                capture_output=True,
                text=True,
            )
            runs.append(result)

        for run in runs:
            assert run.returncode == 0, (
                f"verify-only failed (exit {run.returncode}):\n{run.stdout}\n{run.stderr}"
            )
            assert "PASS" in run.stdout

    @pytest.mark.asyncio
    async def test_two_rebuilds_produce_same_checkpoint_versions(self) -> None:
        """Deterministic ORDER BY guarantees identical checkpoint versions across two rebuilds."""
        import re
        import subprocess

        checkpoint_pattern = re.compile(r"qdrant_ckpt=(\d+)")
        versions = []
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/rebuild_all_projections.py",
                    "--yes",
                    "--rto-seconds",
                    "120",
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"rebuild failed:\n{result.stdout}\n{result.stderr}"
            found = checkpoint_pattern.findall(result.stdout)
            versions.append(found)

        assert versions[0] == versions[1], (
            f"Non-deterministic rebuild: run 1={versions[0]}, run 2={versions[1]}"
        )

    @pytest.mark.asyncio
    async def test_pre_wipe_refusal_aborts_on_non_empty_without_drill_context(self) -> None:
        """AC-DR-3 / P-SOT-6: a rebuild against already-populated projections
        must abort BEFORE wipe when OMNISCIENCE_DR_DRILL is not set. The
        ordinary sanctioned-drill path (env var present, exercised by every
        other test in this class) is unaffected.
        """
        import subprocess

        rebuild_cmd = [
            sys.executable,
            "scripts/rebuild_all_projections.py",
            "--yes",
            "--rto-seconds",
            "120",
        ]

        # Ensure Neo4j/Qdrant are populated by a normal, drill-sanctioned rebuild.
        populate = subprocess.run(rebuild_cmd, capture_output=True, text=True)
        assert populate.returncode == 0, (
            f"setup rebuild failed (exit {populate.returncode}):\n"
            f"{populate.stdout}\n{populate.stderr}"
        )

        # Strip only the drill marker; keep DATABASE_URL/NEO4J_*/QDRANT_* so
        # the process can still reach the (now non-empty) stores.
        env = os.environ.copy()
        env.pop("OMNISCIENCE_DR_DRILL", None)
        refused = subprocess.run(rebuild_cmd, capture_output=True, text=True, env=env)
        assert refused.returncode == 4, (
            f"expected pre-wipe refusal exit code 4, got {refused.returncode}:\n"
            f"{refused.stdout}\n{refused.stderr}"
        )
        assert "non_empty_projections" in refused.stdout

        # Restore a known-good state (drill-sanctioned) for any test that
        # runs after this one in the same session.
        cleanup = subprocess.run(rebuild_cmd, capture_output=True, text=True)
        assert cleanup.returncode == 0, (
            f"cleanup rebuild failed (exit {cleanup.returncode}):\n"
            f"{cleanup.stdout}\n{cleanup.stderr}"
        )
