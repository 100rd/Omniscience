"""Tests for OmniscienceClient using mocked HTTP (respx)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from omniscience_client import OmniscienceClient
from omniscience_client.exceptions import (
    APIError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from omniscience_client.exceptions import PermissionError as OmnisciencePermissionError
from omniscience_client.types import (
    DocumentWithChunks,
    IngestionRun,
    SearchResult,
    Source,
    TokenCreateResponse,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

BASE_URL = "http://test.local"
TOKEN = "omni_test_token"

NOW = datetime.now(tz=UTC).isoformat()
_SOURCE_ID = str(uuid.uuid4())
_DOC_ID = str(uuid.uuid4())
_CHUNK_ID = str(uuid.uuid4())
_RUN_ID = str(uuid.uuid4())
_TOKEN_ID = str(uuid.uuid4())


def _make_source(
    source_id: str = _SOURCE_ID,
    name: str = "Test Source",
    source_type: str = "github",
) -> dict[str, Any]:
    return {
        "id": source_id,
        "type": source_type,
        "name": name,
        "config": {},
        "secrets_ref": None,
        "status": "active",
        "last_sync_at": None,
        "last_error": None,
        "last_error_at": None,
        "freshness_sla_seconds": None,
        "tenant_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _make_search_result(n_hits: int = 2) -> dict[str, Any]:
    hits = []
    for i in range(n_hits):
        hits.append(
            {
                "chunk_id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "score": round(0.9 - i * 0.1, 2),
                "text": f"Result text {i}",
                "source": {"id": _SOURCE_ID, "name": "Test Source", "type": "github"},
                "citation": {
                    "uri": f"https://example.com/doc/{i}",
                    "title": f"Doc {i}",
                    "indexed_at": NOW,
                    "doc_version": 1,
                },
                "lineage": {
                    "ingestion_run_id": None,
                    "embedding_model": "text-embedding-3-small",
                    "embedding_provider": "openai",
                    "parser_version": "1.0.0",
                    "chunker_strategy": "recursive",
                },
                "metadata": {},
            }
        )
    return {
        "hits": hits,
        "query_stats": {
            "total_matches_before_filters": n_hits + 5,
            "vector_matches": n_hits,
            "text_matches": n_hits,
            "duration_ms": 42.0,
        },
    }


def _make_document() -> dict[str, Any]:
    return {
        "document": {
            "id": _DOC_ID,
            "source_id": _SOURCE_ID,
            "external_id": "ext-001",
            "uri": "https://example.com/doc/1",
            "title": "Test Document",
            "content_hash": "abc123",
            "doc_version": 1,
            "doc_metadata": {},
            "indexed_at": NOW,
            "tombstoned_at": None,
        },
        "chunks": [
            {
                "id": _CHUNK_ID,
                "document_id": _DOC_ID,
                "ord": 0,
                "text": "First chunk text.",
                "symbol": None,
                "ingestion_run_id": None,
                "embedding_model": "text-embedding-3-small",
                "embedding_provider": "openai",
                "parser_version": "1.0.0",
                "chunker_strategy": "recursive",
                "chunk_metadata": {},
            }
        ],
    }


def _make_ingestion_run() -> dict[str, Any]:
    return {
        "id": _RUN_ID,
        "source_id": _SOURCE_ID,
        "started_at": NOW,
        "finished_at": NOW,
        "status": "success",
        "docs_new": 5,
        "docs_updated": 2,
        "docs_removed": 0,
        "run_errors": {},
    }


def _make_token_response() -> dict[str, Any]:
    return {
        "token": {
            "id": _TOKEN_ID,
            "name": "CI Token",
            "token_prefix": "omni_ci",
            "scopes": ["search", "sources:read"],
            "workspace_id": None,
            "created_at": NOW,
            "expires_at": None,
            "last_used_at": None,
            "is_active": True,
        },
        "secret": "omni_ci_plaintext_secret",
    }


@pytest.fixture()
def client() -> OmniscienceClient:
    return OmniscienceClient(base_url=BASE_URL, token=TOKEN)


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_search_returns_search_result(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=_make_search_result(2))
    )
    result = await client.search("retrieval augmented generation")
    assert isinstance(result, SearchResult)
    assert len(result.hits) == 2
    assert result.hits[0].score == pytest.approx(0.9)


@respx.mock
@pytest.mark.asyncio
async def test_search_sends_correct_body(client: OmniscienceClient) -> None:
    route = respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=_make_search_result(1))
    )
    await client.search(
        "test query",
        top_k=5,
        sources=["src-1"],
        types=["code"],
        max_age_seconds=3600,
        filters={"lang": "python"},
        retrieval_strategy="keyword",
    )
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["query"] == "test query"
    assert body["top_k"] == 5
    assert body["sources"] == ["src-1"]
    assert body["types"] == ["code"]
    assert body["max_age_seconds"] == 3600
    assert body["filters"] == {"lang": "python"}
    assert body["retrieval_strategy"] == "keyword"


@respx.mock
@pytest.mark.asyncio
async def test_search_omits_none_fields(client: OmniscienceClient) -> None:
    route = respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=_make_search_result(0))
    )
    await client.search("minimal query")
    import json

    body = json.loads(route.calls.last.request.content)
    assert "sources" not in body
    assert "types" not in body
    assert "max_age_seconds" not in body
    assert "filters" not in body


@respx.mock
@pytest.mark.asyncio
async def test_search_zero_hits(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=_make_search_result(0))
    )
    result = await client.search("nothing matches this")
    assert result.hits == []
    assert result.query_stats.duration_ms == pytest.approx(42.0)


@respx.mock
@pytest.mark.asyncio
async def test_search_401_raises_authentication_error(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(401, json={"detail": "Unauthorized"})
    )
    with pytest.raises(AuthenticationError):
        await client.search("query")


@respx.mock
@pytest.mark.asyncio
async def test_search_403_raises_permission_error(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )
    with pytest.raises(OmnisciencePermissionError):
        await client.search("query")


@respx.mock
@pytest.mark.asyncio
async def test_search_503_raises_server_error(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(503, json={"detail": "Service unavailable"})
    )
    with pytest.raises(ServerError) as exc_info:
        await client.search("query")
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Sources tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_list_sources_returns_list(client: OmniscienceClient) -> None:
    respx.get(f"{BASE_URL}/api/v1/sources").mock(
        return_value=httpx.Response(200, json=[_make_source()])
    )
    sources = await client.list_sources()
    assert len(sources) == 1
    assert isinstance(sources[0], Source)
    assert sources[0].name == "Test Source"


@respx.mock
@pytest.mark.asyncio
async def test_list_sources_empty(client: OmniscienceClient) -> None:
    respx.get(f"{BASE_URL}/api/v1/sources").mock(return_value=httpx.Response(200, json=[]))
    sources = await client.list_sources()
    assert sources == []


@respx.mock
@pytest.mark.asyncio
async def test_list_sources_passes_filters(client: OmniscienceClient) -> None:
    route = respx.get(f"{BASE_URL}/api/v1/sources").mock(
        return_value=httpx.Response(200, json=[_make_source()])
    )
    await client.list_sources(source_type="github", status="active")
    params = dict(route.calls.last.request.url.params)
    assert params["source_type"] == "github"
    assert params["status"] == "active"


@respx.mock
@pytest.mark.asyncio
async def test_create_source_returns_source(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/sources").mock(
        return_value=httpx.Response(201, json=_make_source())
    )
    source = await client.create_source(
        type="github",
        name="Test Source",
        config={"repo": "acme/docs"},
    )
    assert isinstance(source, Source)
    assert source.type == "github"


@respx.mock
@pytest.mark.asyncio
async def test_create_source_sends_correct_body(client: OmniscienceClient) -> None:
    route = respx.post(f"{BASE_URL}/api/v1/sources").mock(
        return_value=httpx.Response(201, json=_make_source())
    )
    await client.create_source(
        type="confluence",
        name="Confluence Wiki",
        config={"space_key": "ENG"},
        secrets_ref="vault/confluence",
        freshness_sla_seconds=3600,
    )
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["type"] == "confluence"
    assert body["name"] == "Confluence Wiki"
    assert body["config"] == {"space_key": "ENG"}
    assert body["secrets_ref"] == "vault/confluence"
    assert body["freshness_sla_seconds"] == 3600


# ---------------------------------------------------------------------------
# Document tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_get_document_returns_document_with_chunks(client: OmniscienceClient) -> None:
    respx.get(f"{BASE_URL}/api/v1/documents/{_DOC_ID}").mock(
        return_value=httpx.Response(200, json=_make_document())
    )
    doc = await client.get_document(_DOC_ID)
    assert isinstance(doc, DocumentWithChunks)
    assert str(doc.document.id) == _DOC_ID
    assert len(doc.chunks) == 1
    assert doc.chunks[0].text == "First chunk text."


@respx.mock
@pytest.mark.asyncio
async def test_get_document_404_raises_not_found(client: OmniscienceClient) -> None:
    respx.get(f"{BASE_URL}/api/v1/documents/{_DOC_ID}").mock(
        return_value=httpx.Response(
            404, json={"code": "document_not_found", "message": "Not found"}
        )
    )
    with pytest.raises(NotFoundError):
        await client.get_document(_DOC_ID)


# ---------------------------------------------------------------------------
# Ingestion runs tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_list_ingestion_runs_returns_list(client: OmniscienceClient) -> None:
    respx.get(f"{BASE_URL}/api/v1/ingestion-runs").mock(
        return_value=httpx.Response(200, json=[_make_ingestion_run()])
    )
    runs = await client.list_ingestion_runs()
    assert len(runs) == 1
    assert isinstance(runs[0], IngestionRun)
    assert runs[0].status == "success"
    assert runs[0].docs_new == 5


@respx.mock
@pytest.mark.asyncio
async def test_list_ingestion_runs_passes_params(client: OmniscienceClient) -> None:
    route = respx.get(f"{BASE_URL}/api/v1/ingestion-runs").mock(
        return_value=httpx.Response(200, json=[])
    )
    await client.list_ingestion_runs(source_id=_SOURCE_ID, status="running", limit=10)
    params = dict(route.calls.last.request.url.params)
    assert params["source_id"] == _SOURCE_ID
    assert params["status"] == "running"
    assert params["limit"] == "10"


@respx.mock
@pytest.mark.asyncio
async def test_list_ingestion_runs_empty(client: OmniscienceClient) -> None:
    respx.get(f"{BASE_URL}/api/v1/ingestion-runs").mock(return_value=httpx.Response(200, json=[]))
    runs = await client.list_ingestion_runs()
    assert runs == []


# ---------------------------------------------------------------------------
# Token tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_create_token_returns_response(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/tokens").mock(
        return_value=httpx.Response(201, json=_make_token_response())
    )
    resp = await client.create_token("CI Token", ["search", "sources:read"])
    assert isinstance(resp, TokenCreateResponse)
    assert resp.secret == "omni_ci_plaintext_secret"
    assert resp.token.name == "CI Token"
    assert "search" in resp.token.scopes


@respx.mock
@pytest.mark.asyncio
async def test_create_token_sends_correct_body(client: OmniscienceClient) -> None:
    route = respx.post(f"{BASE_URL}/api/v1/tokens").mock(
        return_value=httpx.Response(201, json=_make_token_response())
    )
    await client.create_token(
        "Expiring Token",
        ["search"],
        expires_at="2027-01-01T00:00:00Z",
    )
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["name"] == "Expiring Token"
    assert body["scopes"] == ["search"]
    assert body["expires_at"] == "2027-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Auth header injection
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_token_sent_as_bearer_header() -> None:
    route = respx.get(f"{BASE_URL}/api/v1/sources").mock(return_value=httpx.Response(200, json=[]))
    c = OmniscienceClient(base_url=BASE_URL, token="omni_secret")
    await c.list_sources()
    await c.close()
    assert route.calls.last.request.headers["authorization"] == "Bearer omni_secret"


@respx.mock
@pytest.mark.asyncio
async def test_no_token_no_auth_header() -> None:
    route = respx.get(f"{BASE_URL}/api/v1/sources").mock(return_value=httpx.Response(200, json=[]))
    c = OmniscienceClient(base_url=BASE_URL)
    await c.list_sources()
    await c.close()
    assert "authorization" not in route.calls.last.request.headers


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_client_as_context_manager() -> None:
    respx.get(f"{BASE_URL}/api/v1/sources").mock(return_value=httpx.Response(200, json=[]))
    async with OmniscienceClient(base_url=BASE_URL, token=TOKEN) as c:
        sources = await c.list_sources()
    assert sources == []


# ---------------------------------------------------------------------------
# Rate limit error
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_429_raises_rate_limit_error(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(429, json={"detail": "Too many requests"})
    )
    with pytest.raises(RateLimitError):
        await client.search("query")


# ---------------------------------------------------------------------------
# Generic API error for unexpected status codes
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_unexpected_status_raises_api_error(client: OmniscienceClient) -> None:
    respx.post(f"{BASE_URL}/api/v1/search").mock(
        return_value=httpx.Response(422, json={"detail": "Unprocessable entity"})
    )
    with pytest.raises(APIError) as exc_info:
        await client.search("query")
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Non-JSON error body fallback
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_non_json_error_body_handled(client: OmniscienceClient) -> None:
    respx.get(f"{BASE_URL}/api/v1/sources").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )
    with pytest.raises(ServerError):
        await client.list_sources()
