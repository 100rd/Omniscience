"""Conformance tests — uncalibrated placeholder scores are tagged on the wire.

Review action point: "Tag placeholder confidence/impact scores as
uncalibrated on the wire contract".

Rationale: ``resolve_incident``'s ``resolution_confidence`` and
``blast_radius``'s ``impact_score`` are both v0.1 heuristic placeholders,
not outputs of a fitted/calibrated model.  ``resolve_incident`` already
carries a first-class ``calibrated: bool`` Pydantic field (AP4) so callers
can tell a real calibrated decimal apart from the heuristic ladder.
``blast_radius`` must carry the same signal — a bare ``impact_score``
float with no calibration marker invites LLM automation to treat it as a
grounded probability.

This file asserts:

1. ``BlastRadiusResponse`` declares a first-class ``calibrated: bool``
   field (not injected post-``model_dump``), defaulting to ``False`` —
   mirroring ``ResolveIncidentResponse.calibrated`` (AP4).
2. The field is present in the model's JSON schema as a boolean, so SDK
   codegen and REST/MCP consumers alike see it on the wire.
3. The live ``mcp_blast_radius`` path (shared by the MCP tool and the
   REST endpoint — both surfaces return this dict verbatim) tags its
   v0.1-deterministic response with ``calibrated: False``.
4. The ``calibrated`` flag and ``meta.scoring_model`` never disagree:
   as long as blast-radius has no fitted-model path, a deterministic
   ``scoring_model`` must always be paired with ``calibrated=False``.
5. Both wire contracts (``resolve_incident`` and ``blast_radius``) use
   the same field name (``calibrated``) and the same semantics (``False``
   when a v0.1 heuristic/placeholder produced the score) — a consistent
   vocabulary is what lets a generic client gate on "is this number
   grounded" without tool-specific logic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from omniscience_core.storage.graph import EntityNodeView, GraphResultView
from omniscience_retrieval.blast_radius import BlastRadiusResponse
from omniscience_retrieval.incidents.resolution import ResolveIncidentResponse
from omniscience_server.blast_radius import mcp_blast_radius

pytestmark = pytest.mark.asyncio

_WS = uuid.UUID("cccccccc-0000-0000-0000-c0000000c001")
_SEED = "service://checkout-api"
_DEP = "service://payments-api"


class _SimpleBlastStore:
    """Minimal fake graph store: one seed, one direct CALLS dependency."""

    def __init__(self) -> None:
        self._seed = EntityNodeView(
            id=uuid.uuid4(),
            name=_SEED,
            kind="service",
            source="src-test",
            chunk_text=None,
            depth=0,
            edge_type=None,
            recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self._dep = EntityNodeView(
            id=uuid.uuid4(),
            name=_DEP,
            kind="service",
            source="src-test",
            chunk_text=None,
            depth=1,
            edge_type="CALLS",
            recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def get_entity(
        self, *, entity_name: str, workspace_id: uuid.UUID, as_of: Any | None = None
    ) -> EntityNodeView | None:
        if entity_name == self._seed.name and workspace_id == _WS:
            return self._seed
        return None

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: Any | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return GraphResultView(seed=self._seed, related=[self._dep], edges=[])


def _make_app(store: _SimpleBlastStore) -> FastAPI:
    app = FastAPI()
    app.state.graph_store = store
    return app


# ---------------------------------------------------------------------------
# 1 + 2. Schema-level: calibrated is a first-class, boolean, defaulted field.
# ---------------------------------------------------------------------------


def test_blast_radius_response_declares_calibrated_field() -> None:
    """``calibrated`` must be a declared Pydantic field, defaulting to False.

    Mirrors ``ResolveIncidentResponse.calibrated`` (AP4) — a caller must be
    able to introspect the schema (or the JSON payload) and know the
    ``impact_score`` values are v0.1 heuristic placeholders, not a
    calibrated probability.
    """
    fields = BlastRadiusResponse.model_fields
    assert "calibrated" in fields, (
        "calibrated must be a declared Pydantic field on BlastRadiusResponse, "
        "mirroring ResolveIncidentResponse.calibrated (AP4)"
    )
    assert fields["calibrated"].default is False, (
        "calibrated must default to False — no fitted/calibrated blast-radius "
        "model exists yet, so the field must never silently default to True"
    )
    assert fields["calibrated"].annotation is bool


def test_blast_radius_calibrated_field_is_boolean_in_json_schema() -> None:
    """The field must appear in the wire-level JSON schema as a boolean.

    This is what SDK codegen / REST consumers see — the flag has to be
    part of the actual schema, not just a runtime-only Python attribute.
    """
    schema = BlastRadiusResponse.model_json_schema()
    properties = schema.get("properties", {})
    assert "calibrated" in properties, (
        "calibrated must appear in BlastRadiusResponse's JSON schema "
        "(model_json_schema) so it round-trips through MCP/REST/SDK codegen"
    )
    assert properties["calibrated"].get("type") == "boolean"


# ---------------------------------------------------------------------------
# 3. Live end-to-end path: the v0.1 heuristic response tags calibrated=False.
# ---------------------------------------------------------------------------


async def test_blast_radius_live_response_tags_uncalibrated() -> None:
    """mcp_blast_radius (shared by MCP tool + REST endpoint) must emit
    ``calibrated: False`` for the current v0.1-deterministic heuristic.

    Both the MCP tool and the REST ``GET /api/v1/blast-radius`` handler
    return this dict verbatim, so this single assertion covers both wire
    surfaces.
    """
    store = _SimpleBlastStore()
    app = _make_app(store)

    result = await mcp_blast_radius(
        app=app,
        entity_id=_SEED,
        workspace_id=_WS,
        action_type="restart",
    )

    assert "calibrated" in result, "calibrated must be present on the wire response"
    assert result["calibrated"] is False, (
        "blast_radius has no fitted/calibrated model yet; calibrated must be "
        f"False for the v0.1 heuristic path, got {result['calibrated']!r}"
    )
    # The impact scores themselves are still returned (ranking signal) —
    # this feature is additive, not a suppression of impact_score.
    assert result["impacted"], "fixture must produce at least one impacted entity"
    for impact in result["impacted"]:
        assert 0.0 <= impact["impact_score"] <= 1.0


async def test_blast_radius_calibrated_flag_agrees_with_scoring_model_meta() -> None:
    """``calibrated`` and ``meta.scoring_model`` must never disagree.

    ``meta.scoring_model`` already documents "v0.1-deterministic" for the
    heuristic path.  A deterministic scoring_model must always pair with
    ``calibrated=False`` — otherwise a caller reading one field would be
    contradicted by the other.
    """
    store = _SimpleBlastStore()
    app = _make_app(store)

    result = await mcp_blast_radius(
        app=app,
        entity_id=_SEED,
        workspace_id=_WS,
        action_type="restart",
    )

    scoring_model = result.get("meta", {}).get("scoring_model")
    if scoring_model == "v0.1-deterministic":
        assert result["calibrated"] is False, (
            "meta.scoring_model='v0.1-deterministic' must pair with "
            "calibrated=False; got calibrated="
            f"{result['calibrated']!r}"
        )


# ---------------------------------------------------------------------------
# 5. Cross-tool contract consistency: same field name, same semantics.
# ---------------------------------------------------------------------------


def test_calibrated_field_vocabulary_matches_resolve_incident() -> None:
    """``blast_radius`` and ``resolve_incident`` must speak the same
    calibration vocabulary.

    A generic MCP/REST/SDK consumer should be able to check
    ``response["calibrated"]`` across every Omniscience tool without
    tool-specific branching to know whether a numeric score is grounded
    in a fitted model or is still a v0.1 placeholder.
    """
    incident_fields = ResolveIncidentResponse.model_fields
    blast_fields = BlastRadiusResponse.model_fields

    assert "calibrated" in incident_fields
    assert "calibrated" in blast_fields
    assert incident_fields["calibrated"].annotation is bool
    assert blast_fields["calibrated"].annotation is bool
    # Both default to False — neither tool has a fitted model that is
    # trusted-by-default; calibration must be proven, not assumed.
    assert incident_fields["calibrated"].default is False
    assert blast_fields["calibrated"].default is False
