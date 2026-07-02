"""AP4 conformance tests — confidence decimal suppression when uncalibrated.

AP4 invariant: when ``calibrated=False`` (no fitted artifact committed),
the live ``score_incident`` path must NOT return a decimal confidence
score.  Instead it must return a qualitative band (``low``/``medium``/
``high``) and a ``calibrated: bool = False`` flag IN the Pydantic schema
(not injected post-``model_dump``).

The calibrated numeric path is preserved for when a real fitted artifact
exists; the v0.1 ladder constants ``0.9/0.6/0.4/0.1`` must not be served
to users as if they were calibrated probabilities.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from omniscience_core.storage.graph import EntityNodeView, GraphResultView
from omniscience_retrieval.incidents.resolution import (
    BAND_HIGH_THRESHOLD,
    BAND_MEDIUM_THRESHOLD,
    CONFIDENCE_ALERT_ONLY,
    CONFIDENCE_PR_TEMPORAL_MATCH,
    CONFIDENCE_RESOURCE_ONLY,
    ResolveIncidentResponse,
    _score_to_band,
)
from omniscience_retrieval.probabilistic_scoring import is_fitted_map_loaded, reload_fitted_map
from omniscience_server.incidents import mcp_resolve_incident

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS = uuid.UUID("aaaaaaaa-0000-0000-0000-a40400000001")
_ALERT_ID = "alert://pagerduty/AP4-TEST-001"
_FIRED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_PR_MERGED_RECENT = _FIRED_AT - timedelta(hours=1)


def _make_alert_node() -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=_ALERT_ID,
        kind="alert",
        source="src-alerts",
        chunk_text="AP4 test alert",
        depth=0,
        edge_type=None,
        valid_from=_FIRED_AT,
    )


def _make_pr_node() -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name="https://github.com/test/repo/pull/1",
        kind="pull_request",
        source="src-github",
        chunk_text="AP4 test PR",
        depth=1,
        edge_type="DEPLOYED_BY",
        valid_from=_PR_MERGED_RECENT,
    )


class _FakeGraphStore:
    """Minimal fake graph store for AP4 conformance tests."""

    def __init__(self, alert: EntityNodeView, related: list[EntityNodeView]) -> None:
        self._alert = alert
        self._related = related

    async def get_entity(
        self, *, entity_name: str, workspace_id: uuid.UUID, as_of: Any | None = None
    ) -> EntityNodeView | None:
        if entity_name == self._alert.name:
            return self._alert
        return None

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: Any | None = None,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return GraphResultView(seed=self._alert, related=self._related, edges=[])


def _make_app(store: _FakeGraphStore) -> FastAPI:
    """Build a minimal FastAPI app with the fake store wired."""
    app = FastAPI()
    app.state.graph_store = store
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = factory
    return app


# ---------------------------------------------------------------------------
# Ensure no fitted artifact is loaded for uncalibrated path tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_artifact_cache(tmp_path: Path) -> Any:
    """Reset the probabilistic_scoring artifact cache to unloaded state."""
    reload_fitted_map(artifact_dir=tmp_path)
    assert not is_fitted_map_loaded(), "Artifact unexpectedly loaded in fixture setup"
    yield
    reload_fitted_map(artifact_dir=None)


# ---------------------------------------------------------------------------
# AP4 test 1: uncalibrated path returns band + calibrated=False, NOT decimal
# ---------------------------------------------------------------------------


def test_uncalibrated_path_returns_band_not_decimal() -> None:
    """AP4: when calibrated=False, confidence is None/absent; band is returned instead.

    The live score_incident path with config=None (no fitted artifact)
    must NOT return a decimal confidence; it must return confidence_band
    and calibrated=False in the schema.
    """
    alert = _make_alert_node()
    pr = _make_pr_node()
    store = _FakeGraphStore(alert=alert, related=[pr])
    app = _make_app(store)

    result = asyncio.run(
        mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS)
    )

    # Core invariant: no decimal confidence on uncalibrated path.
    assert result["calibrated"] is False, "Expected calibrated=False on uncalibrated path"
    assert result["resolution_confidence"] is None, (
        f"resolution_confidence must be None when calibrated=False; "
        f"got {result['resolution_confidence']!r}"
    )
    # Band must be a valid value.
    assert result["confidence_band"] in {"low", "medium", "high"}, (
        f"confidence_band must be low/medium/high; got {result['confidence_band']!r}"
    )


# ---------------------------------------------------------------------------
# AP4 test 2: calibrated field is in Pydantic schema, not injected post-dump
# ---------------------------------------------------------------------------


def test_calibrated_field_is_in_pydantic_schema_not_injected() -> None:
    """AP4: calibrated is a first-class Pydantic field, not post-model_dump injection.

    The ``calibrated`` boolean must be declared in the response model
    definition, not patched onto the dict after ``model_dump()``.
    """
    fields = ResolveIncidentResponse.model_fields
    assert "calibrated" in fields, (
        "calibrated must be a declared Pydantic field on ResolveIncidentResponse"
    )
    assert "confidence_band" in fields, (
        "confidence_band must be a declared Pydantic field on ResolveIncidentResponse"
    )
    # resolution_confidence must be Optional (float | None).
    conf_field = fields["resolution_confidence"]
    # The field must have default=None (i.e. it is optional).
    assert conf_field.default is None, (
        "resolution_confidence must have default=None on ResolveIncidentResponse"
    )


# ---------------------------------------------------------------------------
# AP4 test 3: fitted artifact path returns decimal confidence + calibrated=True
# ---------------------------------------------------------------------------


def test_fitted_artifact_path_returns_decimal_confidence(tmp_path: Path) -> None:
    """AP4: when a fitted artifact exists, the decimal confidence is returned.

    The isotonic-regression calibrated path must return a real decimal
    ``confidence`` value and ``calibrated=True`` in the schema.
    """
    from omniscience_retrieval.incidents.calibration_store import save_calibration_artifact

    # Create and save a minimal valid calibration artifact via the pipeline dict API.
    pipeline_result: dict[str, object] = {
        "isotonic_thresholds": [0.0, 0.5, 1.0],
        "isotonic_values": [0.05, 0.55, 0.95],
        "brier_score": 0.10,
        "ece": 0.05,
        "num_samples": 200,
    }
    save_calibration_artifact(pipeline_result=pipeline_result, artifact_dir=tmp_path)

    # Reload the module-level cache to pick up the new artifact.
    loaded = reload_fitted_map(artifact_dir=tmp_path)
    assert loaded, "Artifact should be loaded after save"
    assert is_fitted_map_loaded(), "is_fitted_map_loaded() should be True after reload"

    alert = _make_alert_node()
    pr = _make_pr_node()
    store = _FakeGraphStore(alert=alert, related=[pr])
    app = _make_app(store)

    result = asyncio.run(
        mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS)
    )

    # With a fitted artifact: decimal is surfaced, band is None.
    assert result["calibrated"] is True, (
        "Expected calibrated=True when a fitted artifact is loaded"
    )
    assert result["resolution_confidence"] is not None, (
        "resolution_confidence must be a decimal float when calibrated=True"
    )
    assert 0.0 <= result["resolution_confidence"] <= 1.0, (
        f"resolution_confidence must be in [0,1]; got {result['resolution_confidence']}"
    )
    assert result["confidence_band"] is None, "confidence_band must be None when calibrated=True"


# ---------------------------------------------------------------------------
# AP4 test 4: confidence_band values are valid
# ---------------------------------------------------------------------------


def test_confidence_band_values_are_valid() -> None:
    """AP4: confidence_band must be one of 'low', 'medium', 'high' or None.

    No other string values are permissible.
    """
    # Test the _score_to_band helper directly.
    assert _score_to_band(CONFIDENCE_PR_TEMPORAL_MATCH) == "high"  # 0.9 >= 0.55
    assert _score_to_band(0.6) == "high"  # 0.6 >= 0.55
    assert _score_to_band(CONFIDENCE_RESOURCE_ONLY) == "medium"  # 0.4 >= 0.25
    assert _score_to_band(CONFIDENCE_ALERT_ONLY) == "low"  # 0.1 < 0.25
    assert _score_to_band(0.0) == "low"

    # Verify the threshold constants match expectations.
    assert BAND_HIGH_THRESHOLD == 0.55
    assert BAND_MEDIUM_THRESHOLD == 0.25

    # All possible band values are in the allowed set.
    for score in [0.0, 0.1, 0.25, 0.4, 0.55, 0.6, 0.9, 1.0]:
        band = _score_to_band(score)
        assert band in {"low", "medium", "high"}, f"Unexpected band {band!r} for score {score}"


# ---------------------------------------------------------------------------
# AP4 test 5: ladder constants not exposed as calibrated decimals
# ---------------------------------------------------------------------------


def test_ladder_constants_not_exposed_to_users_as_calibrated() -> None:
    """AP4: the v0.1 ladder constants (0.9/0.6/0.4/0.1) are never returned
    as calibrated=True to users.

    If the ladder is used, calibrated must be False and confidence must be
    suppressed (None or absent).
    """
    # Run with empty artifact dir -> ladder is used -> calibrated must be False.
    alert = _make_alert_node()
    pr = _make_pr_node()
    store = _FakeGraphStore(alert=alert, related=[pr])
    app = _make_app(store)

    result = asyncio.run(
        mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS)
    )

    # The v0.1 ladder was used (no artifact) — calibrated must be False.
    assert result["calibrated"] is False
    # The decimal must be suppressed.
    assert result["resolution_confidence"] is None
    # Specifically, the raw value 0.9 (CONFIDENCE_PR_TEMPORAL_MATCH) must
    # NOT appear as resolution_confidence when calibrated=False.
    assert result["resolution_confidence"] != CONFIDENCE_PR_TEMPORAL_MATCH
