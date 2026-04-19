"""Tests for federated search (Issue #74).

Covers:
- FederatedInstance model validation
- FederationConfig model defaults and enabled_instances filtering
- FederationConfig.from_json: empty string, valid JSON, invalid JSON, non-list JSON
- FederatedSearch: local-only (no peers)
- FederatedSearch: single remote success - merge and annotation
- FederatedSearch: multiple remotes fanned out in parallel
- FederatedSearch: remote failure is skipped, local still returned
- FederatedSearch: all remotes fail, local returned
- FederatedSearch: partial remote failure
- FederatedSearch: deduplication by chunk_id - highest priority wins (equal scores)
- FederatedSearch: deduplication by chunk_id - highest score wins within same priority
- FederatedSearch: results sorted by score descending after merge
- FederatedSearch: top_k capped to max_remote_results on remote call
- FederatedSearch: source_instance=None for local hits
- FederatedSearch: source_instance=<name> for remote hits
- FederatedSearch: disabled instances are skipped
- FederatedSearch: QueryStats are summed across all sources
- FederatedSearch: QueryStats duration_ms is max across sources
- FederatedSearch: local failure is re-raised immediately
- FederatedSearch: empty result from local and remote
- FederatedSearch: remote HTTP 4xx raises and is caught as peer failure
- FederatedSearch: remote HTTP 5xx raises and is caught as peer failure
- FederatedSearch: JSON payload forwarded to remote includes all request fields
- FederatedSearch: close() releases HTTP client
- Settings: federation fields have correct defaults
- Settings: federation_enabled=True with federation_instances JSON string
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from omniscience_core.config import Settings
from omniscience_retrieval import (
    FederatedInstance,
    FederatedSearch,
    FederationConfig,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from omniscience_retrieval.models import ChunkLineage, Citation, QueryStats, SourceInfo

# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _make_source_info(name: str = "repo-a", stype: str = "git") -> SourceInfo:
    return SourceInfo(id=uuid.uuid4(), name=name, type=stype)


def _make_citation(uri: str = "https://example.com/file.py") -> Citation:
    return Citation(uri=uri, title="file.py", indexed_at=_NOW, doc_version=1)


def _make_lineage() -> ChunkLineage:
    return ChunkLineage(
        ingestion_run_id=uuid.uuid4(),
        embedding_model="nomic-embed-text",
        embedding_provider="ollama",
        parser_version="0.4",
        chunker_strategy="paragraph",
    )


def _make_hit(
    score: float = 0.9,
    chunk_id: uuid.UUID | None = None,
    source_instance: str | None = None,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        score=score,
        text="sample text",
        source=_make_source_info(),
        citation=_make_citation(),
        lineage=_make_lineage(),
        metadata={},
        source_instance=source_instance,
    )


def _make_stats(
    total: int = 5,
    vector: int = 3,
    text: int = 2,
    duration_ms: float = 10.0,
) -> QueryStats:
    return QueryStats(
        total_matches_before_filters=total,
        vector_matches=vector,
        text_matches=text,
        duration_ms=duration_ms,
    )


def _make_result(
    hits: list[SearchHit] | None = None,
    stats: QueryStats | None = None,
) -> SearchResult:
    # Use explicit None sentinel so that _make_result(hits=[]) returns an empty hits list.
    return SearchResult(
        hits=hits if hits is not None else [_make_hit()],
        query_stats=stats if stats is not None else _make_stats(),
    )


def _make_local_service(result: SearchResult | None = None) -> AsyncMock:
    svc = AsyncMock()
    svc.search = AsyncMock(return_value=result if result is not None else _make_result())
    return svc


def _make_instance(
    name: str = "peer-a",
    url: str = "https://peer-a.example.com",
    token: str = "tok_abc",
    enabled: bool = True,
    priority: int = 0,
) -> FederatedInstance:
    return FederatedInstance(name=name, url=url, token=token, enabled=enabled, priority=priority)


def _ok_response(result: SearchResult) -> httpx.Response:
    """Build an httpx.Response that contains a serialised SearchResult."""
    return httpx.Response(200, json=result.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# FederatedInstance model tests
# ---------------------------------------------------------------------------


class TestFederatedInstance:
    def test_required_fields(self) -> None:
        inst = FederatedInstance(name="eu", url="https://eu.example.com", token="t")
        assert inst.name == "eu"
        assert inst.url == "https://eu.example.com"
        assert inst.token == "t"

    def test_defaults(self) -> None:
        inst = FederatedInstance(name="x", url="https://x.com", token="t")
        assert inst.enabled is True
        assert inst.priority == 0

    def test_disabled_instance(self) -> None:
        inst = FederatedInstance(name="x", url="https://x.com", token="t", enabled=False)
        assert inst.enabled is False

    def test_priority_ordering(self) -> None:
        high = FederatedInstance(name="a", url="https://a.com", token="t", priority=0)
        low = FederatedInstance(name="b", url="https://b.com", token="t", priority=10)
        assert high.priority < low.priority


# ---------------------------------------------------------------------------
# FederationConfig model tests
# ---------------------------------------------------------------------------


class TestFederationConfig:
    def test_defaults(self) -> None:
        cfg = FederationConfig()
        assert cfg.instances == []
        assert cfg.timeout_seconds == 5.0
        assert cfg.max_remote_results == 20

    def test_enabled_instances_filters_disabled(self) -> None:
        cfg = FederationConfig(
            instances=[
                _make_instance(name="a", enabled=True),
                _make_instance(name="b", enabled=False),
                _make_instance(name="c", enabled=True),
            ]
        )
        enabled = cfg.enabled_instances
        assert len(enabled) == 2
        assert all(i.enabled for i in enabled)

    def test_from_json_empty_string(self) -> None:
        cfg = FederationConfig.from_json("")
        assert cfg.instances == []

    def test_from_json_whitespace_only(self) -> None:
        cfg = FederationConfig.from_json("   \n  ")
        assert cfg.instances == []

    def test_from_json_valid_list(self) -> None:
        raw = json.dumps(
            [{"name": "eu", "url": "https://eu.example.com", "token": "tok_eu", "priority": 1}]
        )
        cfg = FederationConfig.from_json(raw)
        assert len(cfg.instances) == 1
        assert cfg.instances[0].name == "eu"
        assert cfg.instances[0].priority == 1

    def test_from_json_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            FederationConfig.from_json("{not valid json")

    def test_from_json_non_list_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            FederationConfig.from_json('{"name": "a"}')

    def test_from_json_multiple_instances(self) -> None:
        raw = json.dumps(
            [
                {"name": "us", "url": "https://us.com", "token": "t1"},
                {"name": "eu", "url": "https://eu.com", "token": "t2", "enabled": False},
            ]
        )
        cfg = FederationConfig.from_json(raw)
        assert len(cfg.instances) == 2
        assert len(cfg.enabled_instances) == 1


# ---------------------------------------------------------------------------
# FederatedSearch tests
# ---------------------------------------------------------------------------


class TestFederatedSearchLocalOnly:
    @pytest.mark.asyncio
    async def test_no_peers_returns_local_result(self) -> None:
        local_result = _make_result(hits=[_make_hit(score=0.9)])
        svc = _make_local_service(local_result)
        fed = FederatedSearch(local_service=svc, config=FederationConfig())

        result = await fed.search(SearchRequest(query="hello"))

        assert len(result.hits) == 1
        assert result.hits[0].source_instance is None

    @pytest.mark.asyncio
    async def test_local_hit_source_instance_is_none(self) -> None:
        hit = _make_hit(score=0.8)
        svc = _make_local_service(_make_result(hits=[hit]))
        fed = FederatedSearch(local_service=svc, config=FederationConfig())

        result = await fed.search(SearchRequest(query="x"))

        assert result.hits[0].source_instance is None


class TestFederatedSearchMerge:
    @pytest.mark.asyncio
    async def test_single_remote_merged_with_local(self) -> None:
        local_hit = _make_hit(score=0.9)
        remote_hit = _make_hit(score=0.8)
        local_result = _make_result(hits=[local_hit])
        remote_result = _make_result(hits=[remote_hit])

        svc = _make_local_service(local_result)
        cfg = FederationConfig(instances=[_make_instance(name="peer-1", url="https://p1.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://p1.com/api/v1/search").mock(
                return_value=_ok_response(remote_result)
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="merge test"))

        assert len(result.hits) == 2

    @pytest.mark.asyncio
    async def test_remote_hits_annotated_with_peer_name(self) -> None:
        """A hit returned by a remote peer is annotated with the peer's name."""
        local_result = _make_result(hits=[])  # local returns no hits
        remote_hit = _make_hit(score=0.7)
        remote_result = _make_result(hits=[remote_hit])

        svc = _make_local_service(local_result)
        cfg = FederationConfig(instances=[_make_instance(name="remote-eu", url="https://eu.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://eu.com/api/v1/search").mock(
                return_value=_ok_response(remote_result)
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="annotation"))

        assert len(result.hits) == 1
        assert result.hits[0].source_instance == "remote-eu"

    @pytest.mark.asyncio
    async def test_results_sorted_by_score_descending(self) -> None:
        hit_low = _make_hit(score=0.3)
        hit_high = _make_hit(score=0.95)
        local_result = _make_result(hits=[hit_low])
        remote_result = _make_result(hits=[hit_high])

        svc = _make_local_service(local_result)
        cfg = FederationConfig(instances=[_make_instance(url="https://peer.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://peer.com/api/v1/search").mock(
                return_value=_ok_response(remote_result)
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="sort test"))

        scores = [h.score for h in result.hits]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_deduplication_keeps_highest_priority_on_score_tie(self) -> None:
        """When the same chunk_id appears at the same score, the local copy wins (priority -1)."""
        shared_id = uuid.uuid4()
        # Equal scores: deduplication falls back to priority; local has priority=-1 which wins
        same_score = 0.75
        local_hit = _make_hit(score=same_score, chunk_id=shared_id)
        remote_hit = _make_hit(score=same_score, chunk_id=shared_id)

        local_result = _make_result(hits=[local_hit])
        remote_result = _make_result(hits=[remote_hit])

        svc = _make_local_service(local_result)
        cfg = FederationConfig(
            instances=[_make_instance(name="peer-b", url="https://b.com", priority=0)]
        )

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://b.com/api/v1/search").mock(return_value=_ok_response(remote_result))
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="dedup"))

        # Only one hit for the shared chunk_id
        ids = [h.chunk_id for h in result.hits]
        assert ids.count(shared_id) == 1
        # The local copy wins on priority tie-break (source_instance=None)
        matched = [h for h in result.hits if h.chunk_id == shared_id]
        assert matched[0].source_instance is None

    @pytest.mark.asyncio
    async def test_deduplication_keeps_highest_score_hit(self) -> None:
        """The higher-scoring copy of a chunk wins regardless of which peer it came from."""
        shared_id = uuid.uuid4()
        hit_low = _make_hit(score=0.4, chunk_id=shared_id)  # from p1
        hit_high = _make_hit(score=0.9, chunk_id=shared_id)  # from p2

        svc = _make_local_service(_make_result(hits=[]))
        cfg = FederationConfig(
            instances=[
                _make_instance(name="p1", url="https://p1.com", priority=1),
                _make_instance(name="p2", url="https://p2.com", priority=1),
            ]
        )

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://p1.com/api/v1/search").mock(
                return_value=_ok_response(_make_result(hits=[hit_low]))
            )
            rsps.post("https://p2.com/api/v1/search").mock(
                return_value=_ok_response(_make_result(hits=[hit_high]))
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="priority"))

        ids = [h.chunk_id for h in result.hits]
        assert ids.count(shared_id) == 1
        matched = next(h for h in result.hits if h.chunk_id == shared_id)
        assert matched.score == pytest.approx(0.9)


