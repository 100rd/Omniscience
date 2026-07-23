"""Closed read-only evidence-source protocol (SPEC-MCTX REQ-MCTX-9).

The producer never issues arbitrary graph/vector queries; it calls exactly these
three closed methods. Any exception raised by an implementation is treated as
severance by ``producer.py`` -- never partial success (REQ-MCTX-8).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from omniscience_core.management.request import ManagementContextRequest


@dataclass(frozen=True)
class RawCitation:
    """One candidate citation fetched from a source, before PW0 admission and the
    authority/PII conformance scan run over it (see ``producer.py``)."""

    citation_id: str
    source_ref: str
    uri: str
    snapshot_digest: str
    observed_at: str
    retrieval_strategy: str
    field_class: str
    data_class: str
    transform_state: str
    text: str


@dataclass(frozen=True)
class RawQualityInputs:
    """Per-axis evidence for ``KnowledgeQualitySnapshot``. A ``None`` axis means the
    source could not evaluate it -- the producer maps that to ``unknown``, never a
    favorable default (REQ-MCTX-4)."""

    provenance_status: str | None
    freshness_status: str | None
    conformance_status: str | None
    coverage_status: str | None
    conflict_status: str | None
    projection_status: str | None
    detail: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class SourceHealth:
    healthy: bool
    reason: str | None = None


@runtime_checkable
class ManagementEvidenceSource(Protocol):
    """Closed query profile (REQ-MCTX-9) -- three methods, nothing else. Fixtures
    implement this deterministically; there is no live implementation in this task."""

    def health(self) -> SourceHealth: ...

    def fetch_citations(self, request: ManagementContextRequest) -> tuple[RawCitation, ...]: ...

    def fetch_quality(self, request: ManagementContextRequest) -> RawQualityInputs: ...
