"""Horizontal-read qualification harness (issue #350, AC-SCALE-5).

A free-standing, framework-free scoring module — deliberately not under
`scripts/` (out of this task's scope). Every check is a pure function over
explicit *measured* inputs (synthetic in CI, real observations from a live
multi-replica run when one exists) and returns a frozen `VerificationResult`
— GREEN or RED, never raised, never a fabricated pass on a missing input.

Two or more replicas must pass all four probes — skew, isolation, fairness,
fallback — before a topology may claim HA read-scaling; a single-replica
profile (or one with any unmeasured dimension) is always RED, mirroring
AC-SCALE-5's "single-replica or unmeasured profiles remain non-HA".

`build_qualification_report` assembles a content-addressed
`QualificationReport`, the same self-addressing-sha256 pattern as
`scripts/qualify_backup_restore.py`'s `DrillManifest` (not imported from
there — `scripts/**` is out of this task's scope — the small pattern is
reproduced locally instead of reaching across the boundary).

**Live 2+-replica measurement is a decision-return** (no Kubernetes cluster
in this control-plane checkout): see
`docs/runbooks/read-admission.md` "Blocked on external — decision returns".
This module proves the scoring *logic* is correct against synthetic and
fixture inputs; it does not and cannot fabricate a live measurement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omniscience_core.config import Settings


@dataclass(frozen=True)
class VerificationResult:
    """A decision return — never an exception, always GREEN or RED."""

    status: str  # "GREEN" | "RED"
    reasons: tuple[str, ...] = ()

    @property
    def is_green(self) -> bool:
        return self.status == "GREEN"


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


# ---------------------------------------------------------------------------
# Skew probe
# ---------------------------------------------------------------------------


def score_skew(
    *,
    replica_admitted_counts: Mapping[str, int],
    max_skew_ratio: float,
) -> VerificationResult:
    """Verify admitted-request counts are balanced across replicas.

    ``replica_admitted_counts`` maps replica identity to the number of
    requests it admitted under a uniform synthetic load. Skew is
    ``(max - min) / mean``; RED if it exceeds ``max_skew_ratio`` or if
    fewer than two replicas were measured (a single replica cannot
    demonstrate horizontal balance at all).
    """
    if len(replica_admitted_counts) < 2:
        return VerificationResult(
            "RED", (f"fewer_than_2_replicas_measured:{len(replica_admitted_counts)}",)
        )
    counts = list(replica_admitted_counts.values())
    if any(c < 0 for c in counts):
        return VerificationResult("RED", ("negative_admitted_count",))
    mean = sum(counts) / len(counts)
    if mean <= 0:
        return VerificationResult("RED", ("zero_or_negative_mean_admitted_count",))
    skew_ratio = (max(counts) - min(counts)) / mean
    if skew_ratio > max_skew_ratio:
        return VerificationResult(
            "RED", (f"skew_ratio_exceeded:{skew_ratio:.4f}>{max_skew_ratio}",)
        )
    return VerificationResult("GREEN")


# ---------------------------------------------------------------------------
# Isolation probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantObservation:
    """One tenant's measured admitted/rejected counts under the probe."""

    tenant_id: str
    requests_sent: int
    requests_admitted: int


def score_isolation(
    *,
    observations: Sequence[TenantObservation],
    noisy_tenant_id: str,
    max_cross_tenant_rejection_rate: float,
) -> VerificationResult:
    """Verify a saturating tenant does not degrade other tenants' admission.

    ``observations`` must include the ``noisy_tenant_id`` (a tenant driving
    enough load to exhaust its own budget) plus at least one other tenant.
    RED if any non-noisy tenant's rejection rate exceeds
    ``max_cross_tenant_rejection_rate`` — that is cross-tenant interference,
    which a correctly isolated admission authority must not allow.
    """
    by_tenant = {obs.tenant_id: obs for obs in observations}
    if noisy_tenant_id not in by_tenant:
        return VerificationResult("RED", ("noisy_tenant_not_observed",))
    others = [obs for obs in observations if obs.tenant_id != noisy_tenant_id]
    if not others:
        return VerificationResult("RED", ("no_other_tenant_observed",))

    reasons: list[str] = []
    for obs in others:
        if obs.requests_sent <= 0:
            reasons.append(f"tenant_sent_zero_requests:{obs.tenant_id}")
            continue
        rejection_rate = 1.0 - (obs.requests_admitted / obs.requests_sent)
        if rejection_rate > max_cross_tenant_rejection_rate:
            reasons.append(
                f"cross_tenant_interference:{obs.tenant_id}:"
                f"{rejection_rate:.4f}>{max_cross_tenant_rejection_rate}"
            )
    if reasons:
        return VerificationResult("RED", tuple(reasons))
    return VerificationResult("GREEN")


