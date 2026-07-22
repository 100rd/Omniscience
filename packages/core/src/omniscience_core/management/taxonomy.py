"""Closed taxonomy for the SP-81 management-context producer (SPEC-MCTX, ADR-0021).

Every enum here is closed by design: an unmapped value is never treated as safe or
in-scope (fail closed on ambiguity, mirrors ``omniscience_core.privacy.taxonomy``).
"""

from __future__ import annotations

import re

#: The 12 Continuous Management domain packs (genai-enablement ADR-0020 D3). A
#: request naming any other domain is denied before retrieval (REQ-MCTX-1).
MANAGEMENT_DOMAINS: tuple[str, ...] = (
    "reliability",
    "cost_and_value",
    "ai_and_agent_effectiveness",
    "security",
    "privacy",
    "continuous_compliance",
    "supply_chain_integrity",
    "delivery_effectiveness",
    "knowledge_quality",
    "capacity_performance_sustainability",
    "toil",
    "product_outcomes",
)

#: Field classes a request may ask for (SPEC-MCTX Scope "In:"). Anything else widens
#: the request beyond its authorized purpose and is denied (REQ-MCTX-1 fallback).
FIELD_CLASSES: tuple[str, ...] = (
    "citations",
    "provenance",
    "freshness",
    "coverage",
    "conflicts",
    "projection_health",
    "knowledge_quality",
    "synthesis",
)

#: Orthogonal evidence-fitness axis statuses (REQ-MCTX-4 fallback): missing required
#: evidence yields ``unknown``, ``partial``, or ``severed`` -- never a favorable default.
EVIDENCE_FITNESS_STATUSES: tuple[str, ...] = ("known", "partial", "unknown", "severed")

FRESHNESS_STATUSES: tuple[str, ...] = ("fresh", "stale", "unknown", "degraded")
PROVENANCE_STATUSES: tuple[str, ...] = ("complete", "partial", "unknown", "severed")
CONFORMANCE_STATUSES: tuple[str, ...] = ("conformant", "nonconformant", "partial", "unknown")
COVERAGE_STATUSES: tuple[str, ...] = ("complete", "partial", "unknown", "severed")
CONFLICT_STATUSES: tuple[str, ...] = ("none", "detected", "unresolved", "unknown")
PROJECTION_STATUSES: tuple[str, ...] = ("converged", "degraded", "unknown", "severed")

#: Denial reasons -- REQ-MCTX-1: scope is rejected before any retrieval happens.
DENIAL_REASONS: tuple[str, ...] = (
    "request_schema_invalid",
    "workspace_missing_or_foreign",
    "domain_unknown",
    "purpose_unknown_or_unauthorized",
    "field_class_widened",
    "evidence_cut_in_future",
    "max_age_invalid",
    "contract_revision_missing",
)

#: Fallback-required reasons -- REQ-MCTX-7/8: contract skew and severance never
#: degrade to best-effort parsing or stale success.
FALLBACK_REASONS: tuple[str, ...] = (
    "contract_major_mismatch",
    "schema_digest_mismatch",
    "producer_manifest_mismatch",
    "producer_unavailable",
    "source_severed",
    "evidence_stale",
    "token_revoked",
    "authority_field_detected",
    "bundle_schema_invalid",
)

#: REQ-MCTX-5: forbidden management-truth field-name tokens. A key containing any of
#: these anywhere in the produced bundle fails static/schema checks before response.
FORBIDDEN_AUTHORITY_FIELD_TOKENS: frozenset[str] = frozenset(
    {
        "availability",
        "error_budget",
        "errorbudget",
        "incident",
        "cost_opportunity",
        "risk",
        "compliance",
        "action",
        "approval",
        "effect",
        "verification",
        "verdict",
        "readiness",
        "authorization",
        "sla_breach",
    }
)

#: REQ-MCTX-6: credential-shaped field-name tokens -- never admitted into a bundle.
CREDENTIAL_KEY_TOKENS: frozenset[str] = frozenset(
    {
        "credential",
        "password",
        "api_key",
        "apikey",
        "secret",
        "token",
        "private_key",
        "bearer",
    }
)

#: REQ-MCTX-6: active-content markers -- never admitted into a bundle's text fields.
ACTIVE_CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<\s*script\b", re.IGNORECASE),
    re.compile(r"javascript\s*:", re.IGNORECASE),
    re.compile(r"\bon(error|load|click|mouseover)\s*=", re.IGNORECASE),
    re.compile(r"<\s*iframe\b", re.IGNORECASE),
)

#: REQ-MCTX-6: seeded-PII value shapes -- closed pattern set, defense-in-depth on top
#: of PW0 field-level admission (never the sole PII control).
SEEDED_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-shaped
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),  # credit-card-shaped
    re.compile(r"\b\+?\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}\b"),  # phone-shaped
)
