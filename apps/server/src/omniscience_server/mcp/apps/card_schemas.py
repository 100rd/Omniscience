"""Pydantic schemas for MCP Apps cards (issue #238).

The schemas in this module are the **normative wire contract** for the
``omniscience/apps`` experimental capability.  They are deliberately
small, additive, and dependency-free — a downstream renderer (Claude
Code, Cursor, Cline) should be able to ingest the JSON shape without
any prior knowledge of Omniscience internals.

Schema family
-------------

``ResolveIncidentCard``  — the top-level card for the ``resolve_incident``
tool.  Composes:

* :class:`IncidentSummary` — one-line headline + a free-form summary
  paragraph the client renders verbatim.
* up to three :class:`RankedCandidate` rows — the alert's resource /
  PR / Slack-thread chain ranked by score; each carries:
    * a :class:`ConfidenceBar` (calibrated 0.0-1.0 with a discrete
      visual bucket so clients can pick a colour without bespoke logic);
    * a list of :class:`CitationLink` items for click-through; and
    * a free-form ``subtitle`` for the renderer to surface as muted
      caption text.
* optional :class:`SimilarPastIncident` rows surfaced when the inner
  response carries ``similar_past`` (issue #233; the field is
  optional and absence does NOT break the card).

Stability
---------

* Fields may be **added** in a backwards-compatible way without
  bumping :data:`APPS_SCHEMA_VERSION`; ``model_config = ConfigDict(extra="ignore")``
  is **not** set so unknown fields are rejected on the way in (tests
  pin the exact set of keys).
* Existing fields will NOT be renamed or have their types narrowed
  without a major version bump; clients that pin ``apps_version``
  will keep rendering correctly.
* All datetimes are ISO-8601 UTC; the conversion happens in
  :mod:`omniscience_server.mcp.apps.card_builders` so the schemas
  themselves use ``str`` and remain framework-friendly.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The four discrete confidence buckets used by ``ConfidenceBar.bucket``.
#: Pinned as a Literal so typecheckers catch bucket-name typos at the
#: builder call sites.
ConfidenceBucket = Literal["very_low", "low", "medium", "high"]

#: Thresholds (inclusive lower bound) for the bucket boundaries.  These
#: are intentionally coarse — the bucket is purely a rendering hint;
#: the precise score is always carried in ``ConfidenceBar.value``.
BUCKET_HIGH_THRESHOLD: Final[float] = 0.8
BUCKET_MEDIUM_THRESHOLD: Final[float] = 0.5
BUCKET_LOW_THRESHOLD: Final[float] = 0.2


class CitationLink(BaseModel):
    """A click-through link surfaced inside a card row.

    Maps 1:1 to the canonical-entity-URI convention used by
    Omniscience connectors (e.g. ``https://github.com/o/r/pull/42``,
    ``slack://channel/C123/thread/1700000000.123``,
    ``alert://pagerduty/INC-9001``).  The client renders the title
    and dereferences the URL via its own link handler.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Human-readable label for the link.")
    uri: str = Field(description="Canonical entity URI to navigate to.")
    kind: str | None = Field(
        default=None,
        description=(
            "Optional entity kind hint (``pull_request``, ``slack_thread``, "
            "``alert``, ``service``, ...) so the renderer can pick an icon."
        ),
    )


class ConfidenceBar(BaseModel):
    """A discrete-bucket + continuous-score representation.

    Renderers can either:

    * Read ``value`` and draw a percentage bar (preferred), or
    * Read ``bucket`` and pick a colour from a four-stop palette
      (fallback for renderers without numeric bar support).
    """

    model_config = ConfigDict(extra="forbid")

    value: float = Field(ge=0.0, le=1.0, description="Calibrated score in [0, 1].")
    bucket: ConfidenceBucket = Field(
        description="Discrete visual bucket derived from ``value`` (very_low/low/medium/high)."
    )
    label: str = Field(
        description=("Short human label for screen readers, e.g. ``'high confidence (0.92)'``.")
    )


class RankedCandidate(BaseModel):
    """A single ranked row inside the card (alert, PR, Slack thread, ...)."""

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, description="1-indexed rank within the card.")
    title: str = Field(description="Primary headline for the row.")
    subtitle: str | None = Field(
        default=None,
        description="Muted caption text rendered beneath the title.",
    )
    kind: Literal["alert", "resource", "pull_request", "slack_thread", "other"] = Field(
        description="Categorical kind so the renderer can pick an icon and section."
    )
    confidence: ConfidenceBar
    citations: list[CitationLink] = Field(
        default_factory=list,
        description="Click-through links (entity URIs).  May be empty.",
    )


