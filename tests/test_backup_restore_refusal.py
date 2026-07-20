"""AC-DR-3 tests: destructive-safety refusal.

Production-like names, absent disposable identity, non-empty projections,
active writers, or ambiguous credentials must abort before deletion or
restore — see fixture ids refusal-* in test_backup_restore_fixtures.py.
One test per negative fixture asserting abort=True + a specific reason,
plus one all-clear positive fixture asserting abort=False, mirroring
TestVerifyProjections in tests/test_dr_rebuild.py.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from qualify_backup_restore import (
    AmbiguousIdentityError,
    check_destructive_safety,
)
from qualify_backup_restore import main as cli_main


class _FakeWriterLeaseSource:
    def __init__(self, *, active: bool) -> None:
        self._active = active

    def has_active_writers(self) -> bool:
        return self._active


class _FailingWriterLeaseSource:
    def has_active_writers(self) -> bool:
        raise RuntimeError("lease query unreachable")


class _FakeCredentialResolver:
    def __init__(self, *, ambiguous: bool, identity: str = "unique-recovery-role") -> None:
        self._ambiguous = ambiguous
        self._identity = identity

    def resolve_identity(self) -> str:
        if self._ambiguous:
            raise AmbiguousIdentityError("multiple candidate roles active")
        return self._identity


def _all_clear_kwargs() -> dict[str, object]:
    return {
        "target_name": "restore-qual-recovery",
        "target_tags": {"disposable": "true"},
        "qdrant_chunk_count": 0,
        "neo4j_entity_count": 0,
        "drill_context": False,
        "writer_lease_source": _FakeWriterLeaseSource(active=False),
        "credential_resolver": _FakeCredentialResolver(ambiguous=False),
    }


class TestDestructiveSafetyRefusal:
    def test_production_like_name_aborts(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["target_name"] = "prod-recovery-env"
        result = check_destructive_safety(**kwargs)
        assert result.abort is True
        assert any(r.startswith("production_like_name:") for r in result.reasons)

    def test_production_like_name_case_insensitive_and_word_boundary(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["target_name"] = "PRODUCTION-recovery"
        result = check_destructive_safety(**kwargs)
        assert result.abort is True

    @pytest.mark.parametrize(
        "name",
        [
            "prod1-cluster",
            "prod01",
            "production2",
            "PRODUCTION1",
            "myproduction2",
            "prodenv-restore",
            "live2-cluster",
            "liveenv",
        ],
    )
    def test_production_like_name_digit_suffix_and_concatenation_not_bypassed(
        self, name: str
    ) -> None:
        """A \\b-word-boundary regex treats digits as word characters, so it
        misses prod1/prod01/production2/prodenv/liveenv — all ordinary
        cloud-resource naming patterns. The refusal must be a substring
        match, not word-boundary-anchored."""
        kwargs = _all_clear_kwargs()
        kwargs["target_name"] = name
        result = check_destructive_safety(**kwargs)
        assert result.abort is True
        assert any(r.startswith("production_like_name:") for r in result.reasons)

    def test_missing_disposable_tag_aborts(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["target_tags"] = {"env": "recovery"}  # no disposable=true
        result = check_destructive_safety(**kwargs)
        assert result.abort is True
        assert "absent_disposable_identity" in result.reasons

    def test_non_empty_projections_aborts(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["qdrant_chunk_count"] = 42
        kwargs["neo4j_entity_count"] = 0
        result = check_destructive_safety(**kwargs)
        assert result.abort is True
        assert any(r.startswith("non_empty_projections:") for r in result.reasons)

    def test_non_empty_projections_allowed_in_drill_context(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["qdrant_chunk_count"] = 42
        kwargs["neo4j_entity_count"] = 6
        kwargs["drill_context"] = True
        result = check_destructive_safety(**kwargs)
        assert result.abort is False

    def test_active_writers_aborts(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["writer_lease_source"] = _FakeWriterLeaseSource(active=True)
        result = check_destructive_safety(**kwargs)
        assert result.abort is True
        assert "active_writers_detected" in result.reasons

    def test_writer_lease_check_failure_is_red_not_a_crash(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["writer_lease_source"] = _FailingWriterLeaseSource()
        result = check_destructive_safety(**kwargs)
        assert result.abort is True
        assert any(r.startswith("writer_lease_check_failed:") for r in result.reasons)

    def test_ambiguous_credentials_aborts(self) -> None:
        kwargs = _all_clear_kwargs()
        kwargs["credential_resolver"] = _FakeCredentialResolver(ambiguous=True)
        result = check_destructive_safety(**kwargs)
        assert result.abort is True
        assert any(r.startswith("ambiguous_credentials:") for r in result.reasons)

    def test_all_clear_passes(self) -> None:
        result = check_destructive_safety(**_all_clear_kwargs())
        assert result.abort is False
        assert result.status == "GREEN"
        assert result.reasons == ()

    def test_multiple_failures_all_collected(self) -> None:
        result = check_destructive_safety(
            target_name="prod-live-env",
            target_tags={},
            qdrant_chunk_count=5,
            neo4j_entity_count=3,
            drill_context=False,
            writer_lease_source=_FakeWriterLeaseSource(active=True),
            credential_resolver=_FakeCredentialResolver(ambiguous=True),
        )
        assert result.abort is True
        assert len(result.reasons) >= 5

    def test_optional_checks_skipped_when_not_supplied(self) -> None:
        """target_name/tags/writer_lease/credential_resolver=None means 'not
        evaluated in this caller's context', not an automatic RED — this is
        the subset rebuild_all_projections.py exercises today."""
        result = check_destructive_safety(
            qdrant_chunk_count=0,
            neo4j_entity_count=0,
        )
        assert result.status == "GREEN"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_refusal_red_on_production_name(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(
        [
            "--target-name",
            "prod-cluster",
            "--target-tags-json",
            json.dumps({"disposable": "true"}),
            "--qdrant-chunk-count",
            "0",
            "--neo4j-entity-count",
            "0",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "production_like_name" in captured.err


def test_cli_refusal_green_on_all_clear(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(
        [
            "--target-name",
            "restore-qual-recovery",
            "--target-tags-json",
            json.dumps({"disposable": "true"}),
            "--qdrant-chunk-count",
            "0",
            "--neo4j-entity-count",
            "0",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "GREEN" in captured.out


def test_cli_refusal_counts_required_when_target_name_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["--target-name", "restore-qual-recovery"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "refusal_counts_required" in captured.err
