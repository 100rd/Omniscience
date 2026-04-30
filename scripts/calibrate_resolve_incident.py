#!/usr/bin/env python3
"""Offline calibration harness for ``resolve_incident`` (issue #155).

What this does
--------------

Reads a JSONL file of labelled incident outcomes, fits a 4-component
weight vector by coordinate descent on AUC, prints a calibration report,
and writes ``weights.json`` for the operator to apply via the admin
REST endpoint (``PUT /api/v1/admin/incidents/scoring``).

This is **offline-only** — it never talks to the production database
and never touches production traffic.  The fitted ``weights.json`` is
then applied by an operator out-of-band.

JSONL format
------------

Each line is a single labelled record::

    {
      "alert_id": "alert://pagerduty/INC-123",
      "workspace_id": "<uuid>",                 // for operator audit only
      "components": {
        "recency": 0.9,
        "graph_proximity": 1.0,
        "evidence_count": 0.66,
        "cross_ref_strength": 0.9
      },
      "was_correct": true                       // ground-truth label
    }

The ``components`` block is the per-record output of
:func:`omniscience_server.incidents_scoring.compute_components` — capture
it during a replay against ``resolve_incident`` (see runbook).  Each
record's ``workspace_id`` MUST match the calibrator's ``--workspace-id``
argument; otherwise the row is dropped (workspace isolation guard).

Algorithm
---------

Coordinate descent on AUC:

1. Start at equal weights ``{0.25, 0.25, 0.25, 0.25}``.
2. For each iteration:
   - For each component ``c`` and step ``±s`` over a small grid:
     * Build a perturbed weight vector, renormalise to sum-to-1.0,
       clip to [0, 1].
     * Score every record, compute AUC against the labels.
     * Keep the best perturbation.
3. Halve the step size when no improvement is found in a full pass.
4. Stop when the step size falls below ``1e-4`` or after ``--max-iters``.

The hand-rolled approach keeps zero new dependencies and is fully
deterministic given a seed (no seed needed — the search is exhaustive
over a fixed grid, so reproducibility is automatic).

Usage
-----

::

    uv run python scripts/calibrate_resolve_incident.py \\
        --jsonl tests/fixtures/incident_calibration.jsonl \\
        --workspace-id 0123abcd-... \\
        --output weights.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMPONENT_KEYS: tuple[str, ...] = (
    "recency",
    "graph_proximity",
    "evidence_count",
    "cross_ref_strength",
)

INITIAL_STEP: float = 0.1
MIN_STEP: float = 1e-4
DEFAULT_MAX_ITERS: int = 50


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelledRecord:
    """One labelled outcome — components + ground truth."""

    components: dict[str, float]
    was_correct: bool


@dataclass(frozen=True, slots=True)
class FitResult:
    """Output of :func:`fit_weights`."""

    weights: dict[str, float]
    auc: float
    iterations: int


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def load_jsonl(path: Path, workspace_id: str | None) -> list[LabelledRecord]:
    """Read the JSONL file, filtering rows that don't match ``workspace_id``."""
    records: list[LabelledRecord] = []
    with path.open() as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_no}: invalid JSON: {exc}") from exc
            if workspace_id is not None and row.get("workspace_id") != workspace_id:
                continue
            components = row.get("components")
            if not isinstance(components, dict):
                raise ValueError(f"line {line_no}: missing 'components' dict")
            normalised = {key: float(components.get(key, 0.0)) for key in COMPONENT_KEYS}
            records.append(
                LabelledRecord(
                    components=normalised,
                    was_correct=bool(row.get("was_correct", False)),
                )
            )
    return records


# ---------------------------------------------------------------------------
# AUC — pure, deterministic
# ---------------------------------------------------------------------------


def score_record(record: LabelledRecord, weights: dict[str, float]) -> float:
    """Linear combination — same formula as the live scorer."""
    return sum(record.components[k] * weights[k] for k in COMPONENT_KEYS)


