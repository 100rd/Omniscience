"""SP-81 management-context and knowledge-quality producer v1 (SPEC-MCTX, ADR-0021).

See ``contracts/management/README.md`` for the schema binding this package
implements and ``docs/api/management-context.md`` for the operational picture.
"""

from __future__ import annotations

from omniscience_core.management.bundle import (
    ManagementContextBundle,
    SynthesisStatement,
    validate_bundle_schema,
)
from omniscience_core.management.citation import ManagementCitation, validate_citation
from omniscience_core.management.conformance import conformance_scan, scan_text
from omniscience_core.management.evidence_source import (
    ManagementEvidenceSource,
    RawCitation,
    RawQualityInputs,
    SourceHealth,
)
from omniscience_core.management.manifest import (
    ManagementContextCapabilityManifest,
    ManagementContractPin,
    check_contract_skew,
    validate_manifest,
)
from omniscience_core.management.pii import PiiFieldDecision, admit_field, build_pii_receipt
from omniscience_core.management.producer import ManagementContextProducer
from omniscience_core.management.quality import KnowledgeQualitySnapshot, validate_quality_snapshot
from omniscience_core.management.request import (
    ManagementContextRequest,
    RequestAuthorization,
    validate_request_schema,
    validate_scope,
)
from omniscience_core.management.severance import (
    DenialResult,
    FallbackResult,
    ManagementContextResult,
    validate_fallback_result,
)
from omniscience_core.management.taxonomy import (
    DENIAL_REASONS,
    FALLBACK_REASONS,
    FIELD_CLASSES,
    MANAGEMENT_DOMAINS,
)

__all__ = [
    "DENIAL_REASONS",
    "FALLBACK_REASONS",
    "FIELD_CLASSES",
    "MANAGEMENT_DOMAINS",
    "DenialResult",
    "FallbackResult",
    "KnowledgeQualitySnapshot",
    "ManagementCitation",
    "ManagementContextBundle",
    "ManagementContextCapabilityManifest",
    "ManagementContextProducer",
    "ManagementContextRequest",
    "ManagementContextResult",
    "ManagementContractPin",
    "ManagementEvidenceSource",
    "PiiFieldDecision",
    "RawCitation",
    "RawQualityInputs",
    "RequestAuthorization",
    "SourceHealth",
    "SynthesisStatement",
    "admit_field",
    "build_pii_receipt",
    "check_contract_skew",
    "conformance_scan",
    "scan_text",
    "validate_bundle_schema",
    "validate_citation",
    "validate_fallback_result",
    "validate_manifest",
    "validate_quality_snapshot",
    "validate_request_schema",
    "validate_scope",
]
