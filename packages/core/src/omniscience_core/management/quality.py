"""KnowledgeQualitySnapshot (SPEC-MCTX REQ-MCTX-4) -- provenance, freshness,
conformance, coverage, conflict and projection conditions reported independently.
No global quality score or management verdict field exists on this type; adding one
would fail ``knowledge-quality-snapshot.schema.json`` (``additionalProperties: false``)
and the authority scan in ``conformance.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omniscience_core.management.contract_schemas import validate_against_schema


@dataclass(frozen=True)
class KnowledgeQualitySnapshot:
    snapshot_id: str
    workspace_id: str
    management_domain: str
    provenance_status: str
    freshness_status: str
    conformance_status: str
    coverage_status: str
    conflict_status: str
    projection_status: str
    evaluated_at: str
    detail: Mapping[str, tuple[str, ...]]
    integrity: Mapping[str, str]

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "workspace_id": self.workspace_id,
            "management_domain": self.management_domain,
            "provenance_status": self.provenance_status,
            "freshness_status": self.freshness_status,
            "conformance_status": self.conformance_status,
            "coverage_status": self.coverage_status,
            "conflict_status": self.conflict_status,
            "projection_status": self.projection_status,
            "evaluated_at": self.evaluated_at,
            "detail": {axis: list(reasons) for axis, reasons in self.detail.items()},
            "integrity": dict(self.integrity),
        }


def validate_quality_snapshot(snapshot: KnowledgeQualitySnapshot) -> list[str]:
    return validate_against_schema(
        snapshot.to_schema_dict(), "knowledge-quality-snapshot.schema.json"
    )