# ---------------------------------------------------------------------------
# Fairness probe (AC-SCALE-2 — background degrades first)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneObservation:
    """One lane's measured behaviour under an overload probe."""

    lane: str
    requests_sent: int
    requests_admitted: int
    p99_latency_ms: float
    error_rate: float


def score_fairness(
    *,
    incident: LaneObservation,
    background: LaneObservation,
    incident_latency_threshold_ms: float,
    incident_error_rate_threshold: float,
) -> VerificationResult:
    """Verify overload sheds background traffic before degrading incident.

    RED if the incident lane breaches its own latency/error thresholds
    (meaning incident traffic was degraded — the one thing AC-SCALE-2
    forbids), or if background did not shed a strictly larger fraction of
    its own traffic than incident did (background must degrade *first*,
    not merely also-eventually).
    """
    reasons: list[str] = []
    if incident.p99_latency_ms > incident_latency_threshold_ms:
        reasons.append(
            f"incident_latency_breached:{incident.p99_latency_ms}>{incident_latency_threshold_ms}"
        )
    if incident.error_rate > incident_error_rate_threshold:
        reasons.append(
            f"incident_error_rate_breached:{incident.error_rate}>{incident_error_rate_threshold}"
        )

    def _rejection_rate(obs: LaneObservation) -> float | None:
        if obs.requests_sent <= 0:
            return None
        return 1.0 - (obs.requests_admitted / obs.requests_sent)

    incident_rejection = _rejection_rate(incident)
    background_rejection = _rejection_rate(background)
    if incident_rejection is None or background_rejection is None:
        reasons.append("lane_sent_zero_requests")
    elif background_rejection <= incident_rejection:
        reasons.append(
            f"background_did_not_degrade_first:"
            f"background_rejection={background_rejection:.4f}<=incident_rejection={incident_rejection:.4f}"
        )

    if reasons:
        return VerificationResult("RED", tuple(reasons))
    return VerificationResult("GREEN")


def score_fairness_from_settings(
    settings: Settings,
    *,
    incident: LaneObservation,
    background: LaneObservation,
) -> VerificationResult:
    """`score_fairness`, sourcing its thresholds from
    `Settings.admission_latency_threshold_ms`/`admission_error_rate_threshold`
    instead of a caller-supplied constant.

    These two fields are declared as fail-boot-required Settings inputs
    (`packages/core/src/omniscience_core/config.py`) but had no runtime
    consumer before this wiring — this is that consumer. RED (not raised)
    if either threshold is unset, matching this module's decision-return
    convention (module docstring)."""
    latency_threshold = settings.admission_latency_threshold_ms
    error_rate_threshold = settings.admission_error_rate_threshold
    if latency_threshold is None or error_rate_threshold is None:
        return VerificationResult(
            "RED",
            ("admission_latency_threshold_ms_or_admission_error_rate_threshold_unset",),
        )
    return score_fairness(
        incident=incident,
        background=background,
        incident_latency_threshold_ms=latency_threshold,
        incident_error_rate_threshold=error_rate_threshold,
    )


# ---------------------------------------------------------------------------
# Queue-depth probe (AC-SCALE-2/5 — bounded admission queue ceiling)
# ---------------------------------------------------------------------------


def score_queue_depth(*, observed_queue_depth: int, queue_depth_max: int) -> VerificationResult:
    """Verify an observed admission queue-depth measurement stays within
    `queue_depth_max` (`Settings.admission_queue_depth_max`, AC-SCALE-2/5).

    This process never queues admission requests itself (`try_admit` is a
    synchronous accept/reject — see `admission.process_local` module
    docstring); `observed_queue_depth` is a measurement taken against the
    shared backend/qualification probe, not something computed locally.
    RED if the observation exceeds the bound or is negative.
    """
    if observed_queue_depth < 0:
        return VerificationResult("RED", ("negative_observed_queue_depth",))
    if observed_queue_depth > queue_depth_max:
        return VerificationResult(
            "RED", (f"queue_depth_exceeded:{observed_queue_depth}>{queue_depth_max}",)
        )
    return VerificationResult("GREEN")


