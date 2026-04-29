"""resolve_incident composition (issue #153, Wave 2 of epic #99).

This module is the design centre of the demo path: a single MCP/REST
tool — :func:`resolve_incident` — that composes the four Wave-1
incident sources (alerts, GitHub PR, Slack, OTel) into a recommendation
bundle.  The connectors emit canonical-named entities and ``cross_ref``
edges; the composition here is workspace-scoped, ACL-strict, bitemporal-
aware, and does not invent any new retrieval primitives — it walks
existing ``GraphStore.find_related`` output and classifies neighbours
by canonical-name prefix.

Composition shape
-----------------

1. **Parse** ``alert_id``: must be ``alert://{provider}/{provider_alert_id}``.
2. **Resolve** the seed alert via :meth:`GraphStore.get_entity` — returns
   ``404 alert_not_found`` when absent (cross-workspace alerts are
   indistinguishable from "not found" by design, see #117).
3. **Traverse** outward via :meth:`GraphStore.find_related` (max_depth
   default 2).  Classify each related entity by canonical-name prefix:
   - ``alert://`` -> the seed itself
   - ``arn:`` / ``pod/`` / ``namespace/name`` / ``service://`` -> target resource
   - ``trace://`` -> trace evidence (OTel #152)
   - ``https://github.com/.../pull/N`` -> responsible PR (GitHub PR #150)
   - ``slack://channel/{ch}/thread/{ts}`` -> Slack discussion (Slack #149)
4. **Score** confidence with the v0.1 heuristic (see
   :data:`CONFIDENCE_*` module-level constants).  This v0.1 placeholder
   ships per the architect memo on epic #99; #155 replaces it with a
   calibrated model.
5. **Bundle** the recommendation into :class:`ResolveIncidentResponse`.

ACL invariant (non-negotiable)
------------------------------

- ``workspace_id`` is taken from the caller's bearer token, never from
  input.  Every store call carries it (see :meth:`GraphStore` ACL
  contract from #117 / ADR-0005).
- A foreign workspace's ``alert_id`` returns ``alert_not_found`` — the
  same response a non-existent alert would produce.  Existence is
  never leaked.

``as_of`` deferred from #173
----------------------------

The forcing-function test ``test_resolve_incident_not_present_yet``
(in ``tests/test_mcp_as_of.py``) is replaced with a positive contract
test that asserts ``resolve_incident`` accepts ``as_of`` and forwards it
to the bitemporal-aware ``GraphStore`` reads (#170 / #173 / #174).

Workspace-mismatch HTTP status
------------------------------

Cross-workspace ``alert_id`` resolution returns ``404 alert_not_found``
rather than ``403 forbidden`` to avoid leaking alert existence to a
caller that cannot read the alert.  This mirrors the ``GraphStore``
contract that "cross-workspace entities are indistinguishable from
'not found' by design" (#117 / ADR-0005).  The trade-off is documented
here so a future audit does not mis-classify it as an authz bug.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from fastapi import FastAPI
from omniscience_core.storage import EntityNodeView, GraphResultView, GraphStore
from pydantic import BaseModel, Field, field_validator

from omniscience_server.as_of import (
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)

# ---------------------------------------------------------------------------
# Module constants — no magic numbers in the algorithm body
# ---------------------------------------------------------------------------

#: Default BFS depth when the caller does not override ``max_depth``.
#: Issue #153 architect memo: depth 2 covers alert -> resource ->
#: deploying-PR / matching Slack thread / observed trace; depth 3+ adds
#: noise without recall.
DEFAULT_MAX_DEPTH: Final[int] = 2

#: Lower bound for ``max_depth``.  Depth < 1 makes the traversal a
#: degenerate single-entity lookup which is :func:`mcp_get_entity`'s job.
MIN_MAX_DEPTH: Final[int] = 1

#: Upper bound for ``max_depth``.  Pinned to the same envelope as the
#: GraphRAG composer's :data:`MAX_ANCHOR_DEPTH` so a future hub-entity
#: blast does not surface here either (see #107).
MAX_MAX_DEPTH: Final[int] = 5

#: Soft cap on the number of Slack threads carried in the response.
#: Bounds payload size; deeper traversals are still allowed but the
#: response slices.
MAX_SLACK_THREADS_RETURNED: Final[int] = 10

#: Window inside which a PR merge is considered "temporally correlated"
#: with an alert firing — issue #153 §C.  24h is the architect-memo
#: cliff; tighter windows are punted to #155's calibrated model.
PR_RECENCY_WINDOW_SECONDS: Final[int] = 24 * 60 * 60

# ---------------------------------------------------------------------------
# Confidence-score heuristic (v0.1 placeholder; #155 replaces it)
# ---------------------------------------------------------------------------

#: PR found AND merged within :data:`PR_RECENCY_WINDOW_SECONDS` before
#: the alert fired.  Issue #153 §C.
CONFIDENCE_PR_TEMPORAL_MATCH: Final[float] = 0.9

#: PR found but no temporal correlation (older than the recency window
#: or no merge timestamp on either side).  Issue #153 §C.
CONFIDENCE_PR_NO_TEMPORAL_MATCH: Final[float] = 0.6

#: Target resource resolved but no PR.  Issue #153 §C.
CONFIDENCE_RESOURCE_ONLY: Final[float] = 0.4

#: Only the alert entity itself was resolvable (no resource neighbour
#: found in the traversal).  Issue #153 §C.
CONFIDENCE_ALERT_ONLY: Final[float] = 0.1

#: Alert resolution failed entirely.  Documented for completeness even
#: though the surrounding handler 404s before reaching the heuristic.
CONFIDENCE_NONE: Final[float] = 0.0

# ---------------------------------------------------------------------------
# Canonical-name prefix discriminators
# ---------------------------------------------------------------------------

_ALERT_PREFIX: Final[str] = "alert://"
_TRACE_PREFIX: Final[str] = "trace://"
_SLACK_THREAD_PREFIX: Final[str] = "slack://channel/"
_SERVICE_PREFIX: Final[str] = "service://"
_ARN_PREFIX: Final[str] = "arn:"
_GITHUB_PR_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://github\.com/[^/]+/[^/]+/pull/(\d+)(?:[/?#].*)?$"
)
_POD_RE: Final[re.Pattern[str]] = re.compile(r"^pod/[A-Za-z0-9._-]+$")
# Generic ``<namespace>/<resource>`` k8s reference, e.g. ``default/api``.
_K8S_NAMESPACED_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# ---------------------------------------------------------------------------
# Error codes (mirrored on the MCP / REST surfaces)
# ---------------------------------------------------------------------------

INVALID_ALERT_ID_CODE: Final[str] = "invalid_alert_id"
INVALID_ALERT_ID_MESSAGE: Final[str] = (
    "alert_id must be of the form 'alert://{provider}/{provider_alert_id}'"
)
ALERT_NOT_FOUND_CODE: Final[str] = "alert_not_found"


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------


class AlertSummary(BaseModel):
    """Minimal alert payload returned in the recommendation bundle."""

    name: str = Field(description="Canonical alert name (alert://provider/id).")
    kind: str | None = None
    source: str | None = None
    chunk_text: str | None = None
    valid_from: datetime | None = None


class ResourceSummary(BaseModel):
    """Minimal resource payload (pod / ARN / service / k8s name)."""

    name: str = Field(description="Canonical resource identifier.")
    kind: str | None = None
    source: str | None = None
    edge_type: str | None = Field(
        default=None,
        description="The edge_type by which the alert reached this resource.",
    )


class PrSummary(BaseModel):
    """Minimal PR payload returned as the responsible PR (when any)."""

    name: str = Field(description="Canonical PR URL.")
    kind: str | None = None
    source: str | None = None
    edge_type: str | None = None
    merged_at: datetime | None = Field(
        default=None,
        description=(
            "Best-effort merge timestamp; v0.1 reads it from the entity's "
            "bitemporal ``valid_from`` when populated by the connector "
            "(GitHub PR #150).  Calibrated extraction lands in #155."
        ),
    )


class SlackThreadSummary(BaseModel):
    """Minimal Slack thread payload — name plus optional chunk text."""

    name: str = Field(description="Canonical Slack thread URI.")
    kind: str | None = None
    source: str | None = None
    chunk_text: str | None = None
    edge_type: str | None = None


class ResolveIncidentResponse(BaseModel):
    """The recommendation bundle returned by ``resolve_incident``.

    ``confidence_score`` is a v0.1 placeholder per the architect memo on
    epic #99; the calibrated model lands in #155.  Callers should treat
    the score as a stable schema slot, not a calibrated probability.
    """

    alert: AlertSummary
    target_resource: ResourceSummary | None = None
    responsible_pr: PrSummary | None = None
    slack_threads: list[SlackThreadSummary] = Field(default_factory=list)
    confidence_score: float = Field(ge=0.0, le=1.0)
    effective_as_of: datetime
    meta: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Internal classification dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Classified:
    """Bucketed view of a single traversal neighbour."""

    target_resource: EntityNodeView | None
    responsible_pr: EntityNodeView | None
    slack_threads: tuple[EntityNodeView, ...]


# ---------------------------------------------------------------------------
# Public composition entry point
# ---------------------------------------------------------------------------


async def mcp_resolve_incident(
    app: FastAPI,
    alert_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Resolve an alert into a recommendation bundle.

    Mirrors the shape of :func:`mcp_get_related_entities` (issue #153
    requirement) — same ``workspace_id`` ACL invariant, same ``as_of``
    plumbing, same telemetry histogram (``omniscience_request_duration_seconds``
    with ``tool="resolve_incident"``).

    Parameters
    ----------
    app:
        FastAPI app exposing ``app.state.graph_store`` (Neo4j adapter
        in production, in-memory fake in tests).
    alert_id:
        Canonical alert URI ``alert://{provider}/{provider_alert_id}``.
        Other forms raise ``ValueError("invalid_alert_id:...")``.
    workspace_id:
        REQUIRED. Forwarded to every ``GraphStore`` call.
    as_of:
        Optional ADR-0008 §5 bitemporal anchor.  Reuses
        :func:`enforce_utc` and the surrounding handler's parser.
    max_depth:
        BFS depth from the alert seed.  Clamped to
        ``[MIN_MAX_DEPTH, MAX_MAX_DEPTH]``; default ``DEFAULT_MAX_DEPTH``.
    """
    _validate_alert_id(alert_id)
    normalised_as_of = enforce_utc(as_of)
    clamped_depth = max(MIN_MAX_DEPTH, min(max_depth, MAX_MAX_DEPTH))

    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")

    with record_request_duration(
        surface="mcp", tool="resolve_incident", as_of=normalised_as_of
    ) as patcher:
        seed = await graph_store.get_entity(
            entity_name=alert_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if seed is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
            raise ValueError(f"{ALERT_NOT_FOUND_CODE}:{alert_id}")

        # Traversal: alert -> related entities (resources, PRs, threads, traces).
        try:
            graph_result = await graph_store.find_related(
                entity_name=alert_id,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                max_depth=clamped_depth,
            )
        except ValueError as exc:
            # Seed disappeared between the get_entity and find_related
            # calls — race against re-ingestion.  Return the alert-only
            # bundle rather than 404, mirroring the empty-graph contract.
            if str(exc).startswith("entity_not_found:"):
                graph_result = GraphResultView(seed=seed, related=[], edges=[])
            else:
                raise

        classified = _classify_neighbours(graph_result)
        confidence = _compute_confidence(
            alert=seed,
            classified=classified,
        )
        return _build_response(
            alert=seed,
            classified=classified,
            confidence=confidence,
            as_of=normalised_as_of,
        ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_alert_id(alert_id: str) -> None:
    """Reject alert ids that are not of the form ``alert://provider/id``."""
    if not isinstance(alert_id, str):  # pragma: no cover - typing barrier
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")
    if not alert_id.startswith(_ALERT_PREFIX):
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")
    suffix = alert_id[len(_ALERT_PREFIX) :]
    if "/" not in suffix:
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")
    provider, _, provider_alert_id = suffix.partition("/")
    if not provider.strip() or not provider_alert_id.strip():
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")


# ---------------------------------------------------------------------------
# Classification — pure, easy to unit test
# ---------------------------------------------------------------------------


def _classify_neighbours(result: GraphResultView) -> _Classified:
    """Bucket BFS neighbours by canonical-name prefix.

    Resolves a single ``target_resource`` (closest-depth wins; ties
    broken by traversal order), a single ``responsible_pr`` (most
    recently merged wins, see :func:`_pick_responsible_pr`), and a
    list of Slack threads (capped at ``MAX_SLACK_THREADS_RETURNED``).
    """
    target_candidates: list[EntityNodeView] = []
    pr_candidates: list[EntityNodeView] = []
    thread_candidates: list[EntityNodeView] = []
    for node in result.related:
        if _is_resource(node.name):
            target_candidates.append(node)
        elif _is_pr(node.name):
            pr_candidates.append(node)
        elif _is_slack_thread(node.name):
            thread_candidates.append(node)
        # trace://* and other neighbours are not surfaced in v0.1; #154
        # demo fixture decides whether to promote them.
    target_resource = _pick_target_resource(target_candidates)
    responsible_pr = _pick_responsible_pr(pr_candidates)
    threads = tuple(thread_candidates[:MAX_SLACK_THREADS_RETURNED])
    return _Classified(
        target_resource=target_resource,
        responsible_pr=responsible_pr,
        slack_threads=threads,
    )


def _is_pr(name: str) -> bool:
    """Return ``True`` iff ``name`` is a GitHub PR canonical URL."""
    return bool(_GITHUB_PR_RE.match(name))


def _is_slack_thread(name: str) -> bool:
    return name.startswith(_SLACK_THREAD_PREFIX)


def _is_resource(name: str) -> bool:
    """Return ``True`` for pod / ARN / service / k8s namespaced names.

    Excludes alert / trace / Slack / PR canonical forms so the bucketing
    is mutually exclusive at the prefix layer.
    """
    if name.startswith(_ALERT_PREFIX):
        return False
    if name.startswith(_TRACE_PREFIX):
        return False
    if name.startswith(_SLACK_THREAD_PREFIX):
        return False
    if _GITHUB_PR_RE.match(name):
        return False
    if name.startswith(_ARN_PREFIX):
        return True
    if name.startswith(_SERVICE_PREFIX):
        return True
    if _POD_RE.match(name):
        return True
    return bool(_K8S_NAMESPACED_RE.match(name))


def _pick_target_resource(
    candidates: list[EntityNodeView],
) -> EntityNodeView | None:
    """Closest-depth wins; ties broken by traversal order (input order)."""
    if not candidates:
        return None
    return min(candidates, key=lambda n: n.depth)


def _pick_responsible_pr(
    candidates: list[EntityNodeView],
) -> EntityNodeView | None:
    """Most recently merged wins; falls back to closest-depth.

    The connector (GitHub PR #150) carries the merge timestamp in the
    entity's bitemporal ``valid_from`` — when present.  Entities without
    a populated ``valid_from`` fall back to BFS depth so a single PR
    candidate is still surfaceable.
    """
    if not candidates:
        return None
    with_merge = [c for c in candidates if c.valid_from is not None]
    if with_merge:
        return max(with_merge, key=lambda n: n.valid_from or datetime.min)
    return min(candidates, key=lambda n: n.depth)


# ---------------------------------------------------------------------------
# Confidence-score heuristic
# ---------------------------------------------------------------------------


def _compute_confidence(
    *,
    alert: EntityNodeView,
    classified: _Classified,
) -> float:
    """v0.1 deterministic placeholder per issue #153 §C.

    The 4-rung ladder:

    - 0.9 if ``responsible_pr`` is present AND merged within
      :data:`PR_RECENCY_WINDOW_SECONDS` BEFORE the alert fired.
    - 0.6 if ``responsible_pr`` is present but no temporal correlation.
    - 0.4 if ``target_resource`` resolved but no PR.
    - 0.1 if only the alert entity itself was resolvable.

    The 0.0 rung is documented in :data:`CONFIDENCE_NONE` for parity
    with the issue body but never returned here — alert-resolution
    failure 404s upstream of this call.
    """
    if classified.responsible_pr is not None:
        if _is_temporally_correlated(alert=alert, pr=classified.responsible_pr):
            return CONFIDENCE_PR_TEMPORAL_MATCH
        return CONFIDENCE_PR_NO_TEMPORAL_MATCH
    if classified.target_resource is not None:
        return CONFIDENCE_RESOURCE_ONLY
    return CONFIDENCE_ALERT_ONLY


def _is_temporally_correlated(
    *,
    alert: EntityNodeView,
    pr: EntityNodeView,
) -> bool:
    """Return ``True`` iff PR merge time precedes alert by < window.

    Both timestamps are read from the entity's bitemporal ``valid_from``;
    the connectors populate them deterministically (#150 / #151).  When
    either side is missing the heuristic defaults to ``False`` — the
    "no temporal correlation" rung.
    """
    pr_merged_at = pr.valid_from
    alert_fired_at = alert.valid_from
    if pr_merged_at is None or alert_fired_at is None:
        return False
    if pr_merged_at > alert_fired_at:
        # Merge AFTER fire is a regression-on-rollback or noise; not a
        # cause.  Cliff-out per architect memo.
        return False
    delta = (alert_fired_at - pr_merged_at).total_seconds()
    return 0 <= delta <= PR_RECENCY_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def _build_response(
    *,
    alert: EntityNodeView,
    classified: _Classified,
    confidence: float,
    as_of: datetime | None,
) -> ResolveIncidentResponse:
    """Produce the bounded :class:`ResolveIncidentResponse` payload."""
    return ResolveIncidentResponse(
        alert=AlertSummary(
            name=alert.name,
            kind=alert.kind,
            source=alert.source,
            chunk_text=alert.chunk_text,
            valid_from=alert.valid_from,
        ),
        target_resource=_summarise_resource(classified.target_resource),
        responsible_pr=_summarise_pr(classified.responsible_pr),
        slack_threads=[_summarise_thread(t) for t in classified.slack_threads],
        confidence_score=confidence,
        effective_as_of=resolve_effective_as_of(as_of),
        meta=None,
    )


def _summarise_resource(node: EntityNodeView | None) -> ResourceSummary | None:
    if node is None:
        return None
    return ResourceSummary(
        name=node.name,
        kind=node.kind,
        source=node.source,
        edge_type=node.edge_type,
    )


def _summarise_pr(node: EntityNodeView | None) -> PrSummary | None:
    if node is None:
        return None
    return PrSummary(
        name=node.name,
        kind=node.kind,
        source=node.source,
        edge_type=node.edge_type,
        merged_at=node.valid_from,
    )


def _summarise_thread(node: EntityNodeView) -> SlackThreadSummary:
    return SlackThreadSummary(
        name=node.name,
        kind=node.kind,
        source=node.source,
        chunk_text=node.chunk_text,
        edge_type=node.edge_type,
    )


# ---------------------------------------------------------------------------
# Pydantic field validators (kept here so the contract is one import away)
# ---------------------------------------------------------------------------


class AlertIdRequest(BaseModel):
    """Lightweight wrapper used by the REST surface to validate ``alert_id``.

    The MCP surface uses string args directly; this model centralises
    the prefix check for the REST handler so it can map ``ValueError``
    to ``HTTP 400 invalid_alert_id``.
    """

    alert_id: str

    @field_validator("alert_id")
    @classmethod
    def _validate(cls, value: str) -> str:
        _validate_alert_id(value)
        return value


__all__ = [
    "ALERT_NOT_FOUND_CODE",
    "CONFIDENCE_ALERT_ONLY",
    "CONFIDENCE_NONE",
    "CONFIDENCE_PR_NO_TEMPORAL_MATCH",
    "CONFIDENCE_PR_TEMPORAL_MATCH",
    "CONFIDENCE_RESOURCE_ONLY",
    "DEFAULT_MAX_DEPTH",
    "INVALID_ALERT_ID_CODE",
    "INVALID_ALERT_ID_MESSAGE",
    "MAX_MAX_DEPTH",
    "MAX_SLACK_THREADS_RETURNED",
    "MIN_MAX_DEPTH",
    "PR_RECENCY_WINDOW_SECONDS",
    "AlertIdRequest",
    "AlertSummary",
    "PrSummary",
    "ResolveIncidentResponse",
    "ResourceSummary",
    "SlackThreadSummary",
    "mcp_resolve_incident",
]
