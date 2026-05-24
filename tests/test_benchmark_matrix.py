"""End-to-end test of the cross-vendor matrix runner.

We don't hit any vendor here — every adapter runs in mock-mode and
returns empty / canned responses. The point is to verify the CLI plumbs
fixtures + adapters + scorers + report formatting together cleanly.
"""

from __future__ import annotations

import json

import pytest

from bench import runner_matrix


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCH_LIVE_VENDORS", raising=False)


class TestMatrixCLI:
    def test_help_does_not_crash(self) -> None:
        parser = runner_matrix.build_arg_parser()
        assert parser.prog == "bench/runner_matrix.py"

    def test_json_mode_all_vendors(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = runner_matrix.main(["--vendor", "all", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["live"] is False
        assert "as_of" in payload
        # Every vendor must appear in results, even if all-zeros from mock-mode.
        for name in runner_matrix.VENDOR_ORDER:
            assert name in payload["results"], f"missing vendor {name}"
            metrics = payload["results"][name]
            # 50-incident corpus.
            assert metrics["n"] == 50
            # All metrics present.
            for key in (
                "top1_accuracy",
                "mrr",
                "brier",
                "p50_latency_seconds",
                "p99_latency_seconds",
                "per_category",
            ):
                assert key in metrics

    def test_single_vendor_filters_results(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = runner_matrix.main(["--vendor", "omniscience", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload["results"].keys()) == {"omniscience"}

    def test_unknown_vendor_exits(self) -> None:
        with pytest.raises(SystemExit):
            runner_matrix.main(["--vendor", "not-a-vendor"])

    def test_leaderboard_renders_each_vendor_row(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = runner_matrix.main(["--vendor", "all"])
        assert rc == 0
        report = capsys.readouterr().out
        for name in runner_matrix.VENDOR_ORDER:
            assert name in report
        for column in ("top-1", "MRR", "Brier", "p50", "p99"):
            assert column in report
