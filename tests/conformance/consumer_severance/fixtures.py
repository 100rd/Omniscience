"""Producer-owned MCP v1 consumer-severance fixture matrix (AC-SEV-1).

This module is the single source of truth for the fixture matrix consumed
by both the pytest suite (`tests/test_consumer_severance_matrix.py`) and
the read-only verifier (`scripts/check_consumer_severance.py`). It never
imports `omniscience_server.mcp.metadata` / `.canary` — those, plus the
JSON schemas under `apps/server/src/omniscience_server/mcp/contracts/v1/
schemas/`, are the oracle this kit conforms to. Ground truth was extracted
by hand-reading that code and is re-derived from the schema at test time
(see the exhaustiveness gate in `tests/test_consumer_severance_matrix.py`),
never by calling it.

Three fixture categories, matching the three failure classes a consumer
must switch on:

1. ``REASON_FIXTURES`` — every value of the closed `meta.fallback.reason`
   enum (schema: `meta.schema.json#/properties/fallback/properties/reason`
   — 8 non-null reasons + null), each paired with the `freshness`/
   `consistency` substate that produces it (`metadata.py::_freshness` /
   `::_consistency` / `build_response_meta`'s contract-override clause).
2. ``PAYLOAD_SHAPE_FIXTURES`` — REQ-MCP-4 transport-level failures that
   never reach a `meta.fallback` state at all (oversized/non-finite
   response, missing/invalid `meta`).
3. ``CONTRACT_PIN_FIXTURES`` — the full, exhaustiveness-gated set of
   `canary.py` fail-closed codes (contract-pin mismatch / drift), modelled
   as "any of these means the contract identity is unverifiable" rather
   than re-testing `canary.py` itself (that belongs to
   `tests/test_mcp_canary.py`, out of this kit's scope). Completeness is
   enforced by a source-derived gate (see
   `test_contract_pin_fixtures_cover_every_canary_fail_closed_code` in
   `tests/test_consumer_severance_matrix.py`), mirroring the reason-enum
   gate below.

Decision vocabulary is defined in `reference_consumer.py`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

_CONTRACT_COMMIT = "a" * 40
_CONTRACT_SCHEMA_SHA256 = "b" * 64
_WORKSPACE_ID = "11111111-2222-4333-8444-555555555555"
_TIMESTAMP = "2026-07-18T00:00:00Z"


@dataclass(frozen=True)
class ReasonFixture:
    """One `meta.fallback.reason` state plus its expected consumer decision."""

    id: str
    freshness_status: str
    freshness_reason: str | None
    consistency_status: str
    consistency_reason: str | None
    fallback_reason: str | None
    expected_decision: str
    direct_source_available: bool = True
    note: str = ""

    def meta(self) -> dict[str, Any]:
        freshness: dict[str, Any] = {
            "status": self.freshness_status,
            "evaluated_at": _TIMESTAMP,
            "oldest_source_age_seconds": None if self.freshness_status == "unknown" else 42,
            "stale_source_ids": (
                ["22222222-3333-4444-8888-666666666666"]
                if self.freshness_status == "stale"
                else []
            ),
        }
        if self.freshness_reason is not None:
            freshness["reason"] = self.freshness_reason
        consistency: dict[str, Any] = {
            "status": self.consistency_status,
            "degraded_subsystems": (
                ["neo4j-projection"] if self.consistency_status == "degraded" else []
            ),
            "projection_lag_versions": 3 if self.consistency_status == "degraded" else 0,
        }
        return {
            "contract": {
                "version": "1.0.0",
                "commit": _CONTRACT_COMMIT,
                "schema_sha256": _CONTRACT_SCHEMA_SHA256,
            },
            "workspace_id": _WORKSPACE_ID,
            "generated_at": _TIMESTAMP,
            "effective_as_of": _TIMESTAMP,
            "freshness": freshness,
            "consistency": consistency,
            "fallback": {
                "required": self.fallback_reason is not None,
                "reason": self.fallback_reason,
            },
        }


@dataclass(frozen=True)
class PayloadShapeFixture:
    """A REQ-MCP-4 transport-level failure — no `meta.fallback` state exists."""

    id: str
    condition: str
    error_code: str | None
    expected_decision: str
    note: str = ""


@dataclass(frozen=True)
class ContractPinFixture:
    """One `canary.py` fail-closed code (§2 of the taxonomy) and its decision."""

    id: str
    canary_code: str
    expected_decision: str
    note: str = ""


# ---------------------------------------------------------------------------
# 1. meta.fallback.reason closed enum — 8 non-null + null (9 rows; two rows
#    share the "missing_lineage" reason string because metadata.py collapses
#    two distinct triggers onto it — see metadata.py:136,152-155,174-176).
# ---------------------------------------------------------------------------

REASON_FIXTURES: tuple[ReasonFixture, ...] = (
    ReasonFixture(
        id="fresh-converged-use",
        freshness_status="fresh",
        freshness_reason=None,
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason=None,
        expected_decision="use",
        note="metadata.py:177-179 — no reason, fallback.required=False.",
    ),
    ReasonFixture(
        id="missing-lineage-no-source-ids",
        freshness_status="unknown",
        freshness_reason="missing_lineage",
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="missing_lineage",
        expected_decision="direct_source_fallback",
        note="metadata.py:127-134 — no lineage extracted at all.",
    ),
    ReasonFixture(
        id="missing-lineage-snapshot-absent-or-post-boundary",
        freshness_status="unknown",
        freshness_reason="missing_lineage",
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="missing_lineage",
        expected_decision="direct_source_fallback",
        note=(
            "metadata.py:136,152-155,174-176 — used source id missing from "
            "snapshots, or its last_sync_at is after the evaluation boundary. "
            "Same reason string, distinct trigger from the row above."
        ),
    ),
    ReasonFixture(
        id="source-degraded",
        freshness_status="degraded",
        freshness_reason="source_degraded",
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="source_degraded",
        expected_decision="direct_source_fallback",
        note="metadata.py:147-148,165-167 — a used source's status == 'error'.",
    ),
    ReasonFixture(
        id="stale-source",
        freshness_status="stale",
        freshness_reason="stale_source",
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="stale_source",
        expected_decision="direct_source_fallback",
        note="metadata.py:158-161,168-170 — age exceeds freshness_sla_seconds.",
    ),
    ReasonFixture(
        id="never-synced",
        freshness_status="unknown",
        freshness_reason="never_synced",
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="never_synced",
        expected_decision="direct_source_fallback",
        note="metadata.py:149-151,171-173 — last_sync_at is None.",
    ),
    ReasonFixture(
        id="freshness-sla-unknown",
        freshness_status="unknown",
        freshness_reason="freshness_sla_unknown",
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="freshness_sla_unknown",
        expected_decision="direct_source_fallback",
        note="metadata.py:158-159,174-176 — freshness_sla_seconds is None.",
    ),
    ReasonFixture(
        id="projection-divergence",
        freshness_status="fresh",
        freshness_reason=None,
        consistency_status="degraded",
        consistency_reason="projection_divergence",
        fallback_reason="projection_divergence",
        expected_decision="direct_source_fallback",
        note="metadata.py:203-221 — degraded_subsystems non-empty or lag > 0.",
    ),
    ReasonFixture(
        id="consistency-unknown",
        freshness_status="fresh",
        freshness_reason=None,
        consistency_status="unknown",
        consistency_reason="consistency_unknown",
        fallback_reason="consistency_unknown",
        expected_decision="direct_source_fallback",
        note="metadata.py:228-232 — no lag/watermark evidence, projection-reading tool.",
    ),
    ReasonFixture(
        id="contract-mismatch-overrides-clean-evidence",
        freshness_status="fresh",
        freshness_reason=None,
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="contract_mismatch",
        expected_decision="direct_source_fallback",
        note=(
            "metadata.py:258-259 — contract_match=False or unpinnable commit "
            "always overrides an otherwise-clean freshness/consistency read."
        ),
    ),
    ReasonFixture(
        id="fallback-required-direct-source-also-unavailable-parks",
        freshness_status="degraded",
        freshness_reason="source_degraded",
        consistency_status="converged",
        consistency_reason=None,
        fallback_reason="source_degraded",
        expected_decision="park",
        direct_source_available=False,
        note="REQ-MCP-11 / REQ-KP-5 fallback clause — never invent context.",
    ),
)


# ---------------------------------------------------------------------------
# 2. REQ-MCP-4 payload-shape failures (server.py:156,158; response.schema.json:8)
# ---------------------------------------------------------------------------

PAYLOAD_SHAPE_FIXTURES: tuple[PayloadShapeFixture, ...] = (
    PayloadShapeFixture(
        id="response-invalid-not-finite-json",
        condition="response_invalid",
        error_code="response_invalid:MCP tool response is not finite JSON",
        expected_decision="direct_source_fallback",
        note="server.py:156 — never accept a truncated/invalid success.",
    ),
    PayloadShapeFixture(
        id="response-too-large",
        condition="response_too_large",
        error_code="response_too_large:MCP tool response exceeds the fixed byte budget",
        expected_decision="direct_source_fallback",
        note="server.py:158 — fixed 80,000-byte UTF-8 budget exceeded.",
    ),
    PayloadShapeFixture(
        id="missing-meta-block",
        condition="missing_meta",
        error_code=None,
        expected_decision="planning_only",
        note="response.schema.json:8 required:[meta] — planning-only, ties to REQ-KP-4.",
    ),
    PayloadShapeFixture(
        id="invalid-meta-block",
        condition="invalid_meta",
        error_code=None,
        expected_decision="planning_only",
        note="meta present but fails meta.schema.json validation — planning-only.",
    ),
)


# ---------------------------------------------------------------------------
# 3. canary.py contract-pin fail-closed codes (taxonomy §2) — the full set
#    spanning load_release_pin, verify_runtime_observation, and
#    transport/input validation. Every code means the contract identity
#    cannot be trusted, regardless of which stage raised it.
#    `decide_on_contract_pin_failure` is uniform (any non-empty code ->
#    direct_source_fallback), so these are data rows, not new branches to
#    test. Completeness against `canary.py`'s actual `_fail(...)` call
#    sites is enforced by
#    `test_contract_pin_fixtures_cover_every_canary_fail_closed_code`
#    (tests/test_consumer_severance_matrix.py).
# ---------------------------------------------------------------------------

# Every code raised via a literal `_fail("<code>")` call site in canary.py
# (`load_release_pin` static verification, `verify_runtime_observation`
# runtime checks, and endpoint/token/workspace/transport validation).
# Two of these (`manifest_invalid`, `schema_json_invalid`) are raised only
# via `_fail(code)` with `code` bound through a `code=` keyword argument to
# `_safe_read`/`_strict_object`, so a literal-only source scan of canary.py
# cannot see them — included here for genuine completeness even though the
# gate (a superset check) does not require it.
_STATIC_CONTRACT_PIN_CODES: tuple[str, ...] = (
    "json_value_invalid",
    "schema_path_invalid",
    "bundle_artifact_invalid",
    "bundle_file_set_invalid",
    "bundle_directory_invalid",
    "bundle_address_invalid",
    "manifest_invalid",
    "manifest_address_mismatch",
    "manifest_not_canonical",
    "manifest_shape_invalid",
    "manifest_contract_invalid",
    "tool_registry_record_invalid",
    "tool_registry_sha256_mismatch",
    "tool_registry_shape_invalid",
    "tool_registry_invalid",
    "schema_index_invalid",
    "schema_record_invalid",
    "schema_json_invalid",
    "schema_sha256_mismatch",
    "schema_index_not_sorted",
    "schema_set_sha256_mismatch",
    "tool_registry_schema_binding_invalid",
    "contract_info_timestamp_invalid",
    "contract_info_meta_shape_mismatch",
    "contract_info_meta_contract_mismatch",
    "contract_info_workspace_mismatch",
    "contract_info_timestamp_mismatch",
    "contract_info_freshness_mismatch",
    "contract_info_consistency_mismatch",
    "contract_info_fallback_required",
    "contract_info_fallback_mismatch",
    "contract_info_call_failed",
    "contract_info_structured_content_missing",
    "contract_info_content_mismatch",
    "runtime_tools_capability_missing",
    "runtime_tool_surface_mismatch",
    "runtime_input_schema_mismatch",
    "contract_info_shape_mismatch",
    "contract_info_manifest_mismatch",
    "endpoint_invalid",
    "token_invalid",
    "runtime_tool_surface_unbounded",
    "runtime_tool_pagination_invalid",
    "runtime_tool_pagination_unbounded",
    "transport_failed",
    "workspace_id_invalid",
)

# canary.py:531-543 interpolates `f"contract_info_{field}_mismatch"` for
# each key of the `expected_values` dict in `verify_runtime_observation` —
# not a string literal, so a source scan for `_fail("...")` can't see
# these 8 codes. Field names hardcoded here as ground truth.
_CONTRACT_INFO_FIELD_MISMATCH_FIELDS: tuple[str, ...] = (
    "version",
    "commit",
    "manifest_sha256",
    "schema_sha256",
    "schema_digests",
    "tool_registry_sha256",
    "token_profile_id",
    "capabilities",
)

CONTRACT_PIN_FIXTURES: tuple[ContractPinFixture, ...] = tuple(
    ContractPinFixture(
        id=f"contract-pin-{code}",
        canary_code=code,
        expected_decision="direct_source_fallback",
    )
    for code in (
        *_STATIC_CONTRACT_PIN_CODES,
        *(f"contract_info_{field}_mismatch" for field in _CONTRACT_INFO_FIELD_MISMATCH_FIELDS),
    )
)


def all_ids() -> frozenset[str]:
    return frozenset(
        f.id for f in (*REASON_FIXTURES, *PAYLOAD_SHAPE_FIXTURES, *CONTRACT_PIN_FIXTURES)
    )


def expected_decisions() -> dict[str, str]:
    decisions: dict[str, str] = {}
    for fixture in (*REASON_FIXTURES, *PAYLOAD_SHAPE_FIXTURES, *CONTRACT_PIN_FIXTURES):
        decisions[fixture.id] = fixture.expected_decision
    return decisions


def fixture_matrix_canonical_json() -> str:
    """Deterministic, content-addressable serialization of the full matrix.

    Consumers reproduce this exact byte sequence to compute the
    `fixture_matrix_sha256` their severance receipt must pin against.
    """
    payload = {
        "reason_fixtures": sorted(
            (
                {
                    "id": f.id,
                    "fallback_reason": f.fallback_reason,
                    "direct_source_available": f.direct_source_available,
                    "expected_decision": f.expected_decision,
                }
                for f in REASON_FIXTURES
            ),
            key=lambda row: row["id"],
        ),
        "payload_shape_fixtures": sorted(
            (
                {
                    "id": f.id,
                    "condition": f.condition,
                    "expected_decision": f.expected_decision,
                }
                for f in PAYLOAD_SHAPE_FIXTURES
            ),
            key=lambda row: row["id"],
        ),
        "contract_pin_fixtures": sorted(
            (
                {
                    "id": f.id,
                    "canary_code": f.canary_code,
                    "expected_decision": f.expected_decision,
                }
                for f in CONTRACT_PIN_FIXTURES
            ),
            key=lambda row: row["id"],
        ),
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def fixture_matrix_sha256() -> str:
    return hashlib.sha256(fixture_matrix_canonical_json().encode("utf-8")).hexdigest()


__all__ = [
    "CONTRACT_PIN_FIXTURES",
    "PAYLOAD_SHAPE_FIXTURES",
    "REASON_FIXTURES",
    "ContractPinFixture",
    "PayloadShapeFixture",
    "ReasonFixture",
    "all_ids",
    "expected_decisions",
    "fixture_matrix_canonical_json",
    "fixture_matrix_sha256",
]
