"""PW0 fail-closed PII knowledge-propagation boundary (task-sp-61-pii-wall-pw0).

See ``contracts/pii/README.md`` for the SP-60 shape this package binds to, and
``docs/runbooks/pii-wall.md`` for the operational picture.
"""

from __future__ import annotations

from omniscience_core.privacy.envelope import DataEnvelope, validate_envelope
from omniscience_core.privacy.gate import (
    DetectionResult,
    Detector,
    DetectorFailureError,
    GateDecision,
    GateDisposition,
    IngestPIIGate,
)
from omniscience_core.privacy.lifecycle import (
    DeletionReceipt,
    aggregate_lifecycle,
    build_deletion_receipt,
    check_lifecycle_claim,
)
from omniscience_core.privacy.policy import ConsumerPolicyPin, check_policy_skew
from omniscience_core.privacy.quarantine import (
    FailingQuarantineStore,
    InMemoryQuarantineStore,
    QuarantineError,
    QuarantineStore,
    QuarantineUnavailableError,
)
from omniscience_core.privacy.receipts import (
    BoundaryReceipt,
    PrivacyCoverage,
    SanitizationReceipt,
    compute_integrity,
    scan_receipt_for_raw_leakage,
)
from omniscience_core.privacy.taxonomy import (
    DATA_CLASSES,
    DISPOSITIONS,
    TRANSFORM_STATES,
    is_pw0_admitted,
)

__all__ = [
    "DATA_CLASSES",
    "DISPOSITIONS",
    "TRANSFORM_STATES",
    "BoundaryReceipt",
    "ConsumerPolicyPin",
    "DataEnvelope",
    "DeletionReceipt",
    "DetectionResult",
    "Detector",
    "DetectorFailureError",
    "FailingQuarantineStore",
    "GateDecision",
    "GateDisposition",
    "InMemoryQuarantineStore",
    "IngestPIIGate",
    "PrivacyCoverage",
    "QuarantineError",
    "QuarantineStore",
    "QuarantineUnavailableError",
    "SanitizationReceipt",
    "aggregate_lifecycle",
    "build_deletion_receipt",
    "check_lifecycle_claim",
    "check_policy_skew",
    "compute_integrity",
    "is_pw0_admitted",
    "scan_receipt_for_raw_leakage",
    "validate_envelope",
]
