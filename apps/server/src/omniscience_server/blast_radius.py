"""Blast-radius composition (issue #234, Wave 2 of epic #230 / H5 Track 1).

This module is the design centre of the change-management surface: a
single MCP/REST tool — :func:`blast_radius` — that answers "if we
perform ``action_type`` on ``entity_id``, what downstream entities are
impacted?".  It composes the existing bitemporal graph traversal
(``GraphStore.find_related`` -> ``_TRAVERSE_*_CYPHER_TEMPLATE``) with
an action-aware edge-type allowlist and an impact-scoring heuristic.

Composition shape
-----------------

1. **Validate** ``entity_id``: must be a non-empty string; canonical
   entity names follow the same shape as the other tools (``service://``,
   ``pod/``, ``arn:`` etc.).  No prefix is enforced — the seed is
   resolved by whatever canonical name the connectors planted.
2. **Resolve** the seed via :meth:`GraphStore.get_entity` (workspace-
   scoped, ``as_of``-aware).  Missing seed -> ``entity_not_found``.
3. **Traverse** outward via :meth:`GraphStore.find_related` with the
   ``edge_types`` allowlist for the requested action_type — restricts
   the BFS to causal edges that propagate the action's effect (e.g.
   ``DEPENDS_ON``, ``ROUTES_TO``).
4. **Score** each neighbour:

   * ``impact_score`` = action multiplier * depth decay * edge weight.
     Always in [0.0, 1.0].  Closer hops + first-class causal edges
     score higher.  Deterministic v0.1 placeholder per the issue spec.
   * ``confidence`` = 1.0 when both the seed and the neighbour carry
     a non-tombstoned bitemporal triple at ``as_of``; 0.5 when
     bitemporal state is partially missing (legacy pre-#130 rows);
     1.0 also when ``as_of`` is None (still-current read).
5. **Rank** descending by ``impact_score`` (ties broken by depth, then
   by lexicographic ``entity_id`` for determinism).

Action-type semantics (see ``docs/api/blast-radius.md``)
--------------------------------------------------------

* ``restart``    — transient unavailability; follows runtime edges
                   (``DEPENDS_ON``, ``ROUTES_TO``, ``CALLS``).
* ``delete``     — permanent removal; widest blast (all causal edges
                   that the other actions cover, plus ownership edges
                   ``OWNED_BY``, ``DEPLOYED_BY``).
* ``scale_down`` — degraded capacity; same as ``restart`` plus
                   load-bearing ``LOAD_BALANCED_BY``.
* ``cordon``     — quarantine (no new work); follows
                   ``SCHEDULED_ON`` / ``RUNS_ON`` only — does NOT
                   propagate through call/route edges (the workload
                   itself keeps serving until evicted).

ACL invariant (non-negotiable)
------------------------------

- ``workspace_id`` is taken from the caller's bearer token, never from
  input.  Every store call carries it (issue #117 / ADR-0005).
- A foreign-workspace ``entity_id`` returns ``entity_not_found`` — the
  same response a non-existent entity would produce.  Existence is
  never leaked.

Bitemporal predicate (ADR-0008 §5)
----------------------------------

``as_of`` is forwarded verbatim to ``GraphStore``.  When set, every
node and every edge in the traversal satisfies the canonical predicate
``valid_from <= as_of AND (as_of < valid_to OR valid_to IS NULL)``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from fastapi import FastAPI
from omniscience_core.storage import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
    GraphStore,
)
from pydantic import BaseModel, Field

from omniscience_server.as_of import (
    DEGRADED_PRE_HISTORY,
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)

# ---------------------------------------------------------------------------
# Public action-type contract
# ---------------------------------------------------------------------------

#: Literal of supported action_type values.  Adding a new action is a
#: minor-version change; removing one is a major-version break.
ActionType = Literal["restart", "delete", "scale_down", "cordon"]

#: Tuple of all supported action types — used by validators and the
#: REST/MCP schema layers so the docs and code stay in sync.
ACTION_TYPES: Final[tuple[ActionType, ...]] = ("restart", "delete", "scale_down", "cordon")


# ---------------------------------------------------------------------------
# Depth bounds — pinned to the same envelope as resolve_incident
# ---------------------------------------------------------------------------

#: Default BFS depth when the caller does not override ``max_depth``.
#: Issue #234 spec: depth 3 covers seed -> direct dep -> transitive
#: dep -> consumer's consumer; deeper traversals add noise.
DEFAULT_MAX_DEPTH: Final[int] = 3

#: Lower bound for ``max_depth``.  Depth < 1 makes the traversal a
#: degenerate single-entity lookup with no downstream set.
MIN_MAX_DEPTH: Final[int] = 1

#: Upper bound for ``max_depth``.  Pinned to the same envelope as
#: ``resolve_incident`` (issue #153 :data:`MAX_MAX_DEPTH`) so a hub
#: entity cannot blow up the response payload.
MAX_MAX_DEPTH: Final[int] = 5


# ---------------------------------------------------------------------------
# Error codes (mirrored on the MCP / REST surfaces)
# ---------------------------------------------------------------------------

ENTITY_NOT_FOUND_CODE: Final[str] = "entity_not_found"
INVALID_ACTION_TYPE_CODE: Final[str] = "invalid_action_type"
INVALID_ACTION_TYPE_MESSAGE: Final[str] = "action_type must be one of: " + ", ".join(ACTION_TYPES)
INVALID_ENTITY_ID_CODE: Final[str] = "invalid_entity_id"
INVALID_ENTITY_ID_MESSAGE: Final[str] = "entity_id must be a non-empty string"


# ---------------------------------------------------------------------------
# Action-type -> edge-type allowlist
# ---------------------------------------------------------------------------

#: Edges that propagate a runtime outage (transient unavailability).
#: A restart of X breaks anything that calls X over the network or
#: declares a hard runtime dependency on X.
_RUNTIME_EDGES: Final[frozenset[str]] = frozenset(
    {
        "DEPENDS_ON",
        "ROUTES_TO",
        "CALLS",
    }
)

#: Edges that carry traffic load — relevant for scale-down (degraded
#: capacity) on top of runtime edges.
_LOAD_EDGES: Final[frozenset[str]] = frozenset({"LOAD_BALANCED_BY"})

#: Ownership / deployment edges — relevant only for delete, which also
#: severs the management plane (CI/CD pipelines, owning teams).
_OWNERSHIP_EDGES: Final[frozenset[str]] = frozenset({"OWNED_BY", "DEPLOYED_BY"})

#: Scheduling edges — relevant for cordon (workloads stay on a node
#: until they are evicted; new work won't land there).
_SCHEDULING_EDGES: Final[frozenset[str]] = frozenset({"SCHEDULED_ON", "RUNS_ON"})

#: Action-type -> follow set.  See module docstring for semantics.
_ACTION_EDGE_ALLOWLIST: Final[dict[ActionType, frozenset[str]]] = {
    "restart": _RUNTIME_EDGES,
    "delete": _RUNTIME_EDGES | _LOAD_EDGES | _OWNERSHIP_EDGES | _SCHEDULING_EDGES,
    "scale_down": _RUNTIME_EDGES | _LOAD_EDGES,
    "cordon": _SCHEDULING_EDGES,
}


# ---------------------------------------------------------------------------
# Impact-score heuristic (v0.1 deterministic placeholder)
# ---------------------------------------------------------------------------

#: Multiplier applied to the raw (depth * edge) score, per action type.
#: ``delete`` is the worst-case action and pegs the multiplier at 1.0;
#: the others rank below it on the same scale so cross-action comparisons
#: remain meaningful.
_ACTION_MULTIPLIER: Final[dict[ActionType, float]] = {
    "delete": 1.0,
    "restart": 0.8,
    "scale_down": 0.6,
    "cordon": 0.5,
}

#: Per-edge-type weight applied to the impact score.  Causal-runtime
#: edges weigh more than load/ownership edges so a calling service ranks
#: above a load-balancer fronting the same target.
_EDGE_WEIGHT: Final[dict[str, float]] = {
    "DEPENDS_ON": 1.0,
    "CALLS": 1.0,
    "ROUTES_TO": 0.9,
    "LOAD_BALANCED_BY": 0.7,
    "SCHEDULED_ON": 0.7,
    "RUNS_ON": 0.7,
    "DEPLOYED_BY": 0.5,
    "OWNED_BY": 0.4,
}

#: Default edge weight when the connector emits an edge type that is
#: not in :data:`_EDGE_WEIGHT`.  Conservative middle-of-the-road value
#: so unknown but action-allowlisted edges still rank.
_DEFAULT_EDGE_WEIGHT: Final[float] = 0.6

#: Depth-decay factor.  Score at depth d is ``base * (DECAY ** (d - 1))``,
#: i.e. the first hop is the full score, every extra hop multiplies
#: by ``DECAY``.  0.6 yields scores ~ 1.0 / 0.6 / 0.36 / 0.216 across
#: depths 1..4 — comfortably distinguishable, never zero.
_DEPTH_DECAY: Final[float] = 0.6

#: Confidence when the bitemporal triple is fully populated at ``as_of``
#: (or when ``as_of`` is ``None`` and we trust the current-state mirror).
_CONFIDENCE_FULL: Final[float] = 1.0

#: Confidence when the row is legacy (pre-#130 bitemporal backfill) and
#: the bitemporal triple is partial.  We still surface the entity but
#: signal to the caller that the time-anchoring is best-effort.
_CONFIDENCE_PARTIAL: Final[float] = 0.5


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------


class DependencyPathStep(BaseModel):
    """One hop in the dependency path from seed to impacted entity."""

    from_entity: str = Field(description="Canonical name of the upstream entity.")
    to_entity: str = Field(description="Canonical name of the downstream entity.")
    edge_type: str = Field(description="Causal edge type connecting the two entities.")


class BlastRadiusImpact(BaseModel):
    """Single impacted entity in the blast-radius response."""

    entity_id: str = Field(description="Canonical name of the impacted entity.")
    entity_type: str = Field(description="Entity kind (e.g. 'service', 'pod', 'function').")
    dependency_path: list[DependencyPathStep] = Field(
        default_factory=list,
        description=(
            "Best-effort BFS path from the seed to this entity.  v0.1 "
            "surfaces the last hop only when the underlying traversal "
            "did not preserve the full path."
        ),
    )
    impact_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Deterministic v0.1 heuristic: action multiplier * depth "
            "decay * edge weight, clipped to [0.0, 1.0]."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in the impact assessment.  1.0 when the "
            "bitemporal triple is fully populated; 0.5 for legacy "
            "rows that pre-date the #130 backfill."
        ),
    )


class BlastRadiusResponse(BaseModel):
    """Top-level response envelope for ``blast_radius``."""

    seed_entity_id: str = Field(description="Canonical name of the seed entity.")
    action_type: ActionType = Field(description="Action whose blast radius was computed.")
    max_depth: int = Field(
        ge=MIN_MAX_DEPTH,
        le=MAX_MAX_DEPTH,
        description="Maximum BFS depth that was used for the traversal.",
    )
    impacted: list[BlastRadiusImpact] = Field(
        default_factory=list,
        description="Impacted entities ranked descending by impact_score.",
    )
    effective_as_of: datetime
    meta: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScoredImpact:
    """Internal representation; serialised through :class:`BlastRadiusImpact`."""

    entity_id: str
    entity_type: str
    depth: int
    edge_type: str
    impact_score: float
    confidence: float
    path: tuple[DependencyPathStep, ...]


# ---------------------------------------------------------------------------
# Public composition entry point
# ---------------------------------------------------------------------------


async def mcp_blast_radius(
    app: FastAPI,
    entity_id: str,
    workspace_id: uuid.UUID,
    action_type: ActionType = "restart",
    as_of: datetime | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Resolve the blast-radius bundle for ``entity_id`` under ``action_type``.

    Parameters
    ----------
    app:
        FastAPI app exposing ``app.state.graph_store``.
    entity_id:
        Canonical name of the seed entity (matches the planted
        ``Entity.name`` from the connectors).
    workspace_id:
        REQUIRED.  Forwarded to every ``GraphStore`` call.
    action_type:
        Which action's blast radius to compute.  Picks the edge-type
        allowlist and the score multiplier.  Default ``"restart"`` —
        the most common change-management operation.
    as_of:
        Optional ADR-0008 §5 bitemporal anchor.  Reuses
        :func:`enforce_utc` and the surrounding handler's parser.
    max_depth:
        BFS depth from the seed.  Clamped to
        ``[MIN_MAX_DEPTH, MAX_MAX_DEPTH]``; default
        :data:`DEFAULT_MAX_DEPTH`.

    Returns
    -------
    dict[str, Any]
        JSON-serialised :class:`BlastRadiusResponse`.

    Raises
    ------
    ValueError
        - ``invalid_entity_id:...`` — empty / non-string seed.
        - ``invalid_action_type:...`` — unsupported action.
        - ``entity_not_found:...`` — seed not visible in the caller's
          workspace at ``as_of`` (cross-workspace looks identical).
    """
    _validate_entity_id(entity_id)
    _validate_action_type(action_type)
    normalised_as_of = enforce_utc(as_of)
    clamped_depth = max(MIN_MAX_DEPTH, min(max_depth, MAX_MAX_DEPTH))

    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")

    with record_request_duration(
        surface="mcp", tool="blast_radius", as_of=normalised_as_of
    ) as patcher:
        seed = await graph_store.get_entity(
            entity_name=entity_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if seed is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
            raise ValueError(f"{ENTITY_NOT_FOUND_CODE}:{entity_id}")

        allowlist = sorted(_ACTION_EDGE_ALLOWLIST[action_type])
        try:
            graph_result = await graph_store.find_related(
                entity_name=entity_id,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                max_depth=clamped_depth,
                edge_types=allowlist,
            )
        except ValueError as exc:
            # Seed disappeared between calls — race with re-ingestion.
            # Return an empty downstream set rather than 404, mirroring
            # the empty-graph contract of resolve_incident (#153).
            if str(exc).startswith("entity_not_found:"):
                graph_result = GraphResultView(seed=seed, related=[], edges=[])
            else:
                raise

        impacts = _score_impacts(
            result=graph_result,
            action_type=action_type,
            as_of=normalised_as_of,
        )
        meta = _build_meta(
            action_type=action_type,
            edge_allowlist=allowlist,
            depth=clamped_depth,
        )
        return _build_response(
            seed=seed,
            action_type=action_type,
            max_depth=clamped_depth,
            impacts=impacts,
            as_of=normalised_as_of,
            meta=meta,
        ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_entity_id(entity_id: str) -> None:
    """Reject empty / non-string seeds at the API boundary."""
    if not isinstance(entity_id, str):  # pragma: no cover - typing barrier
        raise ValueError(f"{INVALID_ENTITY_ID_CODE}:{INVALID_ENTITY_ID_MESSAGE}")
    if not entity_id.strip():
        raise ValueError(f"{INVALID_ENTITY_ID_CODE}:{INVALID_ENTITY_ID_MESSAGE}")


def _validate_action_type(action_type: str) -> None:
    """Reject action_type values that fall outside :data:`ACTION_TYPES`."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"{INVALID_ACTION_TYPE_CODE}:{INVALID_ACTION_TYPE_MESSAGE}")


# ---------------------------------------------------------------------------
# Scoring + path reconstruction — pure functions, easy to unit-test
# ---------------------------------------------------------------------------


def _score_impacts(
    *,
    result: GraphResultView,
    action_type: ActionType,
    as_of: datetime | None,
) -> list[_ScoredImpact]:
    """Score and rank the impacted entities.

    De-duplicates by ``entity_id`` (a hub neighbour reached via multiple
    edges keeps its highest-scoring hop) and returns a list ranked
    descending by ``impact_score``.  Tie-broken by depth (closer first),
    then by lexicographic ``entity_id`` for determinism.
    """
    edge_index = _build_edge_index(result.edges, seed_name=result.seed.name)
    multiplier = _ACTION_MULTIPLIER[action_type]
    best: dict[str, _ScoredImpact] = {}
    for neighbour in result.related:
        if neighbour.name == result.seed.name:
            continue
        edge_type = neighbour.edge_type or _infer_edge_type(neighbour, edge_index)
        if edge_type is None:
            # No edge_type recoverable — the traversal allowlist filters
            # at the Cypher layer, so this can only happen for legacy
            # rows.  Skip to keep the response truthful.
            continue
        score = _impact_score(
            depth=neighbour.depth,
            edge_type=edge_type,
            multiplier=multiplier,
        )
        confidence = _confidence(
            seed_recorded=result.seed.recorded_at is not None, neighbour=neighbour, as_of=as_of
        )
        path = _path_for(
            neighbour=neighbour,
            edge_index=edge_index,
            seed_name=result.seed.name,
            edge_type=edge_type,
        )
        candidate = _ScoredImpact(
            entity_id=neighbour.name,
            entity_type=neighbour.kind,
            depth=neighbour.depth,
            edge_type=edge_type,
            impact_score=score,
            confidence=confidence,
            path=path,
        )
        prior = best.get(neighbour.name)
        if prior is None or candidate.impact_score > prior.impact_score:
            best[neighbour.name] = candidate
    ranked = sorted(
        best.values(),
        key=lambda imp: (-imp.impact_score, imp.depth, imp.entity_id),
    )
    return ranked


def _build_edge_index(
    edges: list[GraphEdgeView],
    *,
    seed_name: str,
) -> dict[str, GraphEdgeView]:
    """Map ``to_entity`` -> the last GraphEdgeView that reaches it.

    Used as a fallback for neighbours whose ``edge_type`` was not
    populated by the traversal (legacy rows).  We only need
    ``to_entity`` since the traversal Cypher emits edges as
    ``[start, end, edge_type, ...]``; the start of the last hop is what
    populates ``neighbour.edge_type`` in the post-#132 templates.

    ``seed_name`` is accepted so a future refinement (multi-hop path
    reconstruction) can prefer edges that anchor at the seed.
    """
    index: dict[str, GraphEdgeView] = {}
    for edge in edges:
        # Keep the first edge that reaches a given to_entity.  Ties are
        # rare with the current traversal shape (one edge per neighbour
        # in the collected last-hop tuple).
        index.setdefault(edge.to_entity, edge)
        # Also index by from_entity for undirected lookups — the
        # traversal walks identity-to-identity edges without direction.
        index.setdefault(edge.from_entity, edge)
    # The seed itself should never be returned as an impact; pop it so a
    # caller iterating the index does not accidentally surface it.
    index.pop(seed_name, None)
    return index


def _infer_edge_type(
    neighbour: EntityNodeView,
    edge_index: dict[str, GraphEdgeView],
) -> str | None:
    """Fallback edge-type recovery from the edge collection."""
    edge = edge_index.get(neighbour.name)
    return edge.edge_type if edge else None


def _impact_score(*, depth: int, edge_type: str, multiplier: float) -> float:
    """Compute the v0.1 deterministic impact score.

    ``impact = multiplier * (DECAY ** (depth - 1)) * edge_weight``,
    clipped to ``[0.0, 1.0]``.  Depth 0 (the seed itself) is filtered
    out before this is called, so ``depth >= 1`` is guaranteed.
    """
    decay = _DEPTH_DECAY ** max(depth - 1, 0)
    weight = _EDGE_WEIGHT.get(edge_type, _DEFAULT_EDGE_WEIGHT)
    raw = multiplier * decay * weight
    if raw < 0.0:
        return 0.0
    if raw > 1.0:
        return 1.0
    return raw


def _confidence(
    *,
    seed_recorded: bool,
    neighbour: EntityNodeView,
    as_of: datetime | None,
) -> float:
    """Confidence v0.1: full when the bitemporal triple is populated.

    Pre-#130 legacy rows leave ``recorded_at`` / ``valid_from`` ``None``
    and we cannot prove they were valid at ``as_of`` — surface the
    partial confidence so callers can filter.  Current-state reads
    (``as_of`` is ``None``) always return full confidence because the
    ``:Entity`` mirror IS the source of truth.
    """
    if as_of is None:
        return _CONFIDENCE_FULL
    neighbour_recorded = neighbour.recorded_at is not None
    if seed_recorded and neighbour_recorded:
        return _CONFIDENCE_FULL
    return _CONFIDENCE_PARTIAL


def _path_for(
    *,
    neighbour: EntityNodeView,
    edge_index: dict[str, GraphEdgeView],
    seed_name: str,
    edge_type: str,
) -> tuple[DependencyPathStep, ...]:
    """Best-effort dependency-path reconstruction.

    The traversal Cypher (``_TRAVERSE_*_CYPHER_TEMPLATE``) returns only
    the **last hop** that reaches each neighbour, not the full path.
    A future refinement (issue follow-up) can swap in a path-emitting
    template; for v0.1 we surface that last hop plus a synthetic
    seed -> ... -> neighbour edge so callers can render a chain UI.
    """
    edge = edge_index.get(neighbour.name)
    if edge is None:
        # Fallback: synthesise a direct edge from the seed.  Honest
        # about being best-effort; the surrounding score still reflects
        # the real depth.
        return (
            DependencyPathStep(
                from_entity=seed_name,
                to_entity=neighbour.name,
                edge_type=edge_type,
            ),
        )
    return (
        DependencyPathStep(
            from_entity=edge.from_entity,
            to_entity=edge.to_entity,
            edge_type=edge.edge_type,
        ),
    )


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _build_meta(
    *,
    action_type: ActionType,
    edge_allowlist: list[str],
    depth: int,
) -> dict[str, Any]:
    """Attach the explainability hints expected by ops dashboards."""
    return {
        "action_type": action_type,
        "edge_allowlist": list(edge_allowlist),
        "max_depth": depth,
        "scoring_model": "v0.1-deterministic",
    }


def _build_response(
    *,
    seed: EntityNodeView,
    action_type: ActionType,
    max_depth: int,
    impacts: list[_ScoredImpact],
    as_of: datetime | None,
    meta: dict[str, Any],
) -> BlastRadiusResponse:
    """Convert internal :class:`_ScoredImpact` list into the wire format."""
    impacted = [
        BlastRadiusImpact(
            entity_id=imp.entity_id,
            entity_type=imp.entity_type,
            dependency_path=list(imp.path),
            impact_score=imp.impact_score,
            confidence=imp.confidence,
        )
        for imp in impacts
    ]
    if as_of is not None and not impacts:
        # An empty downstream set under as_of could be pre-history; the
        # caller can disambiguate via meta.degraded_response.
        meta = {**meta, "degraded_response": DEGRADED_PRE_HISTORY}
    return BlastRadiusResponse(
        seed_entity_id=seed.name,
        action_type=action_type,
        max_depth=max_depth,
        impacted=impacted,
        effective_as_of=resolve_effective_as_of(as_of),
        meta=meta,
    )


__all__ = [
    "ACTION_TYPES",
    "DEFAULT_MAX_DEPTH",
    "ENTITY_NOT_FOUND_CODE",
    "INVALID_ACTION_TYPE_CODE",
    "INVALID_ACTION_TYPE_MESSAGE",
    "INVALID_ENTITY_ID_CODE",
    "INVALID_ENTITY_ID_MESSAGE",
    "MAX_MAX_DEPTH",
    "MIN_MAX_DEPTH",
    "ActionType",
    "BlastRadiusImpact",
    "BlastRadiusResponse",
    "DependencyPathStep",
    "mcp_blast_radius",
]