def compute_auc(records: list[LabelledRecord], weights: dict[str, float]) -> float:
    """Compute the area under the ROC curve.

    Uses the rank-based estimator (equivalent to the Mann-Whitney U /
    Wilcoxon statistic) which avoids any numpy dependency.
    """
    scored = [(score_record(r, weights), r.was_correct) for r in records]
    pos = [s for s, label in scored if label]
    neg = [s for s, label in scored if not label]
    if not pos or not neg:
        return 0.5  # Degenerate: AUC is undefined; conservative fallback.
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


# ---------------------------------------------------------------------------
# Coordinate descent
# ---------------------------------------------------------------------------


def _normalise(weights: dict[str, float]) -> dict[str, float]:
    """Clip to ``[0, 1]`` and renormalise the sum to 1.0."""
    clipped = {k: max(0.0, min(1.0, weights[k])) for k in COMPONENT_KEYS}
    total = sum(clipped.values())
    if total <= 0.0:
        return {k: 1.0 / len(COMPONENT_KEYS) for k in COMPONENT_KEYS}
    return {k: clipped[k] / total for k in COMPONENT_KEYS}


def _perturb(weights: dict[str, float], component: str, delta: float) -> dict[str, float]:
    """Bump one component by ``delta`` and renormalise."""
    candidate = dict(weights)
    candidate[component] = candidate[component] + delta
    return _normalise(candidate)


def fit_weights(
    records: list[LabelledRecord],
    max_iters: int = DEFAULT_MAX_ITERS,
) -> FitResult:
    """Coordinate descent on AUC.  Deterministic and bounded."""
    weights = {k: 1.0 / len(COMPONENT_KEYS) for k in COMPONENT_KEYS}
    best_auc = compute_auc(records, weights)
    step = INITIAL_STEP
    iterations = 0
    while step >= MIN_STEP and iterations < max_iters:
        improved = False
        for component in COMPONENT_KEYS:
            for delta in (step, -step):
                candidate = _perturb(weights, component, delta)
                auc = compute_auc(records, candidate)
                if auc > best_auc + 1e-9:
                    weights = candidate
                    best_auc = auc
                    improved = True
        iterations += 1
        if not improved:
            step /= 2.0
    return FitResult(weights=weights, auc=best_auc, iterations=iterations)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(records: list[LabelledRecord], result: FitResult) -> str:
    """Plain-text summary, friendly for runbook paste-in."""
    pos = sum(1 for r in records if r.was_correct)
    neg = len(records) - pos
    lines = [
        "calibrate_resolve_incident — calibration report",
        "------------------------------------------------",
        f"  records:        {len(records)}  (pos={pos}, neg={neg})",
        f"  iterations:     {result.iterations}",
        f"  fitted AUC:     {result.auc:.4f}",
        "  weights:",
    ]
    for key in COMPONENT_KEYS:
        lines.append(f"    {key:<22} {result.weights[key]:.4f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fit per-tenant resolve_incident weights from labelled data.",
    )
    parser.add_argument(
        "--jsonl",
        required=True,
        type=Path,
        help="Path to the labelled JSONL file.",
    )
    parser.add_argument(
        "--workspace-id",
        type=str,
        default=None,
        help=(
            "Filter records to this workspace UUID (enforces workspace "
            "isolation in the calibration data)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("weights.json"),
        help="Where to write the fitted weights JSON.",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.6,
        help="Trust threshold to bundle alongside the fitted weights (default 0.6).",
    )
    parser.add_argument(
        "--max-iters",
        type=int,
        default=DEFAULT_MAX_ITERS,
        help=f"Coordinate-descent iteration cap (default {DEFAULT_MAX_ITERS}).",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    records = load_jsonl(args.jsonl, args.workspace_id)
    if not records:
        print("calibrate_resolve_incident: no records to fit on.", file=sys.stderr)
        return 2
    result = fit_weights(records, max_iters=args.max_iters)
    payload = {
        "weights": result.weights,
        "confidence_threshold": float(args.confidence_threshold),
        "auc": result.auc,
        "iterations": result.iterations,
        "n_records": len(records),
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(render_report(records, result))
    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
