"""Async REST client for the Omniscience API."""

from __future__ import annotations

from typing import Any

import httpx

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

_DEFAULT_TIMEOUT = 30.0


def _raise_for_status(response: httpx.Response) -> None:
    """Map HTTP error codes to typed exceptions."""
    if response.is_success:
        return

    try:
        detail = response.json()
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("detail") or str(detail)
        else:
            message = str(detail)
    except Exception:
        message = response.text or response.reason_phrase or "unknown error"

    status = response.status_code
    if status == 401:
        raise AuthenticationError(message)
    if status == 403:
        raise OmnisciencePermissionError(message)
    if status == 404:
        raise NotFoundError(message)
    if status == 429:
        raise RateLimitError(message)
    if status >= 500:
        raise ServerError(status, message)
    raise APIError(status, message)


class OmniscienceClient:
    """Async HTTP client for the Omniscience REST API.

    Usage::

        async with OmniscienceClient(base_url="http://localhost:8000", token="omni_...") as client:
            result = await client.search("what is retrieval augmented generation?")
            for hit in result.hits:
                print(hit.score, hit.text[:120])

    The client wraps ``httpx.AsyncClient`` and raises typed exceptions for
    all non-2xx responses.  Call ``await client.close()`` (or use it as an
    async context manager) to release the underlying connection pool.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        token: str | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
    ) -> None:
        merged_headers: dict[str, str] = {"Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        if token:
            merged_headers["Authorization"] = f"Bearer {token}"

        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers=merged_headers,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OmniscienceClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> Any:
        filtered = {k: v for k, v in params.items() if v is not None}
        response = await self._http.get(path, params=filtered)
        _raise_for_status(response)
        return response.json()

    async def _post(self, path: str, json: Any = None) -> Any:
        response = await self._http.post(path, json=json)
        _raise_for_status(response)
        return response.json()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 10,
        *,
        sources: list[str] | None = None,
        types: list[str] | None = None,
        max_age_seconds: int | None = None,
        filters: dict[str, Any] | None = None,
        include_tombstoned: bool = False,
        retrieval_strategy: str = "hybrid",
    ) -> SearchResult:
        """Execute a hybrid vector + keyword search.

        Args:
            query: Free-text search query.
            top_k: Maximum number of hits to return (1-500).
            sources: Restrict to specific source IDs or names.
            types: Restrict to specific document types.
            max_age_seconds: Only return documents indexed within this window.
            filters: Arbitrary metadata key/value filters.
            include_tombstoned: Include soft-deleted documents.
            retrieval_strategy: One of ``hybrid``, ``keyword``, ``structural``,
                ``auto``.

        Returns:
            A :class:`~omniscience_client.types.SearchResult` with ranked hits.

        Raises:
            AuthenticationError: Token is missing or invalid.
            PermissionError: Token lacks the ``search`` scope.
            ServerError: The server encountered an internal error.
        """
        body: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "include_tombstoned": include_tombstoned,
            "retrieval_strategy": retrieval_strategy,
        }
        if sources is not None:
            body["sources"] = sources
        if types is not None:
            body["types"] = types
        if max_age_seconds is not None:
            body["max_age_seconds"] = max_age_seconds
        if filters is not None:
            body["filters"] = filters

        data = await self._post("/api/v1/search", json=body)
        return SearchResult.model_validate(data)

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    async def list_sources(
        self,
        *,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[Source]:
        """List all configured ingestion sources.

        Args:
            source_type: Filter by source type (e.g. ``github``, ``notion``).
            status: Filter by source status (e.g. ``active``, ``paused``).

        Returns:
            List of :class:`~omniscience_client.types.Source` objects.

        Raises:
            AuthenticationError: Token is missing or invalid.
            PermissionError: Token lacks ``sources:read`` scope.
        """
        data = await self._get("/api/v1/sources", source_type=source_type, status=status)
        return [Source.model_validate(s) for s in data]

    async def create_source(
        self,
        type: str,  # noqa: A002
        name: str,
        config: dict[str, Any] | None = None,
        *,
        secrets_ref: str | None = None,
        status: str = "active",
        freshness_sla_seconds: int | None = None,
        tenant_id: str | None = None,
    ) -> Source:
        """Create a new ingestion source.

        Args:
            type: Source type identifier (e.g. ``github``, ``confluence``).
            name: Human-readable display name.
            config: Type-specific configuration dict.
            secrets_ref: Optional reference to a secrets store entry.
            status: Initial status — defaults to ``active``.
            freshness_sla_seconds: Alert threshold for stale data.
            tenant_id: Optional tenant UUID for multi-tenant deployments.

        Returns:
            The newly created :class:`~omniscience_client.types.Source`.

        Raises:
            AuthenticationError: Token is missing or invalid.
            PermissionError: Token lacks ``sources:write`` scope.
        """
        body: dict[str, Any] = {
            "type": type,
            "name": name,
            "config": config or {},
            "status": status,
        }
        if secrets_ref is not None:
            body["secrets_ref"] = secrets_ref
        if freshness_sla_seconds is not None:
            body["freshness_sla_seconds"] = freshness_sla_seconds
        if tenant_id is not None:
            body["tenant_id"] = tenant_id

        data = await self._post("/api/v1/sources", json=body)
        return Source.model_validate(data)

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    async def get_document(self, document_id: str) -> DocumentWithChunks:
        """Retrieve a document and all its chunks by ID.

        Args:
            document_id: UUID of the document to fetch.

        Returns:
            A :class:`~omniscience_client.types.DocumentWithChunks` instance.

        Raises:
            NotFoundError: No document with that ID exists.
            AuthenticationError: Token is missing or invalid.
            PermissionError: Token lacks ``search`` scope.
        """
        data = await self._get(f"/api/v1/documents/{document_id}")
        return DocumentWithChunks.model_validate(data)

    # ------------------------------------------------------------------
    # Ingestion runs
    # ------------------------------------------------------------------

    async def list_ingestion_runs(
        self,
        *,
        source_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[IngestionRun]:
        """List recent ingestion runs, newest first.

        Args:
            source_id: Restrict to runs for a specific source UUID.
            status: Filter by run status (e.g. ``running``, ``success``, ``failed``).
            limit: Maximum number of results (1-200, default 50).

        Returns:
            List of :class:`~omniscience_client.types.IngestionRun` objects.

        Raises:
            AuthenticationError: Token is missing or invalid.
            PermissionError: Token lacks ``sources:read`` scope.
        """
        data = await self._get(
            "/api/v1/ingestion-runs",
            source_id=source_id,
            status=status,
            limit=limit,
        )
        return [IngestionRun.model_validate(r) for r in data]

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    async def create_token(
        self,
        name: str,
        scopes: list[str],
        *,
        expires_at: str | None = None,
    ) -> TokenCreateResponse:
        """Mint a new API token.

        The ``secret`` field in the response is shown exactly once and
        cannot be recovered.  Store it securely immediately.

        Args:
            name: Human-readable label for this token.
            scopes: List of scope strings (e.g. ``["search", "sources:read"]``).
            expires_at: Optional ISO-8601 expiry datetime string.

        Returns:
            A :class:`~omniscience_client.types.TokenCreateResponse` with the
            token metadata and the one-time plaintext secret.

        Raises:
            AuthenticationError: Token is missing or invalid.
            ServerError: The server encountered an internal error.
        """
        body: dict[str, Any] = {"name": name, "scopes": scopes}
        if expires_at is not None:
            body["expires_at"] = expires_at

        data = await self._post("/api/v1/tokens", json=body)
        return TokenCreateResponse.model_validate(data)


__all__ = ["OmniscienceClient"]