def score_queue_depth_from_settings(
    settings: Settings, *, observed_queue_depth: int
) -> VerificationResult:
    """`score_queue_depth`, sourcing its ceiling from
    `Settings.admission_queue_depth_max` — the real runtime consumer for
    that fail-boot-required field (see its docstring in
    `packages/core/src/omniscience_core/config.py`). RED if unset."""
    queue_depth_max = settings.admission_queue_depth_max
    if queue_depth_max is None:
        return VerificationResult("RED", ("admission_queue_depth_max_unset",))
    return score_queue_depth(
        observed_queue_depth=observed_queue_depth, queue_depth_max=queue_depth_max
    )


# ---------------------------------------------------------------------------
# Fallback probe (AC-SCALE-3 — stable 429/503, no false-freshness success)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverloadResponseObservation:
    """One observed overload-path HTTP/MCP response, as measured."""

    status_code: int
    code: str
    is_success_status: bool
    meta_freshness_status: str | None


_STABLE_OVERLOAD_CODES: frozenset[int] = frozenset({429, 503})
_FALSE_FRESHNESS_STATUSES: frozenset[str] = frozenset({"fresh"})


def score_fallback(*, observations: Sequence[OverloadResponseObservation]) -> VerificationResult:
    """Verify every overload observation is a stable 429/503 error and
    never a 2xx success carrying false freshness (REQ-MCP-4/7)."""
    if not observations:
        return VerificationResult("RED", ("no_overload_observations",))

    reasons: list[str] = []
    for index, obs in enumerate(observations):
        if obs.is_success_status:
            reasons.append(f"overload_returned_success_status:{index}:{obs.status_code}")
            continue
        if obs.status_code not in _STABLE_OVERLOAD_CODES:
            reasons.append(f"overload_status_not_stable:{index}:{obs.status_code}")
        if obs.meta_freshness_status in _FALSE_FRESHNESS_STATUSES:
            reasons.append(f"overload_claims_fresh_meta:{index}")

    if reasons:
        return VerificationResult("RED", tuple(reasons))
    return VerificationResult("GREEN")


# ---------------------------------------------------------------------------
# Content-addressed qualification report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualificationReport:
    """Content-addressed, tamper-evident horizontal-read qualification
    record. ``report_id`` self-addresses every other field (sha256), so a
    tampered or partially-filled report cannot silently claim a different
    identity — same pattern as `scripts/qualify_backup_restore.py`'s
    `DrillManifest`, reproduced locally (see module docstring).

    ``queue_depth_status`` is GREEN/RED from `score_queue_depth`
    (`_from_settings`), or None when the admission queue-depth bound wasn't
    measured for this report — optional, unlike the four required probes;
    AC-SCALE-5 doesn't gate overall HA status on it (see
    `build_qualification_report`)."""

    report_id: str
    replica_count: int
    skew_status: str
    isolation_status: str
    fairness_status: str
    fallback_status: str
    status: str  # "GREEN" | "RED"
    reasons: tuple[str, ...]
    queue_depth_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "replica_count": self.replica_count,
            "skew_status": self.skew_status,
            "isolation_status": self.isolation_status,
            "fairness_status": self.fairness_status,
            "fallback_status": self.fallback_status,
            "status": self.status,
            "reasons": list(self.reasons),
            "queue_depth_status": self.queue_depth_status,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


_MIN_QUALIFYING_REPLICAS = 2


