"""Blast-radius orchestration layer (issue #234, Wave 2 of epic #230 / H5 Track 1).

FastAPI wiring layer for the ``blast_radius`` MCP/REST tool.  Pure domain
logic lives in :mod:`omniscience_retrieval.blast_radius`; this module
composes the ``GraphStore`` I/O with the domain primitives.

ACL invariant (non-negotiable)
------------------------------

- ``workspace_id`` is taken from the caller's bearer token, never from
  input.  Every store call carries it (issue #117 / ADR-0005).
- A foreign-workspace ``entity_id`` returns ``entity_not_found``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from omniscience_core.storage import GraphResultView, GraphStore

# Re-export domain types and constants for backward-compatible imports.
from omniscience_retrieval.blast_radius import (
    ACTION_EDGE_ALLOWLIST as _ACTION_EDGE_ALLOWLIST,
)
from omniscience_retrieval.blast_radius import (
    ACTION_TYPES,
    DEFAULT_MAX_DEPTH,
    ENTITY_NOT_FOUND_CODE,
    INVALID_ACTION_TYPE_CODE,
    INVALID_ACTION_TYPE_MESSAGE,
    INVALID_ENTITY_ID_CODE,
    INVALID_ENTITY_ID_MESSAGE,
    MAX_MAX_DEPTH,
    MIN_MAX_DEPTH,
    ActionType,
    BlastRadiusImpact,
    BlastRadiusResponse,
    DependencyPathStep,
)
from omniscience_retrieval.blast_radius import (
    build_blast_meta as _build_meta,
)
from omniscience_retrieval.blast_radius import (
    build_blast_response as _build_response,
)
from omniscience_retrieval.blast_radius import (
    score_impacts as _score_impacts,
)
from omniscience_retrieval.blast_radius import (
    validate_action_type as _validate_action_type,
)
from omniscience_retrieval.blast_radius import (
    validate_entity_id as _validate_entity_id,
)

from omniscience_server.as_of import (
    DEGRADED_PRE_HISTORY,
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)


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
        Canonical name of the seed entity.
    workspace_id:
        REQUIRED.  Forwarded to every ``GraphStore`` call.
    action_type:
        Which action's blast radius to compute.  Default ``"restart"``.
    as_of:
        Optional ADR-0008 §5 bitemporal anchor.
    max_depth:
        BFS depth from the seed.  Clamped to ``[MIN_MAX_DEPTH, MAX_MAX_DEPTH]``.
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
            effective_as_of=resolve_effective_as_of(normalised_as_of),
            meta=meta,
            degraded_pre_history=DEGRADED_PRE_HISTORY,
        ).model_dump(mode="json")


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
