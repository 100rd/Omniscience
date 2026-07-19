"""Reference consumer decision rules for MCP v1 consumer-severance (AC-SEV-1/4).

Omniscience does not own or ship any real consumer (Omnius, the SRE
harness); this module is the producer's own encoding of the *consumer-side*
decision rule described in SPEC-MCP's Interfaces block, REQ-MCP-11,
REQ-KP-5, and SPEC-EV REQ-EV-7/REQ-EV-9 — the model the fixture matrix in
`fixtures.py` is asserted against. It never imports
`omniscience_server.mcp.metadata` / `.canary`.

Decision vocabulary
--------------------
- ``"use"`` — continue with the Omniscience response as one input among
  several (never as a sole oracle — see `is_sole_oracle`).
- ``"direct_source_fallback"`` — Omniscience evidence is unusable; consult
  the authoritative direct source (git/K8s/cloud/incident system) instead.
- ``"park"`` — both Omniscience and the direct source are unavailable;
  never invent context.
- ``"planning_only"`` (payload-shape failures only) — the response could
  not even be parsed for a fitness verdict; it may inform planning but can
  never gate anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

Decision = str

KNOWN_FALLBACK_REASONS: frozenset[str | None] = frozenset(
    {
        None,
        "consistency_unknown",
        "contract_mismatch",
        "freshness_sla_unknown",
        "missing_lineage",
        "never_synced",
        "projection_divergence",
        "source_degraded",
        "stale_source",
    }
)

TERMINAL_ACTIONS: tuple[str, ...] = ("merge", "apply", "incident_action", "terminal_decision")


def decide(*, fallback_required: bool, direct_source_available: bool = True) -> Decision:
    """SPEC-MCP Interfaces + REQ-MCP-11 + REQ-KP-5 decision rule (taxonomy §1g)."""
    if not fallback_required:
        return "use"
    return "direct_source_fallback" if direct_source_available else "park"


def decide_from_meta(
    meta: Mapping[str, Any],
    *,
    direct_source_available: bool = True,
) -> Decision:
    """Apply `decide` to a full MCP v1 `meta` block, failing closed on drift.

    An unrecognised `fallback.reason` (schema/enum drift) or a malformed/
    missing `fallback` block is treated the same as `contract_mismatch`:
    SPEC-EV REQ-EV-9 ("unknown major versions fail closed").
    """
    fallback = meta.get("fallback") if isinstance(meta, Mapping) else None
    if not isinstance(fallback, Mapping) or "required" not in fallback:
        return decide(fallback_required=True, direct_source_available=direct_source_available)
    required = bool(fallback["required"])
    reason = fallback.get("reason")
    # Fail closed on: unrecognised reason (schema/enum drift), or `required`
    # disagreeing with a known `reason` (defense-in-depth — a real producer
    # never emits a contradictory shape; metadata.py:271 enforces
    # `"required": reason is not None`, but this doesn't trust that in
    # isolation).
    if (
        reason not in KNOWN_FALLBACK_REASONS
        or (reason is not None and not required)
        or (reason is None and required)
    ):
        required = True
    return decide(fallback_required=required, direct_source_available=direct_source_available)


def decide_on_contract_pin_failure(
    canary_code: str,
    *,
    direct_source_available: bool = True,
) -> Decision:
    """Any canary fail-closed code (taxonomy §2) makes the contract unverifiable."""
    if not canary_code:
        raise ValueError("canary_code must be a non-empty fail-closed code")
    return decide(fallback_required=True, direct_source_available=direct_source_available)


def decide_on_payload_shape(condition: str) -> Decision:
    """REQ-MCP-4 payload-shape failures — never a `meta.fallback` state (taxonomy §1e)."""
    if condition in {"response_invalid", "response_too_large"}:
        return "direct_source_fallback"
    if condition in {"missing_meta", "invalid_meta"}:
        return "planning_only"
    raise ValueError(f"unknown payload-shape condition: {condition!r}")


def is_sole_oracle() -> bool:
    """AC-SEV-4 / REQ-KP-3: no decision, not even `use`, is ever a sole oracle.

    This documents *intent*, not enforced behavior: it is a constant-return
    stub, not a check against real Omnius/SRE gate logic (which lives
    outside this repo and cannot be verified here — same limitation as
    AC-SEV-2/3). No decision value ever changes the answer, so the
    function intentionally takes none.
    """
    return False


def requires_independent_authority(action: str) -> bool:
    """AC-SEV-4: merge/apply/incident-action/terminal decisions always need it.

    Also documents intent, not enforced behavior — see `is_sole_oracle`.
    """
    if action not in TERMINAL_ACTIONS:
        raise ValueError(f"unknown terminal action: {action!r}")
    return True


__all__ = [
    "KNOWN_FALLBACK_REASONS",
    "TERMINAL_ACTIONS",
    "Decision",
    "decide",
    "decide_from_meta",
    "decide_on_contract_pin_failure",
    "decide_on_payload_shape",
    "is_sole_oracle",
    "requires_independent_authority",
]