class TestFederatedSearchFailures:
    @pytest.mark.asyncio
    async def test_remote_network_error_skipped(self) -> None:
        local_hit = _make_hit(score=0.8)
        svc = _make_local_service(_make_result(hits=[local_hit]))
        cfg = FederationConfig(instances=[_make_instance(url="https://broken.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://broken.com/api/v1/search").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="fallback"))

        # Local result still returned
        assert len(result.hits) == 1
        assert result.hits[0].source_instance is None

    @pytest.mark.asyncio
    async def test_remote_http_5xx_is_peer_failure(self) -> None:
        svc = _make_local_service(_make_result(hits=[_make_hit()]))
        cfg = FederationConfig(instances=[_make_instance(url="https://err.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://err.com/api/v1/search").mock(
                return_value=httpx.Response(503, json={"detail": "unavailable"})
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="5xx"))

        # Still returns local result
        assert len(result.hits) >= 1

    @pytest.mark.asyncio
    async def test_remote_http_4xx_is_peer_failure(self) -> None:
        svc = _make_local_service(_make_result(hits=[_make_hit()]))
        cfg = FederationConfig(instances=[_make_instance(url="https://auth-fail.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://auth-fail.com/api/v1/search").mock(
                return_value=httpx.Response(401, json={"detail": "unauthorized"})
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="401"))

        assert len(result.hits) >= 1

    @pytest.mark.asyncio
    async def test_all_remotes_fail_returns_local(self) -> None:
        local_hit = _make_hit(score=0.9)
        svc = _make_local_service(_make_result(hits=[local_hit]))
        cfg = FederationConfig(
            instances=[
                _make_instance(name="p1", url="https://p1.com"),
                _make_instance(name="p2", url="https://p2.com"),
            ]
        )

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://p1.com/api/v1/search").mock(side_effect=httpx.ConnectError("down"))
            rsps.post("https://p2.com/api/v1/search").mock(
                side_effect=httpx.TimeoutException("timeout")
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="all fail"))

        assert len(result.hits) == 1
        assert result.hits[0].source_instance is None

    @pytest.mark.asyncio
    async def test_partial_remote_failure(self) -> None:
        """One remote fails, one succeeds — both local and the good remote contribute."""
        local_hit = _make_hit(score=0.9)
        remote_hit = _make_hit(score=0.75)
        svc = _make_local_service(_make_result(hits=[local_hit]))

        cfg = FederationConfig(
            instances=[
                _make_instance(name="bad", url="https://bad.com"),
                _make_instance(name="good", url="https://good.com"),
            ]
        )

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://bad.com/api/v1/search").mock(side_effect=httpx.ConnectError("down"))
            rsps.post("https://good.com/api/v1/search").mock(
                return_value=_ok_response(_make_result(hits=[remote_hit]))
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="partial"))

        assert len(result.hits) == 2
        instances = {h.source_instance for h in result.hits}
        assert None in instances  # local
        assert "good" in instances

    @pytest.mark.asyncio
    async def test_local_failure_is_reraised(self) -> None:
        svc = AsyncMock()
        svc.search = AsyncMock(side_effect=RuntimeError("local DB down"))
        cfg = FederationConfig()

        fed = FederatedSearch(local_service=svc, config=cfg)
        with pytest.raises(RuntimeError, match="local DB down"):
            await fed.search(SearchRequest(query="crash"))


