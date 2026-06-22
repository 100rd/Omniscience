"""Terraform config <-> state drift endpoint (Gap C, thin exposure stub).

POST /api/v1/drift/terraform

Computes config<->state drift via the pure engine
``omniscience_parsers.infra.compute_tf_drift`` (which walks the ``tfstate_of``
graph: state-not-in-config / config-not-in-state / attribute divergence).

Why a stub
----------
The engine is final and fully tested.  This endpoint is intentionally a thin
exposure stub: it parses the Terraform config + state documents supplied IN THE
REQUEST BODY, builds the infra graph in-process, and runs the engine.

Eventually it should switch to the persisted, Neo4j-backed ``tfstate_of`` graph
once ``extract_infra_graph`` is wired into ingestion (it is not today).  The
target shape is: resolve the caller's Terraform resources + tfstate instances
from the graph store (workspace-scoped, like the other graph endpoints), hand
the resulting ``EntityData``/``EdgeData`` to ``compute_tf_drift``, and drop the
request-body documents.  The handler -> engine call (``compute_tf_drift`` +
``drift_report_to_dict``) stays identical; only the graph SOURCE changes.

Auth
----
* No / invalid bearer token             -> 401 ``unauthorized``
* Token without ``search`` scope         -> 403 ``forbidden`` (scope)
* Authenticated + scoped                  -> 200 with the drift report JSON
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_parsers.infra import (
    TerraformParser,
    TfStateParser,
    compute_tf_drift,
    drift_report_to_dict,
    extract_infra_graph,
)
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/drift", tags=["drift"])

# Module-level singleton — avoid ruff B008 (function call in argument default).
_search_dep: Any = Depends(require_scope(Scope.search))


class TerraformDriftRequest(BaseModel):
    """Config + state documents to diff (thin-stub input — see module docstring)."""

    config_documents: list[str] = Field(
        default_factory=list,
        description="Raw Terraform configuration documents (HCL or tf.json).",
    )
    state_document: str | None = Field(
        default=None,
        description="Raw Terraform state document (JSON tfstate).",
    )


@router.post(
    "/terraform",
    summary="Compute Terraform config<->state drift over the tfstate_of graph",
    dependencies=[_search_dep],
)
async def compute_terraform_drift(body: TerraformDriftRequest) -> dict[str, Any]:
    """Parse the supplied docs, build the infra graph, and return the drift report.

    Fails closed on auth/scope via the ``require_scope`` dependency.  The drift
    classification itself is delegated to the pure engine so the same logic runs
    here and (eventually) over the persisted graph — see the module docstring.
    """
    tf_parser = TerraformParser()
    state_parser = TfStateParser()

    entities = []
    edges = []

    for doc in body.config_documents:
        parsed = tf_parser.parse(doc.encode("utf-8"))
        ents, eds = extract_infra_graph(parsed)
        entities.extend(ents)
        edges.extend(eds)

    if body.state_document is not None:
        parsed_state = state_parser.parse(body.state_document.encode("utf-8"))
        ents, eds = extract_infra_graph(parsed_state)
        entities.extend(ents)
        edges.extend(eds)

    report = compute_tf_drift(entities, edges)
    log.info(
        "terraform_drift_computed",
        config_docs=len(body.config_documents),
        has_state=body.state_document is not None,
        has_drift=report.has_drift,
        state_not_in_config=len(report.state_not_in_config),
        config_not_in_state=len(report.config_not_in_state),
        attribute_divergences=len(report.attribute_divergences),
    )
    return drift_report_to_dict(report)
