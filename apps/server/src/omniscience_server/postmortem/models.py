"""Shared Pydantic models for the post-mortem package.

Lives in its own module so that :mod:`followups` and :mod:`generator`
can both import :class:`PostmortemTimelineRow` without creating a
circular dependency (FollowUp depends on timeline rows for the
extractor's heuristics; the generator embeds FollowUp objects in the
report).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Citation(BaseModel):
    """Provenance pointer attached to every timeline row.

    Lets the reader follow the row back to the exact bitemporal slice
    of the graph it came from.  Stable across renderings so HTML/JSON
    consumers can deep-link by entity + as-of.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(description="Canonical entity name.")
    entity_type: str = Field(description="Entity kind (e.g. 'k8s_pod').")
    as_of: datetime = Field(description="Bitemporal anchor (UTC).")
    source: str = Field(description="Connector / source provenance id.")

    def to_markdown(self) -> str:
        """Render as a compact inline citation 'kind/name @ ISO-8601'."""
        return f"`{self.entity_type}/{self.entity_id}` @ {self.as_of.isoformat()}"


class PostmortemTimelineRow(BaseModel):
    """A single timeline entry with a citation pointer."""

    model_config = ConfigDict(frozen=True)

    ts: datetime = Field(description="Event timestamp (UTC).")
    entity_id: str = Field(description="Canonical entity name.")
    entity_type: str = Field(description="Entity kind.")
    change_kind: str = Field(description="'created' or 'ended'.")
    before_state_summary: str | None = None
    after_state_summary: str | None = None
    citation: Citation = Field(description="Cite-by-entity provenance.")


__all__ = ["Citation", "PostmortemTimelineRow"]
