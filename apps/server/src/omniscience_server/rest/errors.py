"""Standard error responses and exception handlers for the REST API.

Error format:
  {"error": {"code": "...", "message": "...", "details": {}}}

HTTP status codes map to error codes:
  401 -> unauthorized
  403 -> forbidden
  404 -> *_not_found
  429 -> rate_limited
  500 -> internal
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """Inner error object."""

    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    """Top-level error envelope."""

    error: ErrorDetail


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a spec-compliant JSONResponse for an error."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


def _401_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = detail.get("code", "unauthorized")
        message = detail.get("message", "Token missing or invalid")
    else:
        code = "unauthorized"
        message = str(detail) if detail else "Token missing or invalid"
    return error_response(401, code, message)


def _403_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = detail.get("code", "forbidden")
        message = detail.get("message", "Insufficient permissions")
    else:
        code = "forbidden"
        message = str(detail) if detail else "Insufficient permissions"
    return error_response(403, code, message)


def _404_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = detail.get("code", "not_found")
        message = detail.get("message", "Resource not found")
    else:
        code = "not_found"
        message = str(detail) if detail else "Resource not found"
    return error_response(404, code, message)


def _429_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = getattr(exc, "detail", None)
    retry_after: str | None = None
    if isinstance(detail, dict):
        code = detail.get("code", "rate_limited")
        message = detail.get("message", "Rate limit exceeded")
        retry_after = detail.get("retry_after")
    else:
        code = "rate_limited"
        message = str(detail) if detail else "Rate limit exceeded"

    response = error_response(429, code, message)
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return response


def _500_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(500, "internal", "An internal server error occurred")


def _http_exc_handler(request: Request, exc: Exception) -> JSONResponse:
    """Wrap an arbitrary HTTPException in the spec-compliant error envelope.

    Preserves the original status code and extracts code/message from the
    detail dict if available.  Used for 5xx non-500 responses (e.g. 503).
    """
    status_code: int = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = detail.get("code", "error")
        message = detail.get("message", "An error occurred")
    else:
        code = "error"
        message = str(detail) if detail else "An error occurred"
    return error_response(status_code, code, message)


def register_error_handlers(app: FastAPI) -> None:
    """Register all REST API exception handlers on the given FastAPI app."""
    from fastapi.exceptions import HTTPException

    # The handlers must match on HTTPException by status code
    # We register a catch-all HTTPException handler that dispatches by status code
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 401:
            return _401_handler(request, exc)
        if exc.status_code == 403:
            return _403_handler(request, exc)
        if exc.status_code == 404:
            return _404_handler(request, exc)
        if exc.status_code == 429:
            return _429_handler(request, exc)
        if exc.status_code == 500:
            return _500_handler(request, exc)
        # All other status codes (including 503, 422, etc.) — preserve status code
        return _http_exc_handler(request, exc)

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return _500_handler(request, exc)


__all__ = [
    "ErrorDetail",
    "ErrorResponse",
    "error_response",
    "register_error_handlers",
]
