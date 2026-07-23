"""IngestPIIGate -- the PW0 fail-closed admission boundary (task-sp-61-pii-wall-pw0).

SPEC-PII: ``IngestPIIGate.evaluate(source, principal, body_ref, policy_pin) -> admitted
envelope | quarantine | deny``. Every path returns a decision and a non-identifying
``BoundaryReceipt``; only ``ADMIT`` permits a caller to invoke the protected sink.
Policy skew, detector failure, and quarantine failure all resolve to ``PARK`` -- there
is no branch that falls back to admitting unclassified or non-conforming content.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from omniscience_core.privacy.envelope import DataEnvelope
from omniscience_core.privacy.policy import ConsumerPolicyPin, check_policy_skew
from omniscience_core.privacy.quarantine import QuarantineStore, QuarantineUnavailableError
from omniscience_core.privacy.receipts import BoundaryReceipt, digest_envelope_identity
from omniscience_core.privacy.taxonomy import DATA_CLASSES, is_pw0_admitted


class GateDisposition(enum.StrEnum):
    ADMIT = "admitted"
    QUARANTINE = "quarantined"
    PARK = "parked"


@dataclass(frozen=True)
class DetectionResult:
    data_class: str
    transform_state: str
    detector_revisions: tuple[str, ...]


@runtime_checkable
class Detector(Protocol):
    def classify(self, envelope: DataEnvelope) -> DetectionResult: ...


class DetectorFailureError(Exception):
    """Raised by a ``Detector`` implementation to signal it could not classify.

    The gate treats any exception raised by ``detector.classify`` -- this one or any
    other -- identically: park, never admit (SPEC-PII REQ-PII-2 fallback).
    """


@dataclass(frozen=True)
class GateDecision:
    disposition: GateDisposition
    reason: str
    receipt: BoundaryReceipt
    quarantine_id: str | None = None


@dataclass(frozen=True)
class IngestPIIGate:
    """PW0 boundary. Construct once per component with the live quarantine store and
    consumer policy pin; call ``evaluate`` before every protected sink invocation."""

    quarantine_store: QuarantineStore
    consumer_pin: ConsumerPolicyPin

    def evaluate(
        self,
        *,
        envelope: DataEnvelope,
        policy_bundle: Mapping[str, Any],
        detector: Detector,
        reference_now: str,
        receipt_id: str,
        produced_at: str,
    ) -> GateDecision:
        skew_errors = check_policy_skew(
            self.consumer_pin, policy_bundle, reference_now=reference_now
        )
        if skew_errors:
            return self._park(
                envelope,
                policy_bundle=policy_bundle,
                reason="policy_skew",
                detail=tuple(skew_errors),
                receipt_id=receipt_id,
                produced_at=produced_at,
            )

        try:
            detection = detector.classify(envelope)
        except Exception as exc:
            return self._park(
                envelope,
                policy_bundle=policy_bundle,
                reason="detector_failure",
                detail=(f"{type(exc).__name__}: {exc}",),
                receipt_id=receipt_id,
                produced_at=produced_at,
            )

        if detection.data_class not in DATA_CLASSES or detection.data_class == "unknown":
            return self._quarantine_or_park(
                envelope,
                policy_bundle=policy_bundle,
                reason="unknown_class",
                receipt_id=receipt_id,
                produced_at=produced_at,
            )

        if is_pw0_admitted(detection.data_class, detection.transform_state):
            return self._admit(
                envelope,
                policy_bundle=policy_bundle,
                receipt_id=receipt_id,
                produced_at=produced_at,
            )

        return self._quarantine_or_park(
            envelope,
            policy_bundle=policy_bundle,
            reason="not_pw0_admitted",
            receipt_id=receipt_id,
            produced_at=produced_at,
        )

    def _admit(
        self,
        envelope: DataEnvelope,
        *,
        policy_bundle: Mapping[str, Any],
        receipt_id: str,
        produced_at: str,
    ) -> GateDecision:
        receipt = self._receipt(
            envelope,
            disposition=GateDisposition.ADMIT,
            reason="pw0_admitted",
            policy_bundle=policy_bundle,
            receipt_id=receipt_id,
            produced_at=produced_at,
        )
        return GateDecision(
            disposition=GateDisposition.ADMIT, reason="pw0_admitted", receipt=receipt
        )

    def _quarantine_or_park(
        self,
        envelope: DataEnvelope,
        *,
        policy_bundle: Mapping[str, Any],
        reason: str,
        receipt_id: str,
        produced_at: str,
    ) -> GateDecision:
        try:
            quarantine_id = self.quarantine_store.put(envelope)
        except QuarantineUnavailableError as exc:
            return self._park(
                envelope,
                policy_bundle=policy_bundle,
                reason="quarantine_unavailable",
                detail=(str(exc),),
                receipt_id=receipt_id,
                produced_at=produced_at,
            )
        receipt = self._receipt(
            envelope,
            disposition=GateDisposition.QUARANTINE,
            reason=reason,
            policy_bundle=policy_bundle,
            receipt_id=receipt_id,
            produced_at=produced_at,
        )
        return GateDecision(
            disposition=GateDisposition.QUARANTINE,
            reason=reason,
            receipt=receipt,
            quarantine_id=quarantine_id,
        )

    def _park(
        self,
        envelope: DataEnvelope,
        *,
        policy_bundle: Mapping[str, Any],
        reason: str,
        receipt_id: str,
        produced_at: str,
        detail: tuple[str, ...] = (),
    ) -> GateDecision:
        receipt = self._receipt(
            envelope,
            disposition=GateDisposition.PARK,
            reason=reason,
            policy_bundle=policy_bundle,
            receipt_id=receipt_id,
            produced_at=produced_at,
            detail=detail,
        )
        return GateDecision(disposition=GateDisposition.PARK, reason=reason, receipt=receipt)

    def _receipt(
        self,
        envelope: DataEnvelope,
        *,
        disposition: GateDisposition,
        reason: str,
        policy_bundle: Mapping[str, Any],
        receipt_id: str,
        produced_at: str,
        detail: tuple[str, ...] = (),
    ) -> BoundaryReceipt:
        return BoundaryReceipt(
            receipt_id=receipt_id,
            disposition=disposition.value,
            reason=reason,
            envelope_digest=digest_envelope_identity(
                envelope_id=envelope.envelope_id, policy_revision=envelope.policy_revision
            ),
            policy_revision=policy_bundle.get("policy_revision"),
            profile="PW0",
            produced_at=produced_at,
            producer="omniscience-pw0-gate",
            detail=detail,
        )
