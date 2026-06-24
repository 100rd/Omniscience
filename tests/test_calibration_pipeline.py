"""Tests for CalibrationPipeline, calibration_store, and the AP4 gate logic.

New in AP4
----------
- :class:`TestCalibrationArtifactPersistence` — round-trip save/load of the
  fitted isotonic artifact.
- :class:`TestLiveScoringLoadsArtifact` — the live scoring path (``calibrate_isotonic``
  via ``probabilistic_scoring``) loads a fitted artifact and returns
  ``calibrated=True`` from ``mcp_resolve_incident``.
- :class:`TestCalibrationGateLogic` — the Brier/ECE gate fails on a
  deliberately miscalibrated fixture and passes on a well-calibrated one.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from omniscience_core.storage.graph import EntityNodeView, GraphResultView
from omniscience_retrieval.incidents.calibration import (
    CalibrationPipeline,
    bootstrap_metrics,
    calculate_watermark_weight,
    collect_labeled_incidents,
    compute_brier_score,
    compute_ece,
    fit_isotonic_regression,
    get_out_of_fold_predictions,
)
from omniscience_retrieval.incidents.calibration_store import (
    CalibrationArtifact,
    compute_reliability_diagram,
    load_calibration_artifact,
    save_calibration_artifact,
)
from omniscience_retrieval.probabilistic_scoring import (
    LAMBDA_TIME_DECAY,
    is_fitted_map_loaded,
    reload_fitted_map,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_good_raw_data(n_low: int = 50, n_high: int = 50) -> list[dict[str, object]]:
    """Return a well-calibrated labeled dataset.

    Uses seed=2026 which is verified to produce:
    - Brier score ~0.196 (< 0.20 threshold)
    - OOF ECE ~0.086 (< 0.10 threshold)

    These properties hold after CalibrationPipeline.run() applies out-of-fold
    isotonic regression.  The committed fixture (tests/fixtures/calibration_labeled.json)
    is generated with the same seed and parameters.
    """
    import random

    rng = random.Random(2026)  # noqa: S311 -- test data, not crypto
    data: list[dict[str, object]] = []
    for i in range(n_low):
        conf = rng.uniform(0.15, 0.35)
        label = 1 if rng.random() < conf else 0
        data.append(
            {
                "incident_id": f"INC-L{i:03d}",
                "predicted_confidence": round(conf, 4),
                "true_label": label,
                "timestamp": "2026-01-15T12:00:00Z",
            }
        )
    for i in range(n_high):
        conf = rng.uniform(0.65, 0.90)
        label = 1 if rng.random() < conf else 0
        data.append(
            {
                "incident_id": f"INC-H{i:03d}",
                "predicted_confidence": round(conf, 4),
                "true_label": label,
                "timestamp": "2026-03-10T12:00:00Z",
            }
        )
    return data


def _make_bad_raw_data(n: int = 100) -> list[dict[str, object]]:
    """Return a MISCALIBRATED dataset (predicted 0.9 but label=0, predicted 0.1 but label=1)."""
    data: list[dict[str, object]] = []
    for i in range(n // 2):
        # High predicted confidence, actual label=0 — terrible calibration
        data.append(
            {
                "incident_id": f"INC-BAD-H{i:03d}",
                "predicted_confidence": 0.9,
                "true_label": 0,
                "timestamp": "2026-01-15T12:00:00Z",
            }
        )
    for i in range(n // 2):
        # Low predicted confidence, actual label=1 — terrible calibration
        data.append(
            {
                "incident_id": f"INC-BAD-L{i:03d}",
                "predicted_confidence": 0.1,
                "true_label": 1,
                "timestamp": "2026-01-15T12:00:00Z",
            }
        )
    return data


# ---------------------------------------------------------------------------
# Original pipeline tests (preserved)
# ---------------------------------------------------------------------------


def test_calculate_watermark_weight() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    # Future/current
    assert calculate_watermark_weight(as_of, as_of) == 1.0

    # 10 days ago
    valid_from = as_of - timedelta(days=10)
    expected_weight = math.exp(-LAMBDA_TIME_DECAY * 10)
    assert math.isclose(calculate_watermark_weight(valid_from, as_of), expected_weight)


def test_collect_labeled_incidents() -> None:
    raw_data: list[dict[str, object]] = [
        {
            "incident_id": "INC-001",
            "predicted_confidence": 0.8,
            "true_label": 1,
            "timestamp": "2026-01-01T12:00:00Z",
        },
        {
            "incident_id": "INC-002",
            "predicted_confidence": 0.3,
            "true_label": 0,
            "timestamp": datetime(2026, 1, 2, tzinfo=UTC),
        },
        {
            "incident_id": "INC-INVALID",
            # missing predicted_confidence
            "true_label": 1,
        },
    ]

    incidents = collect_labeled_incidents(raw_data)
    assert len(incidents) == 2
    assert incidents[0].incident_id == "INC-001"
    assert incidents[0].predicted_confidence == 0.8
    assert incidents[0].true_label == 1

    assert incidents[1].incident_id == "INC-002"
    assert incidents[1].timestamp == datetime(2026, 1, 2, tzinfo=UTC)


def test_compute_brier_score() -> None:
    predictions = [0.8, 0.2]
    labels = [1, 0]

    # Unweighted Brier: (0.8 - 1)^2 = 0.04; (0.2 - 0)^2 = 0.04
    # Mean = 0.04
    brier = compute_brier_score(predictions, labels)
    assert math.isclose(brier, 0.04)

    # Weighted Brier
    weights = [2.0, 1.0]
    # (2 * 0.04 + 1 * 0.04) / 3 = 0.04
    brier_weighted = compute_brier_score(predictions, labels, weights)
    assert math.isclose(brier_weighted, 0.04)


def test_compute_ece() -> None:
    # 10 bins, boundaries at 0.1, 0.2, ...
    # Bin 0.8: contains 0.8 -> avg conf 0.8. avg acc = 1.0. Error = 0.2
    # Bin 0.2: contains 0.2 -> avg conf 0.2. avg acc = 0.0. Error = 0.2
    predictions = [0.8, 0.2]
    labels = [1, 0]

    ece = compute_ece(predictions, labels, num_bins=10)
    assert math.isclose(ece, 0.2)


def test_fit_isotonic_regression() -> None:
    # Test PAVA
    predictions = [0.1, 0.4, 0.8]
    labels = [0, 0, 1]

    thresholds, values = fit_isotonic_regression(predictions, labels)
    # The isotonic mapping should perfectly fit this since it's monotonic
    assert thresholds == [0.0, 0.1, 0.4, 0.8, 1.0]
    assert values == [0.0, 0.0, 0.0, 1.0, 1.0]

    # Non-monotonic
    predictions = [0.1, 0.4, 0.8]
    labels = [0, 1, 0]
    # 0.1 -> 0
    # 0.4 -> 1
    # 0.8 -> 0
    # PAVA will merge 0.4 and 0.8 to avg value 0.5
    thresholds, values = fit_isotonic_regression(predictions, labels)
    assert values[1] == 0.0  # for 0.1
    # For 0.4 and 0.8 it should be 0.5
    idx_0_4 = thresholds.index(0.4)
    assert values[idx_0_4] == 0.5


def test_bootstrap_metrics() -> None:
    predictions = [0.8, 0.2, 0.9, 0.1]
    labels = [1, 0, 1, 0]
    weights = [1.0, 1.0, 1.0, 1.0]
    metrics = bootstrap_metrics(predictions, labels, weights, num_bootstraps=50)
    assert "brier_ci" in metrics
    assert "ece_ci" in metrics
    assert metrics["brier_ci"][0] <= metrics["brier_ci"][1]


def test_get_out_of_fold_predictions() -> None:
    predictions = [0.1, 0.4, 0.8, 0.9, 0.2]
    labels = [0, 0, 1, 1, 0]
    weights = [1.0, 1.0, 1.0, 1.0, 1.0]
    oof_preds = get_out_of_fold_predictions(predictions, labels, weights, k_folds=2)
    assert len(oof_preds) == 5
    for p in oof_preds:
        assert 0.0 <= p <= 1.0


def test_calibration_pipeline_fallback() -> None:
    raw_data: list[dict[str, object]] = [
        {
            "incident_id": "INC-001",
            "predicted_confidence": 0.9,
            "true_label": 1,
            "timestamp": "2026-01-01T12:00:00Z",
        },
        {
            "incident_id": "INC-002",
            "predicted_confidence": 0.1,
            "true_label": 0,
            "timestamp": "2026-01-02T12:00:00Z",
        },
    ]

    pipeline = CalibrationPipeline(as_of=datetime(2026, 1, 3, tzinfo=UTC), min_samples=10)
    result = pipeline.run(raw_data)

    assert result["mode"] == "uncalibrated"
    assert result["isotonic_thresholds"] == [0.0, 1.0]
    assert result["isotonic_values"] == [0.0, 1.0]
    assert result["brier_score"] < 0.05


def test_calibration_pipeline_calibrated() -> None:
    raw_data: list[dict[str, object]] = []
    # Create enough samples to trigger calibrated mode
    for i in range(15):
        raw_data.append({"incident_id": f"INC-A{i}", "predicted_confidence": 0.8, "true_label": 1})
        raw_data.append({"incident_id": f"INC-B{i}", "predicted_confidence": 0.2, "true_label": 0})

    pipeline = CalibrationPipeline(
        as_of=datetime(2026, 1, 3, tzinfo=UTC), min_samples=20, k_folds=3
    )
    result = pipeline.run(raw_data)

    assert result["mode"] == "calibrated"
    assert "brier_score" in result
    assert "brier_ci" in result
    assert "ece" in result
    assert "ece_ci" in result
    assert len(result["isotonic_thresholds"]) >= 2


# ---------------------------------------------------------------------------
# AP4 — Calibration artifact persistence tests
# ---------------------------------------------------------------------------


class TestCalibrationArtifactPersistence:
    """Verify save → load round-trip for the fitted calibration artifact."""

    def test_save_creates_json_file(self, tmp_path: Path) -> None:
        pipeline_result: dict[str, object] = {
            "isotonic_thresholds": [0.0, 0.3, 0.7, 1.0],
            "isotonic_values": [0.05, 0.28, 0.72, 0.95],
            "brier_score": 0.07,
            "ece": 0.04,
            "num_samples": 200,
            "mode": "calibrated",
        }
        artifact_path = save_calibration_artifact(
            pipeline_result=pipeline_result, artifact_dir=tmp_path
        )
        assert artifact_path.exists()
        raw = json.loads(artifact_path.read_text())
        assert raw["schema_version"] == 1
        assert raw["num_samples"] == 200
        assert raw["brier_score"] == 0.07
        assert raw["ece"] == 0.04
        assert raw["isotonic_thresholds"] == [0.0, 0.3, 0.7, 1.0]
        assert raw["isotonic_values"] == [0.05, 0.28, 0.72, 0.95]

    def test_load_returns_none_when_no_file(self, tmp_path: Path) -> None:
        result = load_calibration_artifact(artifact_dir=tmp_path)
        assert result is None

    def test_load_returns_artifact_after_save(self, tmp_path: Path) -> None:
        pipeline_result: dict[str, object] = {
            "isotonic_thresholds": [0.0, 0.4, 0.8, 1.0],
            "isotonic_values": [0.02, 0.40, 0.82, 0.98],
            "brier_score": 0.08,
            "ece": 0.03,
            "num_samples": 150,
            "mode": "calibrated",
        }
        save_calibration_artifact(pipeline_result=pipeline_result, artifact_dir=tmp_path)
        artifact = load_calibration_artifact(artifact_dir=tmp_path)

        assert artifact is not None
        assert isinstance(artifact, CalibrationArtifact)
        assert artifact.num_samples == 150
        assert artifact.brier_score == 0.08
        assert artifact.ece == 0.03
        assert artifact.isotonic_thresholds == [0.0, 0.4, 0.8, 1.0]
        assert artifact.isotonic_values == [0.02, 0.40, 0.82, 0.98]
        assert artifact.schema_version == 1
        assert artifact.fitted_at.tzinfo is not None

    def test_load_returns_none_on_malformed_json(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "calibration_artifact.json"
        bad_file.write_text("{not: valid json", encoding="utf-8")
        result = load_calibration_artifact(artifact_dir=tmp_path)
        assert result is None

    def test_load_returns_none_on_wrong_schema_version(self, tmp_path: Path) -> None:
        artifact_file = tmp_path / "calibration_artifact.json"
        artifact_file.write_text(
            json.dumps(
                {
                    "schema_version": 99,
                    "fitted_at": "2026-06-24T12:00:00+00:00",
                    "num_samples": 100,
                    "brier_score": 0.1,
                    "ece": 0.05,
                    "isotonic_thresholds": [0.0, 1.0],
                    "isotonic_values": [0.0, 1.0],
                }
            ),
            encoding="utf-8",
        )
        result = load_calibration_artifact(artifact_dir=tmp_path)
        assert result is None

    def test_round_trip_preserves_float_precision(self, tmp_path: Path) -> None:
        thresholds = [0.0, 0.123456789, 0.987654321, 1.0]
        values = [0.011111, 0.122222, 0.977777, 0.999999]
        pipeline_result: dict[str, object] = {
            "isotonic_thresholds": thresholds,
            "isotonic_values": values,
            "brier_score": 0.09,
            "ece": 0.045,
            "num_samples": 80,
        }
        save_calibration_artifact(pipeline_result=pipeline_result, artifact_dir=tmp_path)
        artifact = load_calibration_artifact(artifact_dir=tmp_path)
        assert artifact is not None
        assert math.isclose(artifact.isotonic_thresholds[1], thresholds[1], rel_tol=1e-9)
        assert math.isclose(artifact.isotonic_values[1], values[1], rel_tol=1e-9)


# ---------------------------------------------------------------------------
# AP4 — Live scoring path loads fitted artifact and returns calibrated=True
# ---------------------------------------------------------------------------


class TestLiveScoringLoadsArtifact:
    """Verify that when a fitted artifact exists on disk the live scoring path:
    (a) loads the map (``is_fitted_map_loaded() == True``), and
    (b) ``mcp_resolve_incident`` returns ``calibrated=True``.
    """

    def test_reload_fitted_map_returns_true_after_save(self, tmp_path: Path) -> None:
        """reload_fitted_map returns True when artifact exists."""
        pipeline_result: dict[str, object] = {
            "isotonic_thresholds": [0.0, 0.5, 1.0],
            "isotonic_values": [0.05, 0.50, 0.95],
            "brier_score": 0.10,
            "ece": 0.05,
            "num_samples": 100,
        }
        save_calibration_artifact(pipeline_result=pipeline_result, artifact_dir=tmp_path)
        loaded = reload_fitted_map(artifact_dir=tmp_path)
        assert loaded is True
        assert is_fitted_map_loaded() is True

    def test_reload_fitted_map_returns_false_when_no_artifact(self, tmp_path: Path) -> None:
        """reload_fitted_map returns False when no artifact file exists."""
        loaded = reload_fitted_map(artifact_dir=tmp_path)
        assert loaded is False
        assert is_fitted_map_loaded() is False

    @pytest.mark.asyncio
    async def test_mcp_resolve_incident_calibrated_true_with_artifact(
        self, tmp_path: Path
    ) -> None:
        """mcp_resolve_incident returns calibrated=True when a fitted artifact is loaded."""
        # 1. Save a valid artifact.
        pipeline_result: dict[str, object] = {
            "isotonic_thresholds": [0.0, 0.2, 0.5, 0.8, 1.0],
            "isotonic_values": [0.02, 0.18, 0.50, 0.80, 0.98],
            "brier_score": 0.09,
            "ece": 0.04,
            "num_samples": 120,
        }
        save_calibration_artifact(pipeline_result=pipeline_result, artifact_dir=tmp_path)
        reload_fitted_map(artifact_dir=tmp_path)

        # 2. Build a minimal FastAPI app + fake graph store.
        from omniscience_server.incidents import mcp_resolve_incident

        alert_id = "alert://pagerduty/INC-AP4-TEST"
        workspace_id = uuid.UUID("aaaaaaaa-0000-0000-0000-000000004444")

        alert_node = EntityNodeView(
            id=uuid.uuid4(),
            name=alert_id,
            kind="alert",
            source="src-alerts",
            chunk_text="Alert for AP4 test",
            depth=0,
            edge_type=None,
            valid_from=datetime(2026, 6, 1, tzinfo=UTC),
        )

        class _FakeStore:
            async def get_entity(
                self,
                *,
                entity_name: str,
                workspace_id: uuid.UUID,
                as_of: datetime | None = None,
            ) -> EntityNodeView | None:
                return alert_node if entity_name == alert_id else None

            async def find_related(
                self,
                *,
                entity_name: str,
                workspace_id: uuid.UUID,
                as_of: datetime | None = None,
                max_depth: int = 1,
                edge_types: list[str] | None = None,
            ) -> GraphResultView:
                return GraphResultView(seed=alert_node, related=[], edges=[])

        app = FastAPI()
        app.state.graph_store = _FakeStore()
        # Wire a dummy session factory so _load_scoring_config returns None gracefully.
        session = AsyncMock()
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        app.state.db_session_factory = factory

        result = await mcp_resolve_incident(
            app=app,
            alert_id=alert_id,
            workspace_id=workspace_id,
        )

        # 3. Assert the calibrated flag is True.
        assert "calibrated" in result, "response must contain 'calibrated' key"
        assert result["calibrated"] is True, (
            f"expected calibrated=True, got calibrated={result['calibrated']}"
        )
        assert "resolution_confidence" in result
        confidence = result["resolution_confidence"]
        assert 0.0 <= confidence <= 1.0

    @pytest.mark.asyncio
    async def test_mcp_resolve_incident_calibrated_false_without_artifact(
        self, tmp_path: Path
    ) -> None:
        """mcp_resolve_incident returns calibrated=False when no artifact is loaded."""
        # Ensure no artifact in this tmp_path.
        reload_fitted_map(artifact_dir=tmp_path)
        assert is_fitted_map_loaded() is False

        from omniscience_server.incidents import mcp_resolve_incident

        alert_id = "alert://pagerduty/INC-AP4-NOCAL"
        workspace_id = uuid.UUID("bbbbbbbb-0000-0000-0000-000000004444")

        alert_node = EntityNodeView(
            id=uuid.uuid4(),
            name=alert_id,
            kind="alert",
            source="src-alerts",
            chunk_text="Uncalibrated alert",
            depth=0,
            edge_type=None,
            valid_from=datetime(2026, 6, 1, tzinfo=UTC),
        )

        class _FakeStore2:
            async def get_entity(
                self,
                *,
                entity_name: str,
                workspace_id: uuid.UUID,
                as_of: datetime | None = None,
            ) -> EntityNodeView | None:
                return alert_node if entity_name == alert_id else None

            async def find_related(
                self,
                *,
                entity_name: str,
                workspace_id: uuid.UUID,
                as_of: datetime | None = None,
                max_depth: int = 1,
                edge_types: list[str] | None = None,
            ) -> GraphResultView:
                return GraphResultView(seed=alert_node, related=[], edges=[])

        app = FastAPI()
        app.state.graph_store = _FakeStore2()
        session = AsyncMock()
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(return_value=session)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        app.state.db_session_factory = factory

        result = await mcp_resolve_incident(
            app=app,
            alert_id=alert_id,
            workspace_id=workspace_id,
        )

        assert "calibrated" in result
        assert result["calibrated"] is False, (
            f"expected calibrated=False without artifact, got {result['calibrated']}"
        )


# ---------------------------------------------------------------------------
# AP4 — Calibration gate logic (fail on bad, pass on good)
# ---------------------------------------------------------------------------


class TestCalibrationGateLogic:
    """Gate asserts: Brier <= 0.20 and ECE <= 0.10.

    These thresholds match the CI workflow.
    """

    BRIER_THRESHOLD: float = 0.20
    ECE_THRESHOLD: float = 0.10

    def _run_gate(self, raw_data: list[dict[str, object]]) -> tuple[float, float]:
        """Run the pipeline and return (brier, ece)."""
        pipeline = CalibrationPipeline(min_samples=50, k_folds=5)
        result = pipeline.run(raw_data)
        return float(result["brier_score"]), float(result["ece"])

    def test_gate_fails_on_miscalibrated_fixture(self) -> None:
        """Deliberately miscalibrated data (predicted high → label 0) exceeds thresholds."""
        raw_data = _make_bad_raw_data(n=100)
        brier, ece = self._run_gate(raw_data)
        # The miscalibrated data must exceed at least one threshold.
        assert brier > self.BRIER_THRESHOLD or ece > self.ECE_THRESHOLD, (
            f"Expected gate to FAIL on miscalibrated data, but brier={brier:.4f} "
            f"and ece={ece:.4f} both passed — fixture is not adversarial enough."
        )

    def test_gate_passes_on_well_calibrated_fixture(self) -> None:
        """Well-calibrated data stays within both thresholds."""
        raw_data = _make_good_raw_data(n_low=50, n_high=50)
        brier, ece = self._run_gate(raw_data)
        assert brier <= self.BRIER_THRESHOLD, (
            f"Brier {brier:.4f} > threshold {self.BRIER_THRESHOLD} on good fixture"
        )
        assert ece <= self.ECE_THRESHOLD, (
            f"ECE {ece:.4f} > threshold {self.ECE_THRESHOLD} on good fixture"
        )

    def test_committed_fixture_passes_gate(self) -> None:
        """The committed tests/fixtures/calibration_labeled.json passes the gate."""
        fixture_path = Path("tests/fixtures/calibration_labeled.json")
        if not fixture_path.exists():
            pytest.skip("calibration_labeled.json fixture not found")

        raw_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        brier, ece = self._run_gate(raw_data)
        assert brier <= self.BRIER_THRESHOLD, (
            f"Committed fixture Brier {brier:.4f} > {self.BRIER_THRESHOLD}"
        )
        assert ece <= self.ECE_THRESHOLD, f"Committed fixture ECE {ece:.4f} > {self.ECE_THRESHOLD}"


# ---------------------------------------------------------------------------
# AP4 — Reliability diagram
# ---------------------------------------------------------------------------


class TestReliabilityDiagram:
    def test_empty_input_returns_empty(self) -> None:
        result = compute_reliability_diagram([], [], num_bins=10)
        assert result == []

    def test_bins_are_contiguous(self) -> None:
        predictions = [0.05, 0.15, 0.25, 0.75, 0.85, 0.95]
        labels = [0, 0, 1, 1, 1, 1]
        bins = compute_reliability_diagram(predictions, labels, num_bins=10)
        assert len(bins) > 0
        for b in bins:
            assert "bin_lower" in b
            assert "bin_upper" in b
            assert "mean_predicted" in b
            assert "fraction_positive" in b
            assert "count" in b
            assert b["bin_lower"] < b["bin_upper"]
            assert 0.0 <= b["fraction_positive"] <= 1.0

    def test_perfectly_calibrated_diagonal(self) -> None:
        # 0.5 predicted, half positive
        predictions = [0.5] * 10
        labels = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
        bins = compute_reliability_diagram(predictions, labels, num_bins=10)
        assert len(bins) == 1
        assert math.isclose(bins[0]["fraction_positive"], 0.5)
        assert math.isclose(bins[0]["mean_predicted"], 0.5)
