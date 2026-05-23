"""Runbook -> alert matcher and confidence scoring (issue #231).

The matcher is intentionally pure: it takes a parsed
:class:`RunbookDocument` plus an :class:`AlertView` and returns a
:class:`MatchResult`.  No DB / graph access — the server's
``suggest_runbook`` handler loads candidates from the graph and feeds them
through this function in a loop.

Confidence formula (v0.1 placeholder; calibrated model is a follow-up
tracked under issue #231's "post-merge" thread)
================================================================

Confidence ∈ [0.0, 1.0] is computed as a weighted sum of feature scores:

    confidence = clamp(
        0.55 * exact_alert_name_match    # 1.0 if any alert_name == alert.name
      + 0.25 * pattern_match_score       # max fnmatch ratio over alert_patterns
      + 0.15 * tag_jaccard               # |fm.tags ∩ alert.tags| / |union|
      + 0.05 * severity_match,           # 1.0 if fm.severity == alert.severity
      0.0, 1.0,
    )

Monotonicity invariant (asserted in the integration test): adding more
matching tags or matching exact alert names to a runbook must produce a
weakly higher confidence — never lower.  The integration test fixtures are
constructed to make this trivially checkable.

Why these weights:
    - Exact match dominates because runbook authors who bother to list
      an alert by name *intend* that runbook to fire for that alert.
    - Pattern match is second because globs cover families
      (``alert://datadog/db.*``) — high precision when present.
    - Tag overlap is third — useful but noisier (tags like ``severity:sev2``
      match many runbooks).
    - Severity is a tie-breaker — never the primary signal.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, Field

from omniscience_connectors.runbook.models import RunbookDocument

__all__ = [
    "WEIGHT_EXACT",
    "WEIGHT_PATTERN",
    "WEIGHT_SEVERITY",
    "WEIGHT_TAGS",
    "AlertView",
    "MatchResult",
    "score_runbook_against_alert",
]


# Weights — exposed as constants for the admin telemetry surface and so the
# monotonicity test can lean on them rather than hard-coding "0.55".
WEIGHT_EXACT: Final[float] = 0.55
WEIGHT_PATTERN: Final[float] = 0.25
WEIGHT_TAGS: Final[float] = 0.15
WEIGHT_SEVERITY: Final[float] = 0.05

# A run-of-the-mill heuristic — if the runbook lists *zero* matching signals
# of any kind we round the confidence down to 0 so it never surfaces.
_MIN_SIGNAL_CONFIDENCE: Final[float] = 0.01


class AlertView(BaseModel):
    """Minimal alert projection the matcher needs.

    Built by the server from the alert entity + its bitemporal facts;
    decoupled from the full :class:`omniscience_core.storage.EntityNodeView`
    so the matcher can be unit-tested without a graph store.
    """

    name: str = Field(description="Canonical alert name, e.g. alert://datadog/db.conn")
    tags: list[str] = Field(default_factory=list)
    severity: str | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Per-runbook scoring trace returned by :func:`score_runbook_against_alert`.

    The handler returns the top N of these to the caller so the responder
    can see *why* the suggestion ranked where it did.
    """

    runbook_name: str
    confidence: float
    exact_match: bool
    pattern_match: str | None
    matched_tags: tuple[str, ...]
    severity_match: bool

    def to_payload(self) -> dict[str, object]:
        """JSON-serialisable representation for the REST/MCP wire."""
        return {
            "runbook_name": self.runbook_name,
            "confidence": round(self.confidence, 4),
            "exact_match": self.exact_match,
            "pattern_match": self.pattern_match,
            "matched_tags": list(self.matched_tags),
            "severity_match": self.severity_match,
        }


def _pattern_score(patterns: list[str], alert_name: str) -> tuple[float, str | None]:
    """Return the best fnmatch ratio (0/1) and the pattern that produced it.

    ``fnmatch`` is binary — a glob either matches or it doesn't — so the
    "ratio" is 1.0 on first hit, 0.0 otherwise.  We keep the float surface
    for symmetry with the other features and so a calibrated v0.2 model
    (e.g. cosine similarity) can drop in without changing the signature.
    """
    for pattern in patterns:
        if fnmatch.fnmatchcase(alert_name, pattern):
            return 1.0, pattern
    return 0.0, None


def _tag_jaccard(rb_tags: list[str], alert_tags: list[str]) -> tuple[float, tuple[str, ...]]:
    """Return Jaccard similarity over tag sets plus the intersection.

    Empty alert-tags set yields 0.0 (no signal); empty runbook-tags set
    likewise.  Tags are matched case-sensitively — the connector authors
    can normalise upstream if they want case-insensitive semantics.
    """
    if not rb_tags or not alert_tags:
        return 0.0, ()
    rb_set = set(rb_tags)
    alert_set = set(alert_tags)
    intersection = rb_set & alert_set
    if not intersection:
        return 0.0, ()
    union = rb_set | alert_set
    jaccard = len(intersection) / len(union)
    # Stable ordering for deterministic test assertions.
    return jaccard, tuple(sorted(intersection))


def score_runbook_against_alert(
    *,
    runbook: RunbookDocument,
    alert: AlertView,
) -> MatchResult:
    """Compute the v0.1 confidence score for one runbook / alert pair.

    Pure function — no IO, no global state.  Idempotent; the same inputs
    always produce the same MatchResult.  Safe to call inside a hot loop;
    the algorithm is O(|alert_patterns| + |runbook.tags| + |alert.tags|).
    """
    fm = runbook.front_matter

    exact_match = alert.name in set(fm.alert_names)
    exact_score = 1.0 if exact_match else 0.0

    pattern_score, pattern_hit = _pattern_score(fm.alert_patterns, alert.name)

    tag_score, matched_tags = _tag_jaccard(fm.tags, alert.tags)

    severity_match = (
        fm.severity is not None and alert.severity is not None and fm.severity == alert.severity
    )
    severity_score = 1.0 if severity_match else 0.0

    confidence = (
        WEIGHT_EXACT * exact_score
        + WEIGHT_PATTERN * pattern_score
        + WEIGHT_TAGS * tag_score
        + WEIGHT_SEVERITY * severity_score
    )
    # Clamp to [0, 1] defensively even though the weights sum to 1.0; a
    # future tweak that pushes a feature above 1.0 should not silently
    # produce a malformed confidence.
    confidence = max(0.0, min(1.0, confidence))
    if confidence < _MIN_SIGNAL_CONFIDENCE:
        confidence = 0.0

    return MatchResult(
        runbook_name=runbook.canonical_name,
        confidence=confidence,
        exact_match=exact_match,
        pattern_match=pattern_hit,
        matched_tags=matched_tags,
        severity_match=severity_match,
    )
