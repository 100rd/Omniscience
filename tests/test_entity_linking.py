"""Tests for Issue #29 — Cross-source entity linking (ADR-0012 graph-store API).

Coverage:
- normalize_entity_name: prefix stripping, separator normalisation, edge cases
- exact_name_match: match/no-match, case insensitivity, separator variance
- resource_name_match: tf↔k8s fuzzy matching, threshold behaviour, empty inputs
- EntityLinker.link_entities: creates cross-ref edges, skips same-source pairs
- EntityLinker.link_entities: idempotency — upsert_edge is called with same
  (source_entity_id, target_entity_id) on re-run; graph dedupes via upsert
- EntityLinker.link_entities: returns count of new edges
- EntityLinker.resolve_stubs: delegates to graph_store.resolve_pending_stubs
- Pipeline _stage_link wiring: called after graph stage, swallows errors

NOTE: The SQL-era _make_session_factory / Entity ORM mock has been removed.
EntityLinker now takes graph_store: GraphStore (ADR-0012).  All entity fixtures
use EntityNodeView.  The old _compute_links(existing_set) signature no longer
exists; idempotency is expressed via upsert_edge semantics (the graph dedupes).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.storage.graph import EntityNodeView
from omniscience_index.linker import CROSS_REF_EDGE_TYPE, EntityLinker
from omniscience_index.matchers import (
    exact_name_match,
    normalize_entity_name,
    resource_name_match,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _make_node(
    *,
    source: uuid.UUID | None = None,
    kind: str = "function",
    name: str = "mymod.my_func",
    chunk_text: str | None = None,
) -> EntityNodeView:
    """Construct an EntityNodeView for linker tests."""
    return EntityNodeView(
        id=uuid.uuid4(),
        name=name,
        kind=kind,
        source=str(source or uuid.uuid4()),
        chunk_text=chunk_text,
    )


def _make_graph_store(
    entities: list[EntityNodeView] | None = None,
    resolve_count: int = 0,
) -> AsyncMock:
    """Build a minimal GraphStore mock for EntityLinker tests.

    ``entities``      — list returned by get_all_entities(workspace_id=...)
    ``resolve_count`` — value returned by resolve_pending_stubs(workspace_id=...)
    """
    store = AsyncMock()
    store.get_all_entities = AsyncMock(return_value=entities or [])
    store.upsert_edge = AsyncMock(return_value=None)
    store.resolve_pending_stubs = AsyncMock(return_value=resolve_count)
    return store


# ===========================================================================
# normalize_entity_name
# ===========================================================================


class TestNormalizeEntityName:
    def test_lowercases(self) -> None:
        assert normalize_entity_name("MyService") == "myservice"

    def test_strips_aws_prefix(self) -> None:
        assert normalize_entity_name("aws_s3_bucket") == "s3_bucket"

    def test_strips_k8s_prefix(self) -> None:
        assert normalize_entity_name("k8s_deployment") == "deployment"

    def test_strips_azure_prefix(self) -> None:
        assert normalize_entity_name("azurerm_resource_group") == "resource_group"

    def test_strips_gcp_prefix(self) -> None:
        assert normalize_entity_name("gcp_cloud_run") == "cloud_run"

    def test_replaces_hyphens_with_underscores(self) -> None:
        assert normalize_entity_name("nginx-service") == "nginx_service"

    def test_replaces_dots(self) -> None:
        assert normalize_entity_name("my.service.name") == "my_service_name"

    def test_replaces_slashes(self) -> None:
        assert normalize_entity_name("Deployment/nginx") == "deployment_nginx"

    def test_strips_leading_trailing_underscores(self) -> None:
        result = normalize_entity_name("_foo_")
        assert not result.startswith("_")
        assert not result.endswith("_")

    def test_empty_string(self) -> None:
        assert normalize_entity_name("") == ""

    def test_no_prefix_unchanged(self) -> None:
        assert normalize_entity_name("nginx") == "nginx"

    def test_colon_separator(self) -> None:
        assert normalize_entity_name("service:nginx") == "service_nginx"


# ===========================================================================
# exact_name_match
# ===========================================================================


class TestExactNameMatch:
    def test_identical_names_score_one(self) -> None:
        assert exact_name_match("my_service", "my_service") == 1.0

    def test_case_insensitive_match(self) -> None:
        assert exact_name_match("MyService", "myservice") == 1.0

    def test_separator_normalisation(self) -> None:
        assert exact_name_match("my-service", "my_service") == 1.0

    def test_different_names_score_zero(self) -> None:
        assert exact_name_match("service_a", "service_b") == 0.0

    def test_empty_name_a_score_zero(self) -> None:
        assert exact_name_match("", "service") == 0.0

    def test_empty_name_b_score_zero(self) -> None:
        assert exact_name_match("service", "") == 0.0

    def test_prefix_stripped_before_compare(self) -> None:
        # aws_s3_bucket vs s3_bucket — after stripping they should match
        assert exact_name_match("aws_s3_bucket", "s3_bucket") == 1.0


# ===========================================================================
# resource_name_match
# ===========================================================================


class TestResourceNameMatch:
    def test_identical_names(self) -> None:
        assert resource_name_match("nginx", "nginx") == 1.0

    def test_tf_k8s_partial_overlap(self) -> None:
        score = resource_name_match("my_storage_bucket", "my-storage")
        assert score > 0.0

    def test_empty_tf_resource(self) -> None:
        assert resource_name_match("", "nginx") == 0.0

    def test_empty_k8s_resource(self) -> None:
        assert resource_name_match("nginx", "") == 0.0

    def test_completely_different_names(self) -> None:
        score = resource_name_match("postgres_database", "redis_cache")
        assert score < 0.5

    def test_score_bounded_zero_to_one(self) -> None:
        score = resource_name_match("aws_rds_instance.db", "Deployment/api-db")
        assert 0.0 <= score <= 1.0

    def test_high_overlap_scores_above_threshold(self) -> None:
        score = resource_name_match("aws_instance.nginx", "nginx-deployment")
        assert score >= 0.4

    def test_prefix_stripped_before_compare(self) -> None:
        score = resource_name_match("aws_nginx_instance", "k8s_nginx_service")
        assert score > 0.0


# ===========================================================================
# EntityLinker — unit tests with mocked GraphStore
# ===========================================================================


class TestEntityLinkerLinkEntities:
    @pytest.mark.asyncio
    async def test_returns_zero_when_no_entities(self) -> None:
        """link_entities returns 0 immediately when the workspace has no entities."""
        ws = uuid.uuid4()
        store = _make_graph_store(entities=[])
        linker = EntityLinker(graph_store=store)
        count = await linker.link_entities(source_id=uuid.uuid4(), workspace_id=ws)
        assert count == 0

    @pytest.mark.asyncio
    async def test_creates_edge_for_exact_name_match(self) -> None:
        """Exact-name match across two sources creates a CROSS_REF_EDGE_TYPE edge."""
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_node(source=source_a, name="nginx")
        ent_b = _make_node(source=source_b, name="nginx")

        store = _make_graph_store(entities=[ent_a, ent_b])
        linker = EntityLinker(graph_store=store)
        count = await linker.link_entities(source_id=source_a, workspace_id=ws)

        assert count == 1
        store.upsert_edge.assert_awaited_once()
        edge_arg = store.upsert_edge.call_args.kwargs["edge"]
        assert edge_arg.edge_type == CROSS_REF_EDGE_TYPE

    @pytest.mark.asyncio
    async def test_no_edge_for_same_source(self) -> None:
        """Two entities from the same source never produce a cross-ref edge.

        Production splits entities into source_entities vs other_entities before
        calling _compute_links, so same-source pairs are never candidates.
        """
        ws = uuid.uuid4()
        source_id = uuid.uuid4()
        ent_a = _make_node(source=source_id, name="nginx")
        ent_b = _make_node(source=source_id, name="nginx")

        store = _make_graph_store(entities=[ent_a, ent_b])
        linker = EntityLinker(graph_store=store)
        count = await linker.link_entities(source_id=source_id, workspace_id=ws)

        assert count == 0
        store.upsert_edge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_edge_for_clearly_different_names(self) -> None:
        """Entities with clearly different names produce no edge."""
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_node(source=source_a, name="postgres_primary")
        ent_b = _make_node(source=source_b, name="redis_cache_secondary")

        store = _make_graph_store(entities=[ent_a, ent_b])
        linker = EntityLinker(graph_store=store)
        count = await linker.link_entities(source_id=source_a, workspace_id=ws)

        assert count == 0
        store.upsert_edge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edge_metadata_contains_score(self) -> None:
        """upsert_edge is called with metadata containing a numeric score."""
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_node(source=source_a, name="myservice")
        ent_b = _make_node(source=source_b, name="myservice")

        store = _make_graph_store(entities=[ent_a, ent_b])
        linker = EntityLinker(graph_store=store)
        await linker.link_entities(source_id=source_a, workspace_id=ws)

        edge_arg = store.upsert_edge.call_args.kwargs["edge"]
        assert "score" in edge_arg.metadata
        assert edge_arg.metadata["score"] == 1.0

    @pytest.mark.asyncio
    async def test_edge_metadata_contains_strategy(self) -> None:
        """upsert_edge is called with metadata containing a strategy string."""
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_node(source=source_a, name="myservice")
        ent_b = _make_node(source=source_b, name="myservice")

        store = _make_graph_store(entities=[ent_a, ent_b])
        linker = EntityLinker(graph_store=store)
        await linker.link_entities(source_id=source_a, workspace_id=ws)

        edge_arg = store.upsert_edge.call_args.kwargs["edge"]
        assert "strategy" in edge_arg.metadata
        assert edge_arg.metadata["strategy"] in {
            "exact_name",
            "resource_name",
            "jira_ticket_match",
        }

    @pytest.mark.asyncio
    async def test_resource_name_match_tf_k8s(self) -> None:
        """Terraform↔K8s resource_name strategy creates an edge when score >= threshold."""
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_tf = _make_node(source=source_a, kind="terraform_resource", name="aws_instance.nginx")
        ent_k8s = _make_node(source=source_b, kind="k8s_resource", name="nginx-deployment")

        store = _make_graph_store(entities=[ent_tf, ent_k8s])
        linker = EntityLinker(graph_store=store)
        count = await linker.link_entities(source_id=source_a, workspace_id=ws)

        # "nginx" common token — resource_name score crosses threshold
        assert count >= 1

    @pytest.mark.asyncio
    async def test_link_entities_returns_count(self) -> None:
        """link_entities returns the number of upsert_edge calls made."""
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_node(source=source_a, name="shared_service")
        ent_b = _make_node(source=source_b, name="shared_service")

        store = _make_graph_store(entities=[ent_a, ent_b])
        linker = EntityLinker(graph_store=store)
        count = await linker.link_entities(source_id=source_a, workspace_id=ws)

        assert count == 1
        assert store.upsert_edge.await_count == 1

    @pytest.mark.asyncio
    async def test_idempotency_upsert_called_with_same_pair_on_rerun(self) -> None:
        """Idempotency: running link_entities twice calls upsert_edge with the
        same (source_entity_id, target_entity_id) pair both times.

        The graph-store guarantees deduplication via upsert semantics — the
        production code does NOT maintain a client-side duplicate-tracking set
        between runs.  The correct assertion is therefore that the same edge
        args are submitted on each call (not that the second call is skipped),
        and that the graph layer absorbs the duplicate.
        """
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_node(source=source_a, name="shared_service")
        ent_b = _make_node(source=source_b, name="shared_service")

        store = _make_graph_store(entities=[ent_a, ent_b])
        linker = EntityLinker(graph_store=store)

        await linker.link_entities(source_id=source_a, workspace_id=ws)
        first_edge = store.upsert_edge.call_args.kwargs["edge"]

        # Reset call tracking; re-run with same entity set
        store.upsert_edge.reset_mock()
        await linker.link_entities(source_id=source_a, workspace_id=ws)
        second_edge = store.upsert_edge.call_args.kwargs["edge"]

        # Both runs submit the same (src, tgt) pair — graph dedupes via upsert
        assert first_edge.source_entity_id == second_edge.source_entity_id
        assert first_edge.target_entity_id == second_edge.target_entity_id
        assert first_edge.edge_type == second_edge.edge_type


# ===========================================================================
# EntityLinker — resolve_stubs
# ===========================================================================


class TestEntityLinkerResolve:
    @pytest.mark.asyncio
    async def test_resolve_returns_zero_when_no_stubs(self) -> None:
        """resolve_stubs returns 0 when graph_store.resolve_pending_stubs returns 0."""
        ws = uuid.uuid4()
        store = _make_graph_store(resolve_count=0)
        linker = EntityLinker(graph_store=store)
        count = await linker.resolve_stubs(workspace_id=ws)
        assert count == 0
        store.resolve_pending_stubs.assert_awaited_once_with(workspace_id=ws)

    @pytest.mark.asyncio
    async def test_resolve_returns_stub_count_from_store(self) -> None:
        """resolve_stubs returns the count from graph_store.resolve_pending_stubs.

        NOTE: The SQL-era resolve_cross_references iterated all entities and
        called session.add for each pair.  The graph-based resolve_stubs
        delegates entirely to a Cypher batch in the store; the test asserts
        the delegated return value and that the store method was called.
        """
        ws = uuid.uuid4()
        store = _make_graph_store(resolve_count=3)
        linker = EntityLinker(graph_store=store)
        count = await linker.resolve_stubs(workspace_id=ws)
        assert count == 3
        store.resolve_pending_stubs.assert_awaited_once_with(workspace_id=ws)

    @pytest.mark.asyncio
    async def test_link_entities_calls_resolve_stubs(self) -> None:
        """link_entities always calls resolve_stubs (stub resolution pass) after linking."""
        ws = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_node(source=source_a, name="shared")
        ent_b = _make_node(source=source_b, name="shared")

        store = _make_graph_store(entities=[ent_a, ent_b], resolve_count=1)
        linker = EntityLinker(graph_store=store)
        await linker.link_entities(source_id=source_a, workspace_id=ws)

        # resolve_pending_stubs must be called as part of the link_entities flow
        store.resolve_pending_stubs.assert_awaited_once_with(workspace_id=ws)


# ===========================================================================
# Pipeline wiring — _stage_link
# ===========================================================================


class TestPipelineStageLinkWiring:
    @pytest.mark.asyncio
    async def test_stage_link_calls_link_entities(self) -> None:
        """_stage_link forwards source_id and workspace_id to entity_linker.link_entities."""
        from omniscience_server.ingestion.events import DocumentChangeEvent
        from omniscience_server.ingestion.pipeline import IngestionPipeline

        source_id = uuid.uuid4()
        workspace_id = uuid.uuid4()

        mock_linker = AsyncMock()
        mock_linker.link_entities = AsyncMock(return_value=3)

        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline._entity_linker = mock_linker

        event = MagicMock(spec=DocumentChangeEvent)
        event.source_id = source_id
        event.source_type = "git"

        bound = MagicMock()
        bound.debug = MagicMock()
        bound.warning = MagicMock()

        await pipeline._stage_link(event, workspace_id, bound)

        mock_linker.link_entities.assert_called_once_with(
            source_id=source_id,
            workspace_id=workspace_id,
        )

    @pytest.mark.asyncio
    async def test_stage_link_skipped_when_no_linker(self) -> None:
        """_stage_link is a no-op when entity_linker is None."""
        from omniscience_server.ingestion.events import DocumentChangeEvent
        from omniscience_server.ingestion.pipeline import IngestionPipeline

        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline._entity_linker = None

        event = MagicMock(spec=DocumentChangeEvent)
        event.source_id = uuid.uuid4()
        event.source_type = "git"

        bound = MagicMock()

        # Should return without error; no calls to bound.debug
        await pipeline._stage_link(event, uuid.uuid4(), bound)
        bound.debug.assert_not_called()

    @pytest.mark.asyncio
    async def test_stage_link_swallows_errors(self) -> None:
        """An exception in link_entities must NOT propagate out of _stage_link."""
        from omniscience_server.ingestion.events import DocumentChangeEvent
        from omniscience_server.ingestion.pipeline import IngestionPipeline

        mock_linker = AsyncMock()
        mock_linker.link_entities = AsyncMock(side_effect=RuntimeError("DB failure"))

        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline._entity_linker = mock_linker

        event = MagicMock(spec=DocumentChangeEvent)
        event.source_id = uuid.uuid4()
        event.source_type = "git"

        bound = MagicMock()
        bound.warning = MagicMock()

        # Must not raise
        await pipeline._stage_link(event, uuid.uuid4(), bound)
        bound.warning.assert_called_once()
