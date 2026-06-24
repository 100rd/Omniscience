"""Persistence and loading of fitted isotonic calibration artifacts (AP4).

The calibration pipeline (``CalibrationPipeline.run()``) fits an isotonic
regression map (thresholds + values) from labeled incident data.  This module
provides two primitives:

- :func:`save_calibration_artifact` — serialise a fitted map to a versioned
  JSON file so the runtime can load it without re-fitting on every request.
- :func:`load_calibration_artifact` — load a previously saved artifact and
  return a :class:`CalibrationArtifact`.  Returns ``None`` when no artifact
  exists, letting the caller fall back to the compile-time defaults.

File format
-----------

The artifact is a single JSON file named ``calibration_artifact.json`` written
to ``artifact_dir`` (defaults to the location pointed to by the
``OMNISCIENCE_CALIBRATION_DIR`` env-var, falling back to a path relative to
the package when the env-var is absent).  The format is intentionally simple::

    {
      "schema_version": 1,
      "fitted_at": "2026-06-24T12:00:00+00:00",
      "num_samples": 1234,
      "brier_score": 0.07,
      "ece": 0.04,
      "isotonic_thresholds": [0.0, 0.2, 0.5, 0.8, 1.0],
      "isotonic_values":     [0.05, 0.18, 0.52, 0.79, 0.95]
    }

``calibrated: bool`` contract
------------------------------

The live scoring path sets ``calibrated=True`` ONLY when
:func:`load_calibration_artifact` returns a non-``None`` artifact.  When no
artifact exists (first boot, CI, new deployment before the offline fit job
runs) the system falls back to compile-time Platt defaults and
``calibrated=False``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARTIFACT_FILENAME: Final[str] = "calibration_artifact.json"
ARTIFACT_SCHEMA_VERSION: Final[int] = 1

#: Env-var that overrides the artifact directory.  When unset the default
#: is a ``calibration`` subdirectory next to this module's package.
_ENV_VAR: Final[str] = "OMNISCIENCE_CALIBRATION_DIR"


def _default_artifact_dir() -> Path:
    """Return the default artifact directory.

    Resolution order:
    1. ``OMNISCIENCE_CALIBRATION_DIR`` env-var (absolute or relative to cwd).
    2. ``<package_root>/calibration/`` where *package_root* is the directory
       containing this file's package (i.e. ``omniscience_retrieval/``).
    """
    env_override = os.environ.get(_ENV_VAR)
    if env_override:
        return Path(env_override)
    return Path(__file__).parent.parent / "calibration"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationArtifact:
    """An immutable snapshot of a fitted calibration map.

    Attributes
    ----------
    schema_version:
        File format version.  Currently always ``1``.
    fitted_at:
        UTC timestamp when the artifact was produced.
    num_samples:
        Number of labeled incidents used to fit the isotonic map.
    brier_score:
        Weighted Brier score measured on out-of-fold predictions at fit time.
    ece:
        Weighted ECE measured on out-of-fold predictions at fit time.
    isotonic_thresholds:
        Sorted input breakpoints for the piecewise linear map.
    isotonic_values:
        Output values at each breakpoint (same length as ``isotonic_thresholds``).
    """

    schema_version: int
    fitted_at: datetime
    num_samples: int
    brier_score: float
    ece: float
    isotonic_thresholds: list[float]
    isotonic_values: list[float]


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save_calibration_artifact(
    *,
    pipeline_result: dict[str, object],
    artifact_dir: Path | None = None,
) -> Path:
    """Serialise a :class:`CalibrationPipeline` result to a JSON artifact.

    Parameters
    ----------
    pipeline_result:
        The ``dict`` returned by ``CalibrationPipeline.run()``.  Must contain
        ``isotonic_thresholds``, ``isotonic_values``, ``brier_score``,
        ``ece``, and ``num_samples``.
    artifact_dir:
        Directory in which to write ``calibration_artifact.json``.  Created
        if absent.  Defaults to :func:`_default_artifact_dir`.

    Returns
    -------
    Path
        Absolute path of the written artifact file.
    """
    target_dir = artifact_dir or _default_artifact_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    thresholds = pipeline_result.get("isotonic_thresholds", [0.0, 1.0])
    values = pipeline_result.get("isotonic_values", [0.0, 1.0])
    brier = pipeline_result.get("brier_score", 1.0)
    ece = pipeline_result.get("ece", 1.0)
    num_samples = pipeline_result.get("num_samples", 0)

    payload: dict[str, object] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "fitted_at": datetime.now(UTC).isoformat(),
        "num_samples": num_samples,
        "brier_score": brier,
        "ece": ece,
        "isotonic_thresholds": thresholds,
        "isotonic_values": values,
    }

    artifact_path = target_dir / ARTIFACT_FILENAME
    artifact_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    log.info(
        "calibration_artifact_saved",
        path=str(artifact_path),
        num_samples=num_samples,
        brier_score=brier,
        ece=ece,
    )
    return artifact_path


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_calibration_artifact(
    artifact_dir: Path | None = None,
) -> CalibrationArtifact | None:
    """Load the most recently saved calibration artifact.

    Returns ``None`` (no error) when no artifact file exists — the caller
    should fall back to compile-time defaults and set ``calibrated=False``.

    Returns ``None`` also on parse errors, logging a warning so operators can
    investigate without crashing the scoring path.
    """
    target_dir = artifact_dir or _default_artifact_dir()
    artifact_path = target_dir / ARTIFACT_FILENAME

    if not artifact_path.exists():
        log.debug("calibration_artifact_not_found", path=str(artifact_path))
        return None

    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "calibration_artifact_load_failed",
            path=str(artifact_path),
            error=str(exc),
        )
        return None

    try:
        artifact = _parse_artifact(raw)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning(
            "calibration_artifact_parse_failed",
            path=str(artifact_path),
            error=str(exc),
        )
        return None

    log.info(
        "calibration_artifact_loaded",
        path=str(artifact_path),
        num_samples=artifact.num_samples,
        brier_score=artifact.brier_score,
        ece=artifact.ece,
        fitted_at=artifact.fitted_at.isoformat(),
    )
    return artifact


def _parse_artifact(raw: object) -> CalibrationArtifact:
    """Parse raw JSON dict into a :class:`CalibrationArtifact`.

    Raises ``ValueError`` / ``KeyError`` / ``TypeError`` on malformed input.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"expected dict, got {type(raw).__name__}")

    schema_version = int(raw["schema_version"])
    if schema_version != ARTIFACT_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {schema_version}")

    fitted_at_str = str(raw["fitted_at"])
    fitted_at = datetime.fromisoformat(fitted_at_str)
    if fitted_at.tzinfo is None:
        fitted_at = fitted_at.replace(tzinfo=UTC)

    num_samples = int(raw["num_samples"])
    brier_score = float(raw["brier_score"])
    ece = float(raw["ece"])

    thresholds_raw = raw["isotonic_thresholds"]
    values_raw = raw["isotonic_values"]
    if not isinstance(thresholds_raw, list) or not isinstance(values_raw, list):
        raise TypeError("isotonic_thresholds and isotonic_values must be lists")
    if len(thresholds_raw) != len(values_raw):
        raise ValueError("isotonic_thresholds and isotonic_values must have the same length")

    isotonic_thresholds = [float(v) for v in thresholds_raw]
    isotonic_values = [float(v) for v in values_raw]

    return CalibrationArtifact(
        schema_version=schema_version,
        fitted_at=fitted_at,
        num_samples=num_samples,
        brier_score=brier_score,
        ece=ece,
        isotonic_thresholds=isotonic_thresholds,
        isotonic_values=isotonic_values,
    )


