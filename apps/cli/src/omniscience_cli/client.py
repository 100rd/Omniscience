"""HTTP client for the Omniscience REST API."""

from __future__ import annotations

import os
from typing import Any, cast

import httpx

DEFAULT_URL = "http://localhost:8000"
ENV_URL = "OMNISCIENCE_URL"
ENV_TOKEN = "OMNISCIENCE_TOKEN"  # noqa: S105 — env var name, not a credential


def _json(r: httpx.Response) -> dict[str, Any]:
    """Cast the untyped httpx ``Response.json()`` return to a typed dict."""
    return cast("dict[str, Any]", r.json())


class OmniscienceClientError(Exception):
    """Raised when the API returns an error response."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _raise_for_error(response: httpx.Response) -> None:
    """Parse error body and raise OmniscienceClientError."""
    if response.is_success:
        return
    try:
        body = response.json()
        err = body.get("error", {})
        code = err.get("code", "unknown")
        message = err.get("message", response.text)
    except Exception:
        code = "unknown"
        message = response.text
    raise OmniscienceClientError(code=code, message=message, status=response.status_code)


class OmniscienceClient:
    """Thin synchronous wrapper around the Omniscience REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get(ENV_URL, DEFAULT_URL)).rstrip("/")
        self.token = token or os.environ.get(ENV_TOKEN, "")
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        resp = self._client.get("/health")
        _raise_for_error(resp)
        return _json(resp)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        sources: list[str] | None = None,
        top_k: int = 10,
        max_age_seconds: int | None = None,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if sources:
            payload["sources"] = sources
        if max_age_seconds is not None:
            payload["max_age_seconds"] = max_age_seconds
        if filters:
            payload["filters"] = filters
        resp = self._client.post("/api/v1/search", json=payload)
        _raise_for_error(resp)
        return _json(resp)

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def list_sources(
        self,
        *,
        source_type: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if source_type:
            params["type"] = source_type
        if status:
            params["status"] = status
        resp = self._client.get("/api/v1/sources", params=params)
        _raise_for_error(resp)
        return _json(resp)

    def create_source(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post("/api/v1/sources", json=body)
        _raise_for_error(resp)
        return _json(resp)

    def get_source(self, source_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/api/v1/sources/{source_id}")
        _raise_for_error(resp)
        return _json(resp)

    def delete_source(self, source_id: str) -> None:
        resp = self._client.delete(f"/api/v1/sources/{source_id}")
        _raise_for_error(resp)

    def validate_source(self, source_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/api/v1/sources/{source_id}/stats")
        _raise_for_error(resp)
        return _json(resp)

    def sync_source(self, source_id: str) -> dict[str, Any]:
        resp = self._client.post(f"/api/v1/sources/{source_id}/sync")
        _raise_for_error(resp)
        return _json(resp)

    def get_ingestion_run(self, run_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/api/v1/ingestion-runs/{run_id}")
        _raise_for_error(resp)
        return _json(resp)

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    def list_tokens(self) -> dict[str, Any]:
        resp = self._client.get("/api/v1/tokens")
        _raise_for_error(resp)
        return _json(resp)

    def create_token(self, name: str, scopes: list[str]) -> dict[str, Any]:
        resp = self._client.post(
            "/api/v1/tokens",
            json={"name": name, "scopes": scopes},
        )
        _raise_for_error(resp)
        return _json(resp)

    def revoke_token(self, token_id: str) -> None:
        resp = self._client.delete(f"/api/v1/tokens/{token_id}")
        _raise_for_error(resp)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OmniscienceClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
