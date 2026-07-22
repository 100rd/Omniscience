"""ManagementContextBundle (SPEC-MCTX REQ-MCTX-2) -- binds the request digest,
producer/source revisions, produced/observed times, freshness deadline, coverage
and source-health axes, citations, conflict set, projection state, PII receipt and
integrity. An incomplete bundle is never constructed as a favorable partial result
-- ``producer.py`` builds one only after every required input is known.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omniscience_core.management.citation import ManagementCitation
from omniscience_core.management.contract_schemas import validate_against_schema
from omniscience_core.management.quality import KnowledgeQualitySnapshot


@dataclass(frozen=True)
class SynthesisStatement:
    """Generated narrative -- explicitly typed ``synthesis`` and can never create an
    uncited fact (REQ-MCTX-3); excluded from deterministic replay comparisons
    (REQ-MCTX-10)."""

    text: str
    cites: tuple[str, ...]

    def to_schema_dict(self) -> dict[str, Any]:
        return {"type": "synthesis", "text": self.text, "cites": list(self.cites)}


@dataclass(frozen=True)
class ManagementContextBundle:
    bundle_id: str
    request_digest: str
    producer_revision: str
    source_revisions: Mapping[str, str]
    produced_at: str
    observed_at: str
    freshness_deadline: str
    source_health: str
    citations: tuple[ManagementCitation, ...]
    conflict_set: tuple[str, ...]
    projection_state: str
    coverage_gaps: tuple[str, ...]
    pii_receipt: Mapping[str, Any]
    quality: KnowledgeQualitySnapshot
    synthesis: tuple[SynthesisStatement, ...]
    integrity: Mapping[str, str]

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "request_digest": self.request_digest,
            "producer_revision": self.producer_revision,
            "source_revisions": dict(self.source_revisions),
            "produced_at": self.produced_at,
            "observed_at": self.observed_at,
            "freshness_deadline": self.freshness_deadline,
            "source_health": self.source_health,
            "citations": [citation.to_schema_dict() for citation in self.citations],
            "conflict_set": list(self.conflict_set),
            "projection_state": self.projection_state,
            "coverage_gaps": list(self.coverage_gaps),
            "pii_receipt": dict(self.pii_receipt),
            "quality": self.quality.to_schema_dict(),
            "synthesis": [statement.to_schema_dict() for statement in self.synthesis],
            "integrity": dict(self.integrity),
        }

    def replay_digest_payload(self) -> dict[str, Any]:
        """Deterministic subset used for replay comparisons (REQ-MCTX-10): excludes
        ``synthesis`` (optional, nondeterministic) and its own ``integrity`` field."""
        payload = self.to_schema_dict()
        payload.pop("synthesis", None)
        payload.pop("integrity", None)
        return payload


def validate_bundle_schema(bundle: ManagementContextBundle) -> list[str]:
    return validate_against_schema(
        bundle.to_schema_dict(), "management-context-bundle.schema.json"
    )