def build_qualification_report(
    *,
    replica_count: int,
    skew: VerificationResult,
    isolation: VerificationResult,
    fairness: VerificationResult,
    fallback: VerificationResult,
    queue_depth: VerificationResult | None = None,
) -> QualificationReport:
    """Assemble a `QualificationReport` from the four required measured
    probe results, plus an optional fifth `queue_depth` probe
    (`score_queue_depth`/`_from_settings`). Single-replica or unmeasured
    (fewer than two replicas) topologies are always RED regardless of
    individual probe status — AC-SCALE-5: "single-replica or unmeasured
    profiles remain non-HA". `queue_depth` is optional and backward
    compatible: omitting it (the default) reproduces the exact pre-existing
    four-probe report; a RED `queue_depth` result still fails the overall
    report when supplied."""
    reasons: list[str] = []
    if replica_count < _MIN_QUALIFYING_REPLICAS:
        reasons.append(f"replica_count_below_minimum:{replica_count}<{_MIN_QUALIFYING_REPLICAS}")
    for label, result in (
        ("skew", skew),
        ("isolation", isolation),
        ("fairness", fairness),
        ("fallback", fallback),
    ):
        if not result.is_green:
            reasons.extend(f"{label}:{reason}" for reason in result.reasons)
    if queue_depth is not None and not queue_depth.is_green:
        reasons.extend(f"queue_depth:{reason}" for reason in queue_depth.reasons)

    status = "RED" if reasons else "GREEN"
    queue_depth_status = queue_depth.status if queue_depth is not None else None
    body: dict[str, Any] = {
        "replica_count": replica_count,
        "skew_status": skew.status,
        "isolation_status": isolation.status,
        "fairness_status": fairness.status,
        "fallback_status": fallback.status,
        "status": status,
        "reasons": reasons,
        "queue_depth_status": queue_depth_status,
    }
    content_hash = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()

    return QualificationReport(
        report_id=f"sha256:{content_hash}",
        replica_count=replica_count,
        skew_status=skew.status,
        isolation_status=isolation.status,
        fairness_status=fairness.status,
        fallback_status=fallback.status,
        status=status,
        reasons=tuple(reasons),
        queue_depth_status=queue_depth_status,
    )


_REPORT_REQUIRED_FIELDS: tuple[str, ...] = (
    "report_id",
    "replica_count",
    "skew_status",
    "isolation_status",
    "fairness_status",
    "fallback_status",
    "status",
)


def validate_report_json(report: Any | None) -> VerificationResult:
    """Re-validate an already-serialized qualification report artifact.
    Checks the artifact's own shape and declared per-dimension statuses —
    never trusts a self-declared ``"status": "GREEN"`` alone."""
    if report is None:
        return VerificationResult("RED", ("report_absent",))
    if not isinstance(report, dict):
        return VerificationResult("RED", ("report_malformed:not_an_object",))

    reasons: list[str] = []
    for field in _REPORT_REQUIRED_FIELDS:
        if field not in report or report[field] is None:
            reasons.append(f"report_field_absent:{field}")

    replica_count = report.get("replica_count")
    if isinstance(replica_count, int) and replica_count < _MIN_QUALIFYING_REPLICAS:
        reasons.append(f"report_replica_count_below_minimum:{replica_count}")
    for label in ("skew_status", "isolation_status", "fairness_status", "fallback_status"):
        if report.get(label) not in ("GREEN", "RED"):
            reasons.append(f"report_{label}_not_green_or_red:{report.get(label)!r}")
        elif report.get(label) == "RED":
            reasons.append(f"report_{label}_is_red")

    # queue_depth_status is optional (unmeasured => None is valid); only
    # validate its shape/value when the report actually declares one.
    queue_depth_status = report.get("queue_depth_status")
    if queue_depth_status is not None:
        if queue_depth_status not in ("GREEN", "RED"):
            reasons.append(f"report_queue_depth_status_not_green_or_red:{queue_depth_status!r}")
        elif queue_depth_status == "RED":
            reasons.append("report_queue_depth_status_is_red")

    if report.get("status") != "GREEN":
        reasons.append(f"report_status_not_green:{report.get('status')!r}")

    if reasons:
        return VerificationResult("RED", tuple(dict.fromkeys(reasons)))
    return VerificationResult("GREEN")


__all__ = [
    "LaneObservation",
    "OverloadResponseObservation",
    "QualificationReport",
    "TenantObservation",
    "VerificationResult",
    "build_qualification_report",
    "score_fairness",
    "score_fairness_from_settings",
    "score_fallback",
    "score_isolation",
    "score_queue_depth",
    "score_queue_depth_from_settings",
    "score_skew",
    "validate_report_json",
]