# ---------------------------------------------------------------------------
# Reliability diagram
# ---------------------------------------------------------------------------


def compute_reliability_diagram(
    predictions: list[float],
    labels: list[int],
    num_bins: int = 10,
) -> list[dict[str, float]]:
    """Compute bin-wise predicted-vs-observed for a reliability diagram.

    Returns a list of dicts, one per non-empty bin, with keys:
    - ``bin_lower``, ``bin_upper``: bin boundaries in ``[0, 1]``.
    - ``mean_predicted``: average predicted probability in the bin.
    - ``fraction_positive``: fraction of positives (observed accuracy).
    - ``count``: number of samples in the bin.
    """
    if not predictions:
        return []

    bin_width = 1.0 / num_bins
    bins: list[list[float]] = [[] for _ in range(num_bins)]
    bin_labels: list[list[int]] = [[] for _ in range(num_bins)]

    for p, y in zip(predictions, labels, strict=True):
        b = min(int(p * num_bins), num_bins - 1)
        bins[b].append(p)
        bin_labels[b].append(y)

    result: list[dict[str, float]] = []
    for i in range(num_bins):
        if not bins[i]:
            continue
        count = len(bins[i])
        result.append(
            {
                "bin_lower": float(i * bin_width),
                "bin_upper": float((i + 1) * bin_width),
                "mean_predicted": sum(bins[i]) / count,
                "fraction_positive": sum(bin_labels[i]) / count,
                "count": float(count),
            }
        )
    return result


__all__ = [
    "ARTIFACT_FILENAME",
    "ARTIFACT_SCHEMA_VERSION",
    "CalibrationArtifact",
    "compute_reliability_diagram",
    "load_calibration_artifact",
    "save_calibration_artifact",
]