class TestFederatedSearchBehaviours:
    @pytest.mark.asyncio
    async def test_disabled_instances_not_queried(self) -> None:
        local_result = _make_result(hits=[_make_hit()])
        svc = _make_local_service(local_result)
        cfg = FederationConfig(
            instances=[_make_instance(name="disabled", url="https://disabled.com", enabled=False)]
        )

        with respx.mock(assert_all_called=False) as rsps:
            route = rsps.post("https://disabled.com/api/v1/search").mock(
                return_value=_ok_response(local_result)
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            await fed.search(SearchRequest(query="disabled check"))

        assert route.call_count == 0

    @pytest.mark.asyncio
    async def test_top_k_capped_at_max_remote_results(self) -> None:
        """Remote call's top_k must not exceed config.max_remote_results."""
        captured_payload: dict[str, Any] = {}
        local_result = _make_result(hits=[])
        svc = _make_local_service(local_result)
        cfg = FederationConfig(
            instances=[_make_instance(url="https://cap.com")],
            max_remote_results=5,
        )

        def capture(request: httpx.Request) -> httpx.Response:
            import json as _json

            captured_payload.update(_json.loads(request.content))
            return _ok_response(local_result)

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://cap.com/api/v1/search").mock(side_effect=capture)
            fed = FederatedSearch(local_service=svc, config=cfg)
            await fed.search(SearchRequest(query="cap test", top_k=100))

        assert captured_payload["top_k"] == 5

    @pytest.mark.asyncio
    async def test_request_fields_forwarded_to_remote(self) -> None:
        """All SearchRequest fields are serialised and sent to remotes."""
        captured: dict[str, Any] = {}
        local_result = _make_result(hits=[])
        svc = _make_local_service(local_result)
        cfg = FederationConfig(instances=[_make_instance(url="https://fwd.com")])

        def capture(request: httpx.Request) -> httpx.Response:
            import json as _json

            captured.update(_json.loads(request.content))
            return _ok_response(local_result)

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://fwd.com/api/v1/search").mock(side_effect=capture)
            fed = FederatedSearch(local_service=svc, config=cfg)
            await fed.search(
                SearchRequest(
                    query="forward me",
                    sources=["src1"],
                    types=["git"],
                    retrieval_strategy="keyword",
                )
            )

        assert captured["query"] == "forward me"
        assert captured["sources"] == ["src1"]
        assert captured["types"] == ["git"]
        assert captured["retrieval_strategy"] == "keyword"

    @pytest.mark.asyncio
    async def test_bearer_token_sent_in_auth_header(self) -> None:
        captured_headers: dict[str, str] = {}
        local_result = _make_result(hits=[])
        svc = _make_local_service(local_result)
        cfg = FederationConfig(
            instances=[_make_instance(name="auth-peer", url="https://auth.com", token="SECRET")]
        )

        def capture(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return _ok_response(local_result)

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://auth.com/api/v1/search").mock(side_effect=capture)
            fed = FederatedSearch(local_service=svc, config=cfg)
            await fed.search(SearchRequest(query="auth check"))

        assert captured_headers.get("authorization") == "Bearer SECRET"

    @pytest.mark.asyncio
    async def test_query_stats_summed(self) -> None:
        local_stats = _make_stats(total=3, vector=2, text=1, duration_ms=8.0)
        remote_stats = _make_stats(total=5, vector=4, text=3, duration_ms=15.0)

        svc = _make_local_service(_make_result(hits=[], stats=local_stats))
        remote_result = _make_result(hits=[], stats=remote_stats)
        cfg = FederationConfig(instances=[_make_instance(url="https://stats.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://stats.com/api/v1/search").mock(
                return_value=_ok_response(remote_result)
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="stats"))

        assert result.query_stats.total_matches_before_filters == 8  # 3+5
        assert result.query_stats.vector_matches == 6  # 2+4
        assert result.query_stats.text_matches == 4  # 1+3

    @pytest.mark.asyncio
    async def test_query_stats_duration_is_max(self) -> None:
        local_stats = _make_stats(duration_ms=5.0)
        remote_stats = _make_stats(duration_ms=50.0)

        svc = _make_local_service(_make_result(hits=[], stats=local_stats))
        remote_result = _make_result(hits=[], stats=remote_stats)
        cfg = FederationConfig(instances=[_make_instance(url="https://dur.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://dur.com/api/v1/search").mock(
                return_value=_ok_response(remote_result)
            )
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="duration"))

        assert result.query_stats.duration_ms == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_empty_results_from_all(self) -> None:
        empty = _make_result(hits=[], stats=_make_stats(total=0, vector=0, text=0))
        svc = _make_local_service(empty)
        cfg = FederationConfig(instances=[_make_instance(url="https://empty.com")])

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://empty.com/api/v1/search").mock(return_value=_ok_response(empty))
            fed = FederatedSearch(local_service=svc, config=cfg)
            result = await fed.search(SearchRequest(query="nothing"))

        assert result.hits == []

    @pytest.mark.asyncio
    async def test_close_releases_http_client(self) -> None:
        svc = _make_local_service()
        fed = FederatedSearch(local_service=svc, config=FederationConfig())
        with patch.object(fed._http, "aclose", new_callable=AsyncMock) as mock_close:
            await fed.close()
        mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_remotes_queried_in_parallel(self) -> None:
        """Verify all enabled peers receive the request."""
        call_count = 0
        local_result = _make_result(hits=[])
        svc = _make_local_service(local_result)
        cfg = FederationConfig(
            instances=[
                _make_instance(name="p1", url="https://p1.com"),
                _make_instance(name="p2", url="https://p2.com"),
                _make_instance(name="p3", url="https://p3.com"),
            ]
        )

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _ok_response(local_result)

        with respx.mock(assert_all_called=False) as rsps:
            rsps.post("https://p1.com/api/v1/search").mock(side_effect=handle)
            rsps.post("https://p2.com/api/v1/search").mock(side_effect=handle)
            rsps.post("https://p3.com/api/v1/search").mock(side_effect=handle)
            fed = FederatedSearch(local_service=svc, config=cfg)
            await fed.search(SearchRequest(query="parallel"))

        assert call_count == 3


# ---------------------------------------------------------------------------
# Settings federation fields
# ---------------------------------------------------------------------------


class TestSettingsFederationFields:
    def test_federation_disabled_by_default(self) -> None:
        s = Settings(
            database_url="postgresql+asyncpg://u:p@h/db",
            nats_url="nats://localhost:4222",
        )
        assert s.federation_enabled is False

    def test_federation_instances_default_empty_string(self) -> None:
        s = Settings(
            database_url="postgresql+asyncpg://u:p@h/db",
            nats_url="nats://localhost:4222",
        )
        assert s.federation_instances == ""

    def test_federation_timeout_default(self) -> None:
        s = Settings(
            database_url="postgresql+asyncpg://u:p@h/db",
            nats_url="nats://localhost:4222",
        )
        assert s.federation_timeout_seconds == 5

    def test_federation_enabled_setting(self) -> None:
        s = Settings(
            database_url="postgresql+asyncpg://u:p@h/db",
            nats_url="nats://localhost:4222",
            federation_enabled=True,
        )
        assert s.federation_enabled is True

    def test_federation_instances_json_stored(self) -> None:
        instances_json = json.dumps(
            [{"name": "eu", "url": "https://eu.example.com", "token": "tok_1"}]
        )
        s = Settings(
            database_url="postgresql+asyncpg://u:p@h/db",
            nats_url="nats://localhost:4222",
            federation_instances=instances_json,
        )
        parsed = FederationConfig.from_json(s.federation_instances)
        assert len(parsed.instances) == 1
        assert parsed.instances[0].name == "eu"
