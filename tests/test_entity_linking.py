"""Tests for Issue #29 — Cross-source entity linking.

Coverage:
- normalize_entity_name: prefix stripping, separator normalisation, edge cases
- exact_name_match: match/no-match, case insensitivity, separator variance
- resource_name_match: tf↔k8s fuzzy matching, threshold behaviour, empty inputs
- EntityLinker.link_entities: creates cross-ref edges, skips same-source pairs
- EntityLinker.link_entities: idempotency (running twice doesn't duplicate edges)
- EntityLinker.link_entities: returns count of new edges
- EntityLinker.resolve_cross_references: global pass over multiple sources
- Pipeline stage_link wiring: called after graph stage, swallows errors
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_core.db.models import Entity
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


def _make_entity(
    *,
    source_id: uuid.UUID | None = None,
    entity_type: str = "function",
    name: str = "mymod.my_func",
    display_name: str | None = None,
    entity_metadata: dict[str, Any] | None = None,
) -> Entity:
    return Entity(
        id=uuid.uuid4(),
        source_id=source_id or uuid.uuid4(),
        entity_type=entity_type,
        name=name,
        display_name=display_name or name.split(".")[-1],
        chunk_id=None,
        entity_metadata=entity_metadata or {},
        created_at=_NOW,
    )


def _make_session_factory(
    entities: list[Entity] | None = None,
    existing_edges: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
) -> MagicMock:
    """Build an async_sessionmaker mock for EntityLinker tests.

    ``entities`` is the list returned by SELECT Entity queries.
    ``existing_edges`` is the list of (src_id, tgt_id) pairs for cross_ref edges.
    """
    entities = entities or []
    existing_edges = existing_edges or []

    # Scalars result for Entity queries
    def _make_scalars_result(rows: list[Any]) -> MagicMock:
        sr = MagicMock()
        sr.scalars.return_value.all.return_value = rows
        return sr

    # Rows result for Edge pair queries
    def _make_rows_result(pairs: list[tuple[uuid.UUID, uuid.UUID]]) -> MagicMock:
        rr = MagicMock()
        rr.__iter__ = MagicMock(return_value=iter(pairs))
        return rr

    session = AsyncMock()

    # execute is called multiple times; cycle through expected return values
    call_returns = [
        _make_scalars_result(entities),  # _fetch_entities_for_source
        _make_scalars_result(entities),  # _fetch_entities_excluding_source
        _make_rows_result(existing_edges),  # _fetch_existing_cross_ref_pairs
    ]

    async def _execute_side_effect(_stmt: Any) -> Any:
        if call_returns:
            return call_returns.pop(0)
        return _make_scalars_result([])

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    session.flush = AsyncMock()
    session.add = MagicMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=tx)

    factory = MagicMock()
    factory.return_value = cm
    return factory


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
        # If prefix stripping leaves leading/trailing underscores
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
        # "aws_s3_bucket.my_storage" ↔ "my-storage" should have overlap
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
        # Both refer to "nginx" — should score high
        score = resource_name_match("aws_instance.nginx", "nginx-deployment")
        assert score >= 0.4

    def test_prefix_stripped_before_compare(self) -> None:
        # aws_nginx_instance vs k8s_nginx → after stripping, "nginx" common token
        score = resource_name_match("aws_nginx_instance", "k8s_nginx_service")
        assert score > 0.0


# ===========================================================================
# EntityLinker — unit tests with mocked session
# ===========================================================================


class TestEntityLinkerLinkEntities:
    @pytest.mark.asyncio
    async def test_returns_zero_when_no_entities(self) -> None:
        factory = _make_session_factory(entities=[])
        linker = EntityLinker(factory)
        count = await linker.link_entities(uuid.uuid4())
        assert count == 0

    @pytest.mark.asyncio
    async def test_creates_edge_for_exact_name_match(self) -> None:
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_entity(source_id=source_a, name="nginx", display_name="nginx")
        ent_b = _make_entity(source_id=source_b, name="nginx", display_name="nginx")

        linker = EntityLinker.__new__(EntityLinker)

        existing: set[tuple[uuid.UUID, uuid.UUID]] = set()
        new_edges = linker._compute_links([ent_a], [ent_b], existing)

        assert len(new_edges) == 1
        assert new_edges[0].edge_type == CROSS_REF_EDGE_TYPE

    @pytest.mark.asyncio
    async def test_no_edge_for_same_source(self) -> None:
        source_id = uuid.uuid4()
        ent_a = _make_entity(source_id=source_id, name="nginx")
        ent_b = _make_entity(source_id=source_id, name="nginx")

        linker = EntityLinker.__new__(EntityLinker)
        existing: set[tuple[uuid.UUID, uuid.UUID]] = set()
        new_edges = linker._compute_links([ent_a], [ent_b], existing)

        assert new_edges == []

    @pytest.mark.asyncio
    async def test_idempotency_no_duplicate_edge(self) -> None:
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_entity(source_id=source_a, name="nginx")
        ent_b = _make_entity(source_id=source_b, name="nginx")

        linker = EntityLinker.__new__(EntityLinker)

        # First pass — creates edge
        existing: set[tuple[uuid.UUID, uuid.UUID]] = set()
        first_pass = linker._compute_links([ent_a], [ent_b], existing)
        assert len(first_pass) == 1

        # Second pass — existing_pairs now contains the pair
        second_pass = linker._compute_links([ent_a], [ent_b], existing)
        assert len(second_pass) == 0

    @pytest.mark.asyncio
    async def test_edge_metadata_contains_score(self) -> None:
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_entity(source_id=source_a, name="myservice")
        ent_b = _make_entity(source_id=source_b, name="myservice")

        linker = EntityLinker.__new__(EntityLinker)
        existing: set[tuple[uuid.UUID, uuid.UUID]] = set()
        edges = linker._compute_links([ent_a], [ent_b], existing)

        assert "score" in edges[0].edge_metadata
        assert edges[0].edge_metadata["score"] == 1.0

    @pytest.mark.asyncio
    async def test_edge_metadata_contains_strategy(self) -> None:
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_entity(source_id=source_a, name="myservice")
        ent_b = _make_entity(source_id=source_b, name="myservice")

        linker = EntityLinker.__new__(EntityLinker)
        existing: set[tuple[uuid.UUID, uuid.UUID]] = set()
        edges = linker._compute_links([ent_a], [ent_b], existing)

        assert "strategy" in edges[0].edge_metadata
        assert edges[0].edge_metadata["strategy"] in {
            "exact_name",
            "exact_display_name",
            "resource_name",
            "service_name",
        }

    @pytest.mark.asyncio
    async def test_no_edge_for_clearly_different_names(self) -> None:
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_entity(source_id=source_a, name="postgres_primary")
        ent_b = _make_entity(source_id=source_b, name="redis_cache_secondary")

        linker = EntityLinker.__new__(EntityLinker)
        existing: set[tuple[uuid.UUID, uuid.UUID]] = set()
        edges = linker._compute_links([ent_a], [ent_b], existing)

        assert edges == []

    @pytest.mark.asyncio
    async def test_resource_name_match_tf_k8s(self) -> None:
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_tf = _make_entity(
            source_id=source_a,
            entity_type="terraform_resource",
            name="aws_instance.nginx",
            display_name="nginx",
        )
        ent_k8s = _make_entity(
            source_id=source_b,
            entity_type="k8s_resource",
            name="nginx-deployment",
            display_name="nginx",
        )

        linker = EntityLinker.__new__(EntityLinker)
        existing: set[tuple[uuid.UUID, uuid.UUID]] = set()
        edges = linker._compute_links([ent_tf], [ent_k8s], existing)

        # display_name "nginx" == "nginx" → exact_display_name match
        assert len(edges) >= 1

    @pytest.mark.asyncio
    async def test_link_entities_returns_count(self) -> None:
        """link_entities returns the number of new edges created."""
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()

        ent_a = _make_entity(source_id=source_a, name="shared_service")
        ent_b = _make_entity(source_id=source_b, name="shared_service")

        # Simulate DB: both queries return all entities (source + others)


        session = AsyncMock()

        def _scalars(rows: list[Any]) -> MagicMock:
            r = MagicMock()
            r.scalars.return_value.all.return_value = rows
            return r

        def _rows(pairs: list[Any]) -> MagicMock:
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter(pairs))
            return r

        call_returns = [
            _scalars([ent_a]),       # fetch for source_a
            _scalars([ent_b]),       # fetch excluding source_a
            _rows([]),               # existing cross_ref pairs
        ]

        async def _exec(_stmt: Any) -> Any:
            return call_returns.pop(0)

        session.execute = AsyncMock(side_effect=_exec)
        session.flush = AsyncMock()
        session.add = MagicMock()

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)

        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=tx)

        factory = MagicMock()
        factory.return_value = cm

        linker = EntityLinker(factory)
        count = await linker.link_entities(source_a)

        assert count == 1
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_link_entities_idempotent_with_existing_pairs(self) -> None:
        """Running link_entities twice returns 0 on second call (existing_pairs loaded)."""
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_entity(source_id=source_a, name="shared_service")
        ent_b = _make_entity(source_id=source_b, name="shared_service")

        # Second call: existing_pairs already has the pair
        existing_pair = (ent_a.id, ent_b.id)

        session = AsyncMock()

        def _scalars(rows: list[Any]) -> MagicMock:
            r = MagicMock()
            r.scalars.return_value.all.return_value = rows
            return r

        def _rows(pairs: list[Any]) -> MagicMock:
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter(pairs))
            return r

        call_returns = [
            _scalars([ent_a]),
            _scalars([ent_b]),
            _rows([existing_pair]),  # already linked
        ]

        async def _exec(_stmt: Any) -> Any:
            return call_returns.pop(0)

        session.execute = AsyncMock(side_effect=_exec)
        session.flush = AsyncMock()
        session.add = MagicMock()

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=tx)

        factory = MagicMock()
        factory.return_value = cm

        linker = EntityLinker(factory)
        count = await linker.link_entities(source_a)

        assert count == 0
        session.add.assert_not_called()


# ===========================================================================
# EntityLinker — resolve_cross_references
# ===========================================================================


class TestEntityLinkerResolve:
    @pytest.mark.asyncio
    async def test_resolve_returns_zero_when_no_entities(self) -> None:
        session = AsyncMock()

        def _scalars(rows: list[Any]) -> MagicMock:
            r = MagicMock()
            r.scalars.return_value.all.return_value = rows
            return r

        def _rows(pairs: list[Any]) -> MagicMock:
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter(pairs))
            return r

        call_returns = [_scalars([])]

        async def _exec(_stmt: Any) -> Any:
            return call_returns.pop(0)

        session.execute = AsyncMock(side_effect=_exec)
        session.flush = AsyncMock()
        session.add = MagicMock()

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=tx)

        factory = MagicMock()
        factory.return_value = cm

        linker = EntityLinker(factory)
        count = await linker.resolve_cross_references()
        assert count == 0

    @pytest.mark.asyncio
    async def test_resolve_links_across_two_sources(self) -> None:
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()
        ent_a = _make_entity(source_id=source_a, name="shared")
        ent_b = _make_entity(source_id=source_b, name="shared")

        session = AsyncMock()

        def _scalars(rows: list[Any]) -> MagicMock:
            r = MagicMock()
            r.scalars.return_value.all.return_value = rows
            return r

        def _rows(pairs: list[Any]) -> MagicMock:
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter(pairs))
            return r

        call_returns = [
            _scalars([ent_a, ent_b]),  # _fetch_all_entities
            _rows([]),                  # _fetch_existing_cross_ref_pairs
        ]

        async def _exec(_stmt: Any) -> Any:
            return call_returns.pop(0)

        session.execute = AsyncMock(side_effect=_exec)
        session.flush = AsyncMock()
        session.add = MagicMock()

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=tx)

        factory = MagicMock()
        factory.return_value = cm

        linker = EntityLinker(factory)
        count = await linker.resolve_cross_references()

        assert count == 1
        session.add.assert_called_once()


# ===========================================================================
# Pipeline wiring — _stage_link
# ===========================================================================


class TestPipelineStageLinkWiring:
    @pytest.mark.asyncio
    async def test_stage_link_calls_link_entities(self) -> None:
        """_stage_link forwards source_id to entity_linker.link_entities."""
        from omniscience_server.ingestion.events import DocumentChangeEvent
        from omniscience_server.ingestion.pipeline import IngestionPipeline

        source_id = uuid.uuid4()

        mock_linker = AsyncMock()
        mock_linker.link_entities = AsyncMock(return_value=3)

        # Minimal pipeline — only need the linker wired
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline._entity_linker = mock_linker

        event = MagicMock(spec=DocumentChangeEvent)
        event.source_id = source_id
        event.source_type = "git"

        bound = MagicMock()
        bound.debug = MagicMock()
        bound.warning = MagicMock()

        await pipeline._stage_link(event, bound)

        mock_linker.link_entities.assert_called_once_with(source_id)

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

        # Should return without error
        await pipeline._stage_link(event, bound)
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
        await pipeline._stage_link(event, bound)
        bound.warning.assert_called_once()
