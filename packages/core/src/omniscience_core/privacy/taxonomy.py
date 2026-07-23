"""PW0 closed taxonomy (task-sp-61-pii-wall-pw0, ADR-0018 D2/D3, SPEC-PII REQ-PII-3).

Values mirror the vendored SP-60 schemas in ``contracts/pii/schemas/v1`` byte for byte
(see ``contracts/pii/pin.json``). This module is the single place the PW0 admission
predicate lives; ``gate.py`` calls it and nothing else re-derives it.
"""

from __future__ import annotations

DATA_CLASSES: tuple[str, ...] = (
    "public",
    "internal_non_personal",
    "personal",
    "sensitive_personal",
    "prohibited",
    "unknown",
)

TRANSFORM_STATES: tuple[str, ...] = ("raw", "redacted", "pseudonymized", "aggregated")

DISPOSITIONS: tuple[str, ...] = (
    "complete",
    "pending",
    "excluded",
    "immutable_retention",
    "backup_pending",
    "failed",
    "unavailable",
)

PW0_SAFE_DATA_CLASSES = frozenset({"public", "internal_non_personal"})
"""Never personal by definition; admitted under PW0 regardless of transform_state."""

PW0_REDACTION_ELIGIBLE_CLASSES = frozenset({"personal", "sensitive_personal"})
"""Admitted under PW0 only once deterministically redacted (ADR-0018 D3: "or
deterministically redacted data"). Raw personal/sensitive_personal is never admitted."""


def is_pw0_admitted(data_class: str, transform_state: str) -> bool:
    """Return whether ``data_class``/``transform_state`` may enter a PW0-protected sink.

    ``prohibited`` and ``unknown`` are never admitted regardless of transform_state:
    ADR-0018 D2 -- unknown never means non-personal, and prohibited data cannot be
    made safe by redaction. Any unmapped value outside the closed taxonomy is treated
    the same as ``unknown`` (fail closed on ambiguity, never on trust).
    """
    if data_class not in DATA_CLASSES or transform_state not in TRANSFORM_STATES:
        return False
    if data_class in PW0_SAFE_DATA_CLASSES:
        return True
    return data_class in PW0_REDACTION_ELIGIBLE_CLASSES and transform_state == "redacted"
