"""Cross-source entity linker (Neo4j-native).

As of Epic #96 and ADR-0012, this module links entities directly in
Neo4j using the GraphStore adapter. SQL-side linking is deprecated.

Matching strategy (priority order):
1. Exact name match (score 1.0)
2. ARN match (score 0.8 - 1.0)
3. Resource name match (Terraform <-> K8s)
4. Service name match (K8s <-> Grafana)
"""

from __future__ import annotations

import re
import uuid

import structlog
from omniscience_core.storage.graph import EdgeUpsert, EntityNodeView, GraphStore

from omniscience_index.matchers import (
    arn_match,
    exact_name_match,
    otel_match,
    resource_name_match,
    tags_match,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESOURCE_MATCH_THRESHOLD: float = 0.5
SERVICE_MATCH_THRESHOLD: float = 0.5
CROSS_REF_EDGE_TYPE: str = "cross_ref"

_AWS_LIVE_KIND: str = "aws_live"
_TFSTATE_KIND: str = "tfstate_instance"
_JIRA_KIND: str = "jira_issue"

# Regex for Jira keys like PROJ-123
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-[0-9]+)\b")


class EntityLinker:
    """Links entities across sources within a workspace using Neo4j."""

    def __init__(self, graph_store: GraphStore) -> None:
        self._graph_store = graph_store

    async def link_entities(self, source_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
        """Find and create cross-source edges for entities in *source_id*."""
        all_entities = await self._graph_store.get_all_entities(workspace_id=workspace_id)
        if not all_entities:
            return 0

        source_entities = [e for e in all_entities if str(e.source) == str(source_id)]
        other_entities = [e for e in all_entities if str(e.source) != str(source_id)]

        if not source_entities:
            return 0

        # 1. Intra-workspace linking (Exact match, ARN, etc.)
        created = 0
        if other_entities:
            new_links = self._compute_links(source_entities, other_entities)
            for ent_a, ent_b, score, strategy in new_links:
                await self._graph_store.upsert_edge(
                    edge=EdgeUpsert(
                        source_entity_id=ent_a.id,
                        target_entity_id=ent_b.id,
                        edge_type=CROSS_REF_EDGE_TYPE,
                        metadata={
                            "score": score,
                            "strategy": strategy,
                            "linked_source_id": str(ent_b.source),
                        },
                    ),
                    workspace_id=workspace_id,
                )
                created += 1

        # 2. Stub Resolution pass (Global cross-file linking)
        await self.resolve_stubs(workspace_id)

        return created

    async def resolve_stubs(self, workspace_id: uuid.UUID) -> int:
        """Merge stub nodes with real entities in the same workspace."""
        # Use the storage-native resolution (Cypher batch) for efficiency.
        resolved = await self._graph_store.resolve_pending_stubs(workspace_id=workspace_id)
        if resolved > 0:
            log.info(
                "entity_linker_stubs_resolved",
                workspace_id=str(workspace_id),
                count=resolved,
            )
        return resolved

    def _compute_links(
        self,
        source_entities: list[EntityNodeView],
        candidate_entities: list[EntityNodeView],
    ) -> list[tuple[EntityNodeView, EntityNodeView, float, str]]:
        """Run all matching strategies and return candidate pairs."""
        new_links = []
        for ent_a in source_entities:
            for ent_b in candidate_entities:
                score, strategy = self._match_score(ent_a, ent_b)
                if score > 0.0:
                    new_links.append((ent_a, ent_b, score, strategy))
        return new_links

    def _match_score(self, ent_a: EntityNodeView, ent_b: EntityNodeView) -> tuple[float, str]:
        """Best match score between two entities."""
        # 1. Exact name match
        if exact_name_match(ent_a.name, ent_b.name) == 1.0:
            return 1.0, "exact_name"

        # 2. ARN match (deterministic)
        if arn_match(ent_a, ent_b) == 1.0:
            return 1.0, "arn_match"

        # 3. OTel match (deterministic)
        if otel_match(ent_a, ent_b) == 1.0:
            return 1.0, "otel_match"

        # 4. Tags match (deterministic)
        if tags_match(ent_a, ent_b) == 1.0:
            return 1.0, "tags_match"

        # 2. Jira Ticket ID match (Code/Commit -> Jira Issue)
        is_jira_a = ent_a.kind == _JIRA_KIND
        is_jira_b = ent_b.kind == _JIRA_KIND

        if (is_jira_a or is_jira_b) and (ent_a.kind != ent_b.kind):
            jira_issue = ent_a if is_jira_a else ent_b
            other = ent_b if is_jira_a else ent_a

            # Look for Jira Key in 'other' entity's text or name
            text_to_scan = f"{other.name} {other.chunk_text or ''}"
            matches = _JIRA_KEY_RE.findall(text_to_scan)

            # Jira entity name is typically the ticket key (PROJ-123)
            if jira_issue.display_name in matches:
                return 1.0, "jira_ticket_match"

        # 3. Resource name match (Terraform <-> K8s)
        tf_types = {"terraform_resource", "terraform_module", "resource", "tfstate_instance"}
        k8s_types = {"k8s_resource", "service", "deployment", "pod"}

        is_tf_a = ent_a.kind in tf_types
        is_k8s_a = ent_a.kind in k8s_types
        is_tf_b = ent_b.kind in tf_types
        is_k8s_b = ent_b.kind in k8s_types

        if (is_tf_a and is_k8s_b) or (is_k8s_a and is_tf_b):
            score = resource_name_match(ent_a.name, ent_b.name)
            if score >= RESOURCE_MATCH_THRESHOLD:
                return score, "resource_name"

        return 0.0, ""


__all__ = ["CROSS_REF_EDGE_TYPE", "EntityLinker"]