class SimilarPastIncident(BaseModel):
    """A reference to a past incident clustered with the current one (issue #233).

    This block is rendered only when the inner ``resolve_incident``
    response carries a ``similar_past`` field.  Absence does NOT
    invalidate the card.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(description="Canonical alert URI of the past incident.")
    title: str = Field(description="Short human-readable summary.")
    occurred_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp of when the past incident fired.",
    )
    similarity_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional similarity score from the clusterer in [0, 1].",
    )
    resolution_hint: str | None = Field(
        default=None,
        description="Optional one-line note on how the past incident was resolved.",
    )


class IncidentSummary(BaseModel):
    """The header block of the card."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="One-line incident headline.")
    summary: str = Field(description="Paragraph-length human summary.")
    alert_uri: str = Field(description="Canonical alert URI (``alert://...``).")
    fired_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp of when the alert fired.",
    )
    below_trust_threshold: bool = Field(
        default=False,
        description=(
            "Mirrors ``meta.below_trust_threshold`` from the inner response. "
            "When ``True`` the renderer is expected to surface a warning chip."
        ),
    )


class ResolveIncidentCard(BaseModel):
    """Top-level MCP Apps card for ``resolve_incident``.

    Shape:

    .. code-block:: json

        {
            "apps_version": "1.0",
            "card_type": "resolve_incident",
            "summary": { ... IncidentSummary ... },
            "candidates": [ ... up to 3 RankedCandidate ... ],
            "similar_past": [ ... optional SimilarPastIncident ... ]
        }

    The ``apps_version`` and ``card_type`` keys are required and pinned
    as ``Literal`` so any drift is a typecheck error.
    """

    model_config = ConfigDict(extra="forbid")

    apps_version: Literal["1.0"] = Field(
        default="1.0",
        description="Schema version; see APPS_SCHEMA_VERSION.",
    )
    card_type: Literal["resolve_incident"] = Field(
        default="resolve_incident",
        description="Stable discriminator so a client can dispatch to a renderer.",
    )
    summary: IncidentSummary
    candidates: list[RankedCandidate] = Field(
        default_factory=list,
        max_length=3,
        description="Top-N (N<=3) ranked candidates, descending by confidence.",
    )
    similar_past: list[SimilarPastIncident] = Field(
        default_factory=list,
        description=(
            "Optional list of past incidents in the same cluster (#233). "
            "Empty list means either the inner response did not carry the "
            "field OR the cluster contains no past incidents."
        ),
    )
    degraded_subsystems: list[str] = Field(
        default_factory=list,
        description="List of subsystems that are operating in a degraded mode."
    )
    snapshot_id: str | None = Field(
        default=None,
        description="ID of the graph snapshot used for this response."
    )


def bucket_for_score(value: float) -> ConfidenceBucket:
    """Pick the discrete :data:`ConfidenceBucket` for a continuous score.

    Boundaries:

    * ``>= 0.8`` -> ``"high"``
    * ``>= 0.5`` -> ``"medium"``
    * ``>= 0.2`` -> ``"low"``
    * otherwise -> ``"very_low"``

    Defined here (not in the builder) so the contract test can import
    it directly and assert that boundary scores map to the documented
    bucket.
    """
    if value >= BUCKET_HIGH_THRESHOLD:
        return "high"
    if value >= BUCKET_MEDIUM_THRESHOLD:
        return "medium"
    if value >= BUCKET_LOW_THRESHOLD:
        return "low"
    return "very_low"
