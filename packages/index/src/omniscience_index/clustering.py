"""Same-incident clustering (issue #233, Wave 2 of epic #230 / H5 Track 1).

Pure, side-effect-free signature + similarity primitives for incident
clustering.  The MCP/REST surface lives in ``apps/server``; this
module owns only the deterministic, easily-unit-testable building
blocks so the ranking algorithm can evolve independently of the
auth / transport layer.

Design contract
---------------

1. **Signature** — :func:`compute_signature` returns a stable SHA-256
   hex digest over the canonical triple ``(alert_type, service,
   symptom_text_normalised)``.  Two alerts with the same triple produce
   byte-identical signatures across runs and processes; that is the
   "we saw this exact thing before" identity.

2. **Symptom normalisation** — :func:`normalise_symptom` strips IDs,
   timestamps, and host-specific tokens so the same root cause on two
   different pods collapses to the same string.  Without this the
   signature is too tight to ever match.

3. **Similarity** — :func:`score_similarity` returns a calibrated
   score in ``[0.0, 1.0]`` from the structured features of two
   incidents.  The formula is documented inline; callers can use the
   per-component values surfaced in :class:`SimilarityBreakdown` for
   diagnostics without re-deriving them.

4. **Recency decay** — :func:`recency_multiplier` produces an
   exponential decay (1.0 at age 0, ~0.37 at the ``since_days``
   boundary) so a 90-day-old match outranks no fresh match, but a
   2-day-old match outranks a 60-day-old match of equal structural
   similarity.

5. **No I/O.**  All inputs are plain dataclasses.  This keeps the
   module straightforward to reason about and free from FastAPI /
   GraphStore / DB coupling — the same primitives can later power
   batch clustering jobs.

Issue #240 (Datadog connector) hook
-----------------------------------

Datadog events now land as bitemporal entities in the graph.  Their
``provider`` (``datadog``) and ``monitor_id`` make :func:`extract_alert_type`
richer — see the docstring for the precedence rules — but the function
is connector-agnostic and degrades cleanly when only the canonical
``alert://provider/id`` name is available.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

# ---------------------------------------------------------------------------
# Public constants — exposed so tests can pin behaviour at the boundaries
# ---------------------------------------------------------------------------

#: Component weights used by :func:`score_similarity`.  They sum to 1.0
#: so the pre-decay structural score also lies in ``[0.0, 1.0]``.
WEIGHT_ALERT_TYPE: Final[float] = 0.40
WEIGHT_SERVICE: Final[float] = 0.30
WEIGHT_SYMPTOM_COSINE: Final[float] = 0.30

#: Bonus added when both incidents share an identical signature hash.
#: Capped by the post-multiplication clamp so the maximum returned
#: score stays at 1.0.
SIGNATURE_MATCH_BONUS: Final[float] = 0.20

#: Window beyond which a candidate's age is clamped (i.e. treated as
#: "as old as the search window").  Mirrors the MCP ``since_days``
#: parameter so old candidates stop dominating against newer noise.
DEFAULT_SINCE_DAYS: Final[int] = 90

#: Maximum number of similar incidents the MCP tool returns by default.
DEFAULT_LIMIT: Final[int] = 5

#: Minimum similarity score below which a candidate is dropped before
#: ranking.  Set deliberately low so the ranking does the heavy lifting
#: — callers that want a stricter floor can post-filter on the
#: returned ``score`` field.
MIN_SIMILARITY_FLOOR: Final[float] = 0.05

#: Number of bytes of the SHA-256 digest exposed in the public
#: signature string.  16 bytes (128 bits) gives ~6e-20 collision
#: probability across 1e10 incidents — far beyond any realistic load
#: while keeping log lines compact.
SIGNATURE_DIGEST_BYTES: Final[int] = 16

# ---------------------------------------------------------------------------
# Symptom-normalisation patterns
# ---------------------------------------------------------------------------

#: Hex blobs of 8+ characters (UUIDs, trace ids, container ids).
_HEX_BLOB_RE: Final[re.Pattern[str]] = re.compile(r"\b[0-9a-fA-F]{8,}\b")

#: ISO-8601 timestamps (subset — fast path covers the common variants).
_ISO_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)

#: Standalone integers >=4 digits — port numbers, counts, error codes.
#: Three-digit numbers (HTTP status etc.) are left in because they carry
#: signal: a 503 and a 500 are different root symptoms.
_LONG_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{4,}\b")

#: Whitespace collapse — single space output everywhere.
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Pod-id-style trailing token like ``api-7f9b3``.  We keep the prefix
#: (``api``) and drop the suffix so two restarts of the same workload
#: collapse to the same string.
_POD_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[a-zA-Z])-[0-9a-z]{4,}\b")

# ---------------------------------------------------------------------------
# Data classes — keep the API explicit, no ad-hoc dicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncidentFeatures:
    """The minimum feature vector a single incident contributes.

    All fields are optional except ``incident_id`` and ``alert_type``;
    missing fields collapse to neutral matchers (0.0 for embeddings,
    "" for strings).  This keeps the scorer resilient to partial data
    — e.g. a webhook alert without a target service still produces a
    usable signature on the alert-type axis alone.
    """

    incident_id: str
    alert_type: str
    service: str | None = None
    symptom_text: str | None = None
    symptom_embedding: tuple[float, ...] | None = None
    fired_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SimilarityBreakdown:
    """Per-component similarity breakdown surfaced for diagnostics.

    Returned alongside the scalar score so callers (REST / MCP) can
    expose the "why does this match?" trace without recomputing
    anything.  The components are pre-decay; the final returned
    ``score`` is post-decay and post-bonus, both clamped to ``[0, 1]``.
    """

    alert_type_match: float
    service_match: float
    symptom_cosine: float
    signature_match: bool
    recency_multiplier: float
    score: float


@dataclass(slots=True)
class SimilarIncidentCandidate:
    """Composed candidate ready for ranking + payload assembly.

    The MCP/REST layer wraps this into its own response model — keeping
    the wire schema in the apps layer means the clustering module never
    needs to know about Pydantic / FastAPI.
    """

    features: IncidentFeatures
    signature: str
    breakdown: SimilarityBreakdown
    extras: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public functions — signature
# ---------------------------------------------------------------------------


def normalise_symptom(text: str | None) -> str:
    """Collapse a symptom string to a stable, low-cardinality form.

    Drops timestamps, hex blobs, long numbers, and pod-id-style trailing
    tokens so the same root cause across different hosts / pods / runs
    produces an identical normalised string.  Lowercases and collapses
    whitespace as the final step.

    Returns the empty string for ``None`` or whitespace-only inputs —
    a deterministic "no symptom" signal the signature function combines
    safely with the other axes.
    """
    if text is None:
        return ""
    cleaned = text.strip()
    if not cleaned:
        return ""
    cleaned = _ISO_TIMESTAMP_RE.sub("<ts>", cleaned)
    cleaned = _HEX_BLOB_RE.sub("<hex>", cleaned)
    cleaned = _POD_SUFFIX_RE.sub("", cleaned)
    cleaned = _LONG_NUMBER_RE.sub("<n>", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip().lower()
    return cleaned


def compute_signature(
    *,
    alert_type: str,
    service: str | None,
    symptom_text: str | None,
) -> str:
    """Stable SHA-256 signature over the canonical triple.

    Returns a hex digest of :data:`SIGNATURE_DIGEST_BYTES` bytes.  The
    same three inputs in the same workspace ALWAYS produce the same
    hash — no clocks, no salt, no per-run state.  Two incidents with
    matching signatures are the textbook "we saw this exact thing
    before" case.

    The symptom is fed through :func:`normalise_symptom` first so the
    signature is resilient to timestamps / IDs / pod-name churn — see
    the module docstring for the rationale.
    """
    canonical = "|".join(
        [
            (alert_type or "").strip().lower(),
            (service or "").strip().lower(),
            normalise_symptom(symptom_text),
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return digest[:SIGNATURE_DIGEST_BYTES].hex()


def extract_alert_type(*, canonical_name: str, kind: str | None = None) -> str:
    """Pull the alert-type token from a canonical alert URI.

    Precedence (degrades cleanly when fields are missing):

    1. The provider segment of ``alert://{provider}/{provider_alert_id}``.
       Always present for connector-emitted alerts.
    2. The entity ``kind`` (e.g. ``"alert"`` from the alerts connector
       or ``"datadog_event"`` from #240).  Used when the canonical
       name doesn't parse — defensive fallback only.
    3. ``"unknown"`` so the signature still computes.
    """
    if canonical_name.startswith("alert://"):
        suffix = canonical_name[len("alert://") :]
        provider, _, _ = suffix.partition("/")
        if provider.strip():
            return provider.strip().lower()
    if kind:
        return kind.strip().lower()
    return "unknown"


# ---------------------------------------------------------------------------
# Public functions — similarity
# ---------------------------------------------------------------------------


def cosine_similarity(
    a: tuple[float, ...] | None,
    b: tuple[float, ...] | None,
) -> float:
    """Cosine similarity in ``[0.0, 1.0]`` — sign-clipped for ranking.

    Returns 0.0 when either side is missing or zero-norm so the score
    contribution is "no signal" rather than "actively contradicting".
    Negative cosines are clipped to 0.0; for the symptom-embedding use
    case a contradictory text doesn't reduce similarity, it just
    contributes no boost.
    """
    if a is None or b is None:
        return 0.0
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    cosine = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    if cosine < 0.0:
        return 0.0
    if cosine > 1.0:
        return 1.0
    return cosine


def recency_multiplier(
    *,
    fired_at: datetime | None,
    now: datetime,
    since_days: int,
) -> float:
    """Exponential decay over the candidate's age, in ``(0.0, 1.0]``.

    Formula: ``exp(-age_days / since_days)`` clipped at the window
    boundary.  Returns 1.0 when the candidate has no ``fired_at`` (we
    cannot decay what we cannot date — neutral multiplier).

    At ``age_days == since_days`` the multiplier is ``1/e ~= 0.368``;
    at ``age_days == 2 * since_days`` it's ~0.135.  The decay is gentle
    enough that a strong structural match still beats a weaker but
    recent one, while preserving the "fresher wins on ties" intuition.
    """
    if fired_at is None:
        return 1.0
    if since_days <= 0:
        return 1.0
    age_seconds = (now - fired_at).total_seconds()
    if age_seconds <= 0:
        return 1.0
    age_days = age_seconds / 86400.0
    decay = math.exp(-age_days / float(since_days))
    if decay < 0.0:
        return 0.0
    if decay > 1.0:
        return 1.0
    return decay


def score_similarity(
    *,
    seed: IncidentFeatures,
    candidate: IncidentFeatures,
    now: datetime,
    since_days: int = DEFAULT_SINCE_DAYS,
) -> SimilarityBreakdown:
    """Score a candidate against a seed; returns the full breakdown.

    Formula
    -------
    ``raw = w_type * type_eq + w_service * service_eq + w_cosine * cosine``
    ``decayed = raw * recency_multiplier``
    ``boosted = min(1.0, decayed + (BONUS if signature_match else 0))``

    Where ``type_eq`` and ``service_eq`` are 1.0 on case-insensitive
    string equality and 0.0 otherwise; ``cosine`` is the symptom
    embedding cosine (0.0 when either side is missing).  The recency
    multiplier and signature bonus apply on top so identical-triple
    matches always rank first, but a fresh near-match still outranks
    a stale exact match if the freshness gap is wide enough.
    """
    type_eq = 1.0 if _ieq(seed.alert_type, candidate.alert_type) else 0.0
    service_eq = 1.0 if _ieq(seed.service, candidate.service) else 0.0
    cosine = cosine_similarity(seed.symptom_embedding, candidate.symptom_embedding)
    raw = (
        WEIGHT_ALERT_TYPE * type_eq + WEIGHT_SERVICE * service_eq + WEIGHT_SYMPTOM_COSINE * cosine
    )
    decay = recency_multiplier(
        fired_at=candidate.fired_at,
        now=now,
        since_days=since_days,
    )
    decayed = raw * decay

    seed_sig = compute_signature(
        alert_type=seed.alert_type,
        service=seed.service,
        symptom_text=seed.symptom_text,
    )
    cand_sig = compute_signature(
        alert_type=candidate.alert_type,
        service=candidate.service,
        symptom_text=candidate.symptom_text,
    )
    signature_match = seed_sig == cand_sig
    final = decayed + (SIGNATURE_MATCH_BONUS if signature_match else 0.0)
    if final > 1.0:
        final = 1.0
    if final < 0.0:
        final = 0.0
    return SimilarityBreakdown(
        alert_type_match=type_eq,
        service_match=service_eq,
        symptom_cosine=cosine,
        signature_match=signature_match,
        recency_multiplier=decay,
        score=final,
    )


def rank_candidates(
    *,
    seed: IncidentFeatures,
    candidates: list[IncidentFeatures],
    now: datetime,
    since_days: int = DEFAULT_SINCE_DAYS,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SIMILARITY_FLOOR,
) -> list[SimilarIncidentCandidate]:
    """Score, filter, and rank candidates against a seed incident.

    Excludes the seed itself (by ``incident_id`` equality), drops any
    candidate scoring below ``min_score``, sorts the survivors by
    score descending with ``incident_id`` as the stable tie-breaker,
    and truncates at ``limit``.

    Determinism: same inputs always produce the same ordering — the
    tie-breaker ensures sort stability even when scores collide
    exactly (which they do for two candidates with identical features).
    """
    if limit <= 0:
        return []
    scored: list[SimilarIncidentCandidate] = []
    for candidate in candidates:
        if candidate.incident_id == seed.incident_id:
            continue
        breakdown = score_similarity(
            seed=seed,
            candidate=candidate,
            now=now,
            since_days=since_days,
        )
        if breakdown.score < min_score:
            continue
        signature = compute_signature(
            alert_type=candidate.alert_type,
            service=candidate.service,
            symptom_text=candidate.symptom_text,
        )
        scored.append(
            SimilarIncidentCandidate(
                features=candidate,
                signature=signature,
                breakdown=breakdown,
            )
        )
    scored.sort(key=lambda c: (-c.breakdown.score, c.features.incident_id))
    return scored[:limit]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ieq(a: str | None, b: str | None) -> bool:
    """Case-insensitive equality that treats ``None`` as "no value"."""
    if a is None or b is None:
        return False
    return a.strip().lower() == b.strip().lower()


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_SINCE_DAYS",
    "MIN_SIMILARITY_FLOOR",
    "SIGNATURE_DIGEST_BYTES",
    "SIGNATURE_MATCH_BONUS",
    "WEIGHT_ALERT_TYPE",
    "WEIGHT_SERVICE",
    "WEIGHT_SYMPTOM_COSINE",
    "IncidentFeatures",
    "SimilarIncidentCandidate",
    "SimilarityBreakdown",
    "compute_signature",
    "cosine_similarity",
    "extract_alert_type",
    "normalise_symptom",
    "rank_candidates",
    "recency_multiplier",
    "score_similarity",
]
