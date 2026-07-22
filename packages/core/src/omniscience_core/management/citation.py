"""ManagementCitation (SPEC-MCTX REQ-MCTX-3) -- every fact is joined to a stable
source citation and exact evidence fitness. Never carries raw content itself; the
producer only assembles a citation once its source field has passed PW0 admission
and the authority/PII conformance scan (see ``producer.py``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omniscience_core.management.contract_schemas import validate_against_schema


@dataclass(frozen=True)
class ManagementCitation:
    citation_id: str
    source_ref: str
    uri: str
    snapshot_digest: str
    observed_at: str
    retrieval_strategy: str
    field_class: str
    evidence_fitness: str

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "source_ref": self.source_ref,
            "uri": self.uri,
            "snapshot_digest": self.snapshot_digest,
            "observed_at": self.observed_at,
            "retrieval_strategy": self.retrieval_strategy,
            "field_class": self.field_class,
            "evidence_fitness": self.evidence_fitness,
        }


def validate_citation(citation: ManagementCitation) -> list[str]:
    return validate_against_schema(citation.to_schema_dict(), "management-citation.schema.json")
