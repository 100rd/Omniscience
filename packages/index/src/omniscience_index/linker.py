"""Cross-source entity linker.

:class:`EntityLinker` finds entities from different sources that refer to
the same real-world concept and creates ``"cross_ref"`` edges in the
``edges`` table to make the connections explicit.

Matching strategy (applied in priority order):

1. **Exact name match** — entities whose normalised names are identical
   across *different* sources are linked unconditionally (score == 1.0).

2. **Resource name match** — Terraform resources ↔ Kubernetes resources
   are compared with :func:`~omniscience_index.matchers.resource_name_match`.
   Pairs scoring above :data:`RESOURCE_MATCH_THRESHOLD` are linked.

3. **Service name match** — a Kubernetes ``Service`` entity ↔ a Grafana
   dashboard entity are fuzzy-matched on their normalised names.
   Pairs scoring above :data:`SERVICE_MATCH_THRESHOLD` are linked.

Idempotency is achieved by a ``(source_entity_id, target_entity_id,
edge_type)`` uniqueness check before inserting: if a ``"cross_ref"`` edge
already exists for the pair it is not duplicated.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from omniscience_core.db.models import Edge, Entity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_index.matchers import (
    exact_name_match,
    resource_name_match,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Minimum score from :func:`~omniscience_index.matchers.resource_name_match`
#: to create a cross-ref edge between Terraform and K8s entities.
RESOURCE_MATCH_THRESHOLD: float = 0.5

#: Minimum score for a service name match (K8s Service ↔ Grafana dashboard).
SERVICE_MATCH_THRESHOLD: float = 0.5

#: Edge type used for all cross-source links created by this module.
CROSS_REF_EDGE_TYPE: str = "cross_ref"


# ---------------------------------------------------------------------------
# EntityLinker
# ---------------------------------------------------------------------------


class EntityLinker:
    """Links entities across sources by matching names and types.

    Args:
        session_factory: An :class:`~sqlalchemy.ext.asyncio.async_sessionmaker`
            that yields :class:`~sqlalchemy.ext.asyncio.AsyncSession` objects.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def link_entities(self, source_id: uuid.UUID) -> int:
        """Find and create cross-source edges for entities in *source_id*.

        Loads all entities belonging to *source_id*, then queries every
        other source for candidate matches using the three matching
        strategies.  New ``"cross_ref"`` edges are inserted; existing
        ones are skipped (idempotent).

        Args:
            source_id: UUID of the source whose entities are being linked.

        Returns:
            Number of new edges created during this call.
        """
        async with self._session_factory() as session, session.begin():
            # Load entities from the target source
            source_entities = await self._fetch_entities_for_source(session, source_id)
            if not source_entities:
                return 0

            # Load entities from all *other* sources
            other_entities = await self._fetch_entities_excluding_source(session, source_id)
            if not other_entities:
                return 0

            # Load existing cross-ref edges to avoid duplicates
            existing_pairs = await self._fetch_existing_cross_ref_pairs(session)

            new_edges = self._compute_links(source_entities, other_entities, existing_pairs)

            for edge in new_edges:
                session.add(edge)

            await session.flush()

        count = len(new_edges)
        log.debug(
            "entity_linker.link_entities",
            source_id=str(source_id),
            new_edges=count,
        )
        return count

    async def resolve_cross_references(self) -> int:
        """Global pass: resolve all unlinked cross-source references.

        Loads *all* entities from *all* sources and runs the full
        matching pipeline over every pair of distinct sources.  Designed
        for post-bulk-ingestion reconciliation or periodic background jobs.

        Returns:
            Total number of new edges created.
        """
        async with self._session_factory() as session, session.begin():
            all_entities = await self._fetch_all_entities(session)
            if not all_entities:
                return 0

            existing_pairs = await self._fetch_existing_cross_ref_pairs(session)

            # Group by source for efficient cross-source iteration
            by_source: dict[uuid.UUID, list[Entity]] = {}
            for ent in all_entities:
                by_source.setdefault(ent.source_id, []).append(ent)

            source_ids = list(by_source.keys())
            new_edges: list[Edge] = []

            for i, sid_a in enumerate(source_ids):
                for sid_b in source_ids[i + 1 :]:
                    links = self._compute_links(
                        by_source[sid_a],
                        by_source[sid_b],
                        existing_pairs,
                    )
                    # Update existing_pairs so later iterations don't re-add
                    for edge in links:
                        existing_pairs.add((edge.source_entity_id, edge.target_entity_id))
                        existing_pairs.add((edge.target_entity_id, edge.source_entity_id))
                    new_edges.extend(links)

            for edge in new_edges:
                session.add(edge)

            await session.flush()

        count = len(new_edges)
        log.debug("entity_linker.resolve_cross_references", new_edges=count)
        return count

    # ------------------------------------------------------------------
    # Matching logic
    # ------------------------------------------------------------------

    def _compute_links(
        self,
        source_entities: list[Entity],
        candidate_entities: list[Entity],
        existing_pairs: set[tuple[uuid.UUID, uuid.UUID]],
    ) -> list[Edge]:
        """Run all matching strategies and return new edges to create."""
        new_edges: list[Edge] = []
        now = datetime.now(UTC)

        for ent_a in source_entities:
            for ent_b in candidate_entities:
                # Never link entities from the same source
                if ent_a.source_id == ent_b.source_id:
                    continue

                pair = (ent_a.id, ent_b.id)
                reverse = (ent_b.id, ent_a.id)
                if pair in existing_pairs or reverse in existing_pairs:
                    continue

                score, strategy = self._match_score(ent_a, ent_b)
                if score <= 0.0:
                    continue

                edge = Edge(
                    id=uuid.uuid4(),
                    source_entity_id=ent_a.id,
                    target_entity_id=ent_b.id,
                    edge_type=CROSS_REF_EDGE_TYPE,
                    edge_metadata={
                        "score": score,
                        "strategy": strategy,
                        "linked_source_id": str(ent_b.source_id),
                    },
                    created_at=now,
                )
                new_edges.append(edge)
                # Track immediately so the inner loop doesn't add a duplicate
                existing_pairs.add(pair)
                existing_pairs.add(reverse)

        return new_edges

    def _match_score(self, ent_a: Entity, ent_b: Entity) -> tuple[float, str]:
        """Return ``(score, strategy_name)`` for the best match between two entities.

        Returns ``(0.0, "")`` when no strategy produces a positive score.
        """
        # Strategy 1: exact name match (highest priority)
        if exact_name_match(ent_a.name, ent_b.name) == 1.0:
            return 1.0, "exact_name"

        if exact_name_match(ent_a.display_name, ent_b.display_name) == 1.0:
            return 1.0, "exact_display_name"

        # Strategy 2: Terraform resource ↔ K8s resource
        tf_types = {"terraform_resource", "terraform_module", "resource"}
        k8s_types = {"k8s_resource", "service", "deployment"}

        is_tf_a = ent_a.entity_type in tf_types
        is_k8s_a = ent_a.entity_type in k8s_types
        is_tf_b = ent_b.entity_type in tf_types
        is_k8s_b = ent_b.entity_type in k8s_types

        if (is_tf_a and is_k8s_b) or (is_k8s_a and is_tf_b):
            score = resource_name_match(ent_a.name, ent_b.name)
            if score >= RESOURCE_MATCH_THRESHOLD:
                return score, "resource_name"

        # Strategy 3: K8s Service ↔ Grafana dashboard
        grafana_types = {"dashboard", "grafana_dashboard"}
        is_k8s_service_a = is_k8s_a and _is_service_entity(ent_a)
        is_grafana_a = ent_a.entity_type in grafana_types
        is_k8s_service_b = is_k8s_b and _is_service_entity(ent_b)
        is_grafana_b = ent_b.entity_type in grafana_types

        if (is_k8s_service_a and is_grafana_b) or (is_grafana_a and is_k8s_service_b):
            # Use resource_name_match: same Dice-coefficient on normalised tokens
            score = resource_name_match(ent_a.name, ent_b.name)
            if score >= SERVICE_MATCH_THRESHOLD:
                return score, "service_name"

        return 0.0, ""

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def _fetch_entities_for_source(
        self, session: AsyncSession, source_id: uuid.UUID
    ) -> list[Entity]:
        stmt = select(Entity).where(Entity.source_id == source_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _fetch_entities_excluding_source(
        self, session: AsyncSession, source_id: uuid.UUID
    ) -> list[Entity]:
        stmt = select(Entity).where(Entity.source_id != source_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _fetch_all_entities(self, session: AsyncSession) -> list[Entity]:
        result = await session.execute(select(Entity))
        return list(result.scalars().all())

    async def _fetch_existing_cross_ref_pairs(
        self, session: AsyncSession
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        """Return a set of (source_id, target_id) for existing cross_ref edges."""
        stmt = select(Edge.source_entity_id, Edge.target_entity_id).where(
            Edge.edge_type == CROSS_REF_EDGE_TYPE
        )
        result = await session.execute(stmt)
        pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for row in result:
            pairs.add((row[0], row[1]))
        return pairs


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _is_service_entity(entity: Entity) -> bool:
    """Return True if the entity looks like a Kubernetes Service."""
    meta: dict[str, Any] = entity.entity_metadata
    k8s_kind = str(meta.get("k8s_kind", "")).lower()
    return k8s_kind == "service" or entity.entity_type.lower() in {"service"}


__all__ = [
    "CROSS_REF_EDGE_TYPE",
    "RESOURCE_MATCH_THRESHOLD",
    "SERVICE_MATCH_THRESHOLD",
    "EntityLinker",
]
