"""Unit tests for the benchmark runner skeleton (issue #241, scaffolding subset).

Scope:

* Schema validator catches the common ways a fixture goes wrong.
* Scorers compute the documented formulas on hand-built inputs.
* Runner round-trips a small in-memory client through ``run_corpus`` and
  ``summarise``.
* The corpus on disk validates and the dry-run mode goes green.

Live-network tests against a real Omniscience server are out of scope
for this PR and will land alongside the cross-vendor adapter matrix.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from bench import scorers
from bench.runner import (
    DEFAULT_CORPUS_DIR,
    DryRunClient,
    _bundle_to_response,
    build_arg_parser,
    format_report,
    load_corpus,
    main,
    metrics_to_json,
    run_corpus,
)
from bench.schema import (
    CATEGORIES,
    ExpectedRootCause,
    IncidentFixture,
    InvalidIncidentError,
    parse_incident,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "bench-test-0001",
        "category": "oom",
        "description": "x",
        "alert_payload": {"k": "v"},
        "expected_root_cause": {
            "summary": "s",
            "canonical_entities": ["https://example/pr/1", "pod/x"],
        },
        "expected_runbook_step": "do the thing",
        "expected_blast_radius": ["service/x"],
    }
    base.update(overrides)
    return base


def _fixture(
    fid: str = "f",
    category: str = "oom",
    canonical: tuple[str, ...] = ("ent-1", "ent-2"),
) -> IncidentFixture:
    return IncidentFixture(
        id=fid,
        category=category,
        description="d",
        alert_payload={"id": fid},
        expected_root_cause=ExpectedRootCause(summary="s", canonical_entities=canonical),
        expected_runbook_step="r",
        expected_blast_radius=("service/x",),
    )


def _result(
    fid: str = "f",
    retrieved: tuple[str, ...] = ("ent-1",),
    confidence: float = 1.0,
    latency: float = 0.05,
    golden_top1: str = "ent-1",
    golden_all: tuple[str, ...] = ("ent-1", "ent-2"),
    category: str = "oom",
    error: str | None = None,
) -> scorers.FixtureResult:
    return scorers.FixtureResult(
        fixture_id=fid,
        category=category,
        retrieved_entities=retrieved,
        confidence=confidence,
        latency_seconds=latency,
        golden_top1=golden_top1,
        golden_all=golden_all,
        error=error,
    )


# ---------------------------------------------------------------------------
# Schema validator
# ---------------------------------------------------------------------------


class TestSchemaValidator:
    def test_valid_fixture_parses(self) -> None:
        fixture = parse_incident(_valid_raw(), path_hint="t")
        assert fixture.id == "bench-test-0001"
        assert fixture.category == "oom"
        assert fixture.expected_root_cause.canonical_entities[0] == "https://example/pr/1"
        assert fixture.expected_blast_radius == ("service/x",)
        assert fixture.notes == ""

    def test_missing_required_field_raises(self) -> None:
        raw = _valid_raw()
        del raw["id"]
        with pytest.raises(InvalidIncidentError, match="missing required field 'id'"):
            parse_incident(raw, path_hint="t")

    def test_unknown_category_raises(self) -> None:
        raw = _valid_raw(category="not-a-category")
        with pytest.raises(InvalidIncidentError, match="category"):
            parse_incident(raw, path_hint="t")

    def test_empty_canonical_entities_raises(self) -> None:
        raw = _valid_raw(
            expected_root_cause={"summary": "s", "canonical_entities": []},
        )
        with pytest.raises(InvalidIncidentError, match="canonical_entities"):
            parse_incident(raw, path_hint="t")

    def test_non_string_blast_radius_raises(self) -> None:
        raw = _valid_raw(expected_blast_radius=["service/x", 42])
        with pytest.raises(InvalidIncidentError, match="expected_blast_radius"):
            parse_incident(raw, path_hint="t")

    def test_empty_runbook_step_raises(self) -> None:
        raw = _valid_raw(expected_runbook_step="")
        with pytest.raises(InvalidIncidentError, match="expected_runbook_step"):
            parse_incident(raw, path_hint="t")

    def test_categories_constant_is_non_empty(self) -> None:
        assert len(CATEGORIES) >= 6


# ---------------------------------------------------------------------------
# Scorer formulas
# ---------------------------------------------------------------------------


class TestScorerFormulas:
    def test_top1_accuracy_on_mixed_hits(self) -> None:
        results = [
            _result(fid="a", retrieved=("ent-1",)),  # hit
            _result(fid="b", retrieved=("ent-2",)),  # miss (top is ent-2)
            _result(fid="c", retrieved=("ent-1", "ent-2")),  # hit
            _result(fid="d", retrieved=()),  # miss (empty)
        ]
        assert scorers.top1_accuracy(results) == pytest.approx(2 / 4)

    def test_mrr_uses_first_golden_match_rank(self) -> None:
        results = [
            # rank 1 (ent-1)
            _result(fid="a", retrieved=("ent-1", "ent-2"), golden_all=("ent-1", "ent-2")),
            # rank 2 (ent-2 is in goldens)
            _result(fid="b", retrieved=("other", "ent-2"), golden_all=("ent-1", "ent-2")),
            # no hit
            _result(fid="c", retrieved=("x", "y"), golden_all=("ent-1",)),
        ]
        # RR: 1.0, 0.5, 0.0  -> mean 0.5
        assert scorers.mean_reciprocal_rank(results) == pytest.approx(0.5)

    def test_brier_score_perfect_calibration_is_zero(self) -> None:
        results = [
            _result(retrieved=("ent-1",), confidence=1.0),  # correct, conf 1.0
            _result(retrieved=("zzz",), confidence=0.0),  # wrong, conf 0.0
        ]
        assert scorers.brier_score(results) == pytest.approx(0.0)

    def test_brier_score_overconfident_wrong(self) -> None:
        results = [
            _result(retrieved=("zzz",), confidence=1.0),  # wrong, conf 1.0 -> (1-0)^2 = 1
            _result(retrieved=("zzz",), confidence=1.0),  # wrong, conf 1.0 -> 1
        ]
        assert scorers.brier_score(results) == pytest.approx(1.0)

    def test_brier_excludes_errored_calls(self) -> None:
        results = [
            _result(retrieved=("ent-1",), confidence=1.0),  # correct -> 0
            _result(retrieved=(), confidence=0.5, error="HTTPError"),  # excluded
        ]
        assert scorers.brier_score(results) == pytest.approx(0.0)

    def test_errored_call_counts_as_top1_miss_and_rr_zero(self) -> None:
        results = [
            _result(retrieved=(), confidence=0.0, error="boom", golden_top1="ent-1"),
        ]
        assert scorers.top1_accuracy(results) == 0.0
        assert scorers.mean_reciprocal_rank(results) == 0.0

    def test_latency_percentiles_nearest_rank(self) -> None:
        # samples sorted: [0.01, 0.02, 0.05, 0.10, 1.00]
        results = [
            _result(fid=str(i), latency=lat)
            for i, lat in enumerate([0.10, 0.01, 1.00, 0.02, 0.05])
        ]
        assert scorers.latency_percentile(results, 50.0) == pytest.approx(0.05)
        # p99 of 5 samples by nearest-rank = ceil(0.99*5)=5 -> samples[4] = 1.00
        assert scorers.latency_percentile(results, 99.0) == pytest.approx(1.00)
        # p=0 clamps to rank 1 (the minimum sample), not 0; documents the contract.
        assert scorers.latency_percentile(results, 0.0) == pytest.approx(0.01)
        # Empty input returns 0.0 (no samples to report).
        assert scorers.latency_percentile([], 50.0) == pytest.approx(0.0)

    def test_latency_percentile_rejects_bad_input(self) -> None:
        with pytest.raises(ValueError):
            scorers.latency_percentile([_result()], 150.0)

    def test_summarise_breaks_down_by_category(self) -> None:
        results = [
            _result(fid="a", category="oom", retrieved=("ent-1",)),
            _result(fid="b", category="oom", retrieved=("zzz",)),
            _result(fid="c", category="dns", retrieved=("ent-1",)),
        ]
        metrics = scorers.summarise(results)
        assert metrics.n == 3
        assert metrics.n_errors == 0
        assert "oom" in metrics.per_category
        assert metrics.per_category["oom"].n == 2
        assert metrics.per_category["oom"].top1_accuracy == pytest.approx(0.5)
        assert metrics.per_category["dns"].top1_accuracy == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bundle adapter
# ---------------------------------------------------------------------------


class TestBundleToResponse:
    def test_orders_target_then_pr_then_threads(self) -> None:
        bundle = {
            "target_resource": {"name": "pod/x"},
            "responsible_pr": {"name": "https://github.com/a/b/pull/1"},
            "slack_threads": [
                {"name": "slack://t1"},
                {"name": "slack://t2"},
            ],
            "confidence_score": 0.9,
        }
        resp = _bundle_to_response(bundle)
        assert resp.retrieved_entities == (
            "pod/x",
            "https://github.com/a/b/pull/1",
            "slack://t1",
            "slack://t2",
        )
        assert resp.confidence == pytest.approx(0.9)

    def test_missing_fields_are_skipped(self) -> None:
        # Only target_resource present; PR + threads absent.
        resp = _bundle_to_response({"target_resource": {"name": "pod/x"}})
        assert resp.retrieved_entities == ("pod/x",)
        assert resp.confidence == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Corpus loading + dry-run round-trip
# ---------------------------------------------------------------------------


class TestCorpusLoading:
    def test_scaffolding_corpus_loads_and_covers_min_categories(self) -> None:
        fixtures = load_corpus(DEFAULT_CORPUS_DIR)
        assert 10 <= len(fixtures) <= 20, (
            "scaffolding PR ships 10-15 fixtures; follow-up grows to 50"
        )
        # All ids unique.
        ids = [f.id for f in fixtures]
        assert len(set(ids)) == len(ids)
        # At least 6 distinct categories represented (out of 8 declared).
        cats = {f.category for f in fixtures}
        assert len(cats) >= 6, f"too few categories covered: {cats}"

    def test_load_corpus_rejects_duplicate_ids(self, tmp_path: Path) -> None:
        for name in ("a.yaml", "b.yaml"):
            (tmp_path / name).write_text(
                "id: dup\ncategory: oom\ndescription: x\nalert_payload: {}\n"
                "expected_root_cause:\n  summary: s\n  canonical_entities: [e]\n"
                "expected_runbook_step: r\nexpected_blast_radius: [b]\n"
            )
        with pytest.raises(InvalidIncidentError, match="duplicate fixture id"):
            load_corpus(tmp_path)

    def test_load_corpus_rejects_non_mapping_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "bad.yaml").write_text("- just\n- a\n- list\n")
        with pytest.raises(InvalidIncidentError, match="top-level YAML must be a mapping"):
            load_corpus(tmp_path)


class TestDryRunRoundTrip:
    def test_dry_run_client_returns_perfect_results(self) -> None:
        fixtures = load_corpus(DEFAULT_CORPUS_DIR)
        client = DryRunClient({json.dumps(f.alert_payload, sort_keys=True): f for f in fixtures})
        results = run_corpus(fixtures, client)
        metrics = scorers.summarise(results)
        assert metrics.n == len(fixtures)
        assert metrics.n_errors == 0
        assert metrics.top1_accuracy == pytest.approx(1.0)
        assert metrics.mrr == pytest.approx(1.0)
        # Brier on perfect calibration (conf=1.0, correct=1) is 0.
        assert metrics.brier == pytest.approx(0.0)
        # Latency on an in-memory stub must be small (< 100ms).
        assert metrics.p99_latency_seconds < 0.1

    def test_run_corpus_records_errors_without_crashing(self) -> None:
        class BoomClient:
            def resolve_incident(self, alert_payload: dict[str, Any], *, timeout: float) -> Any:
                raise RuntimeError("boom")

        fixture = _fixture()
        results = run_corpus([fixture], BoomClient())
        assert len(results) == 1
        assert results[0].error is not None
        assert "RuntimeError" in results[0].error
        # Top-1 / MRR are 0 on errors; latency was still recorded.
        assert scorers.top1_accuracy(results) == 0.0
        assert math.isfinite(results[0].latency_seconds)


# ---------------------------------------------------------------------------
# Reporting + CLI
# ---------------------------------------------------------------------------


class TestReporting:
    def test_format_report_mentions_every_metric(self) -> None:
        results = [_result(retrieved=("ent-1",), confidence=1.0)]
        report = format_report(scorers.summarise(results))
        for needle in ("top-1", "MRR", "Brier", "p50", "p99", "per-category"):
            assert needle in report

    def test_metrics_to_json_is_valid_json(self) -> None:
        results = [_result(retrieved=("ent-1",), confidence=1.0)]
        payload = metrics_to_json(scorers.summarise(results))
        parsed = json.loads(payload)
        assert parsed["n"] == 1
        assert "per_category" in parsed


class TestCLI:
    def test_arg_parser_help_does_not_crash(self) -> None:
        parser = build_arg_parser()
        # No assert beyond "doesn't raise" — argparse stays sane.
        assert parser.prog == "bench/runner.py"

    def test_validate_corpus_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--validate-corpus"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "validated" in out

    def test_main_requires_endpoint_unless_validate(self) -> None:
        with pytest.raises(SystemExit):
            main([])  # no args at all -> parser.error -> SystemExit

    def test_dry_run_via_main(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--mcp-endpoint", "http://unused", "--dry-run", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["top1_accuracy"] == pytest.approx(1.0)
