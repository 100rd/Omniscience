"""Exceptions raised by the Omniscience client."""

from __future__ import annotations


class OmniscienceError(Exception):
    """Base exception for all Omniscience client errors."""


class AuthenticationError(OmniscienceError):
    """Raised when the server returns 401 Unauthorized."""

    def __init__(self, message: str = "Invalid or missing token") -> None:
        super().__init__(message)


class PermissionError(OmniscienceError):  # noqa: A001
    """Raised when the server returns 403 Forbidden."""

    def __init__(self, message: str = "Token lacks required scope") -> None:
        super().__init__(message)


class NotFoundError(OmniscienceError):
    """Raised when the server returns 404 Not Found."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"Not found: {resource}")


class RateLimitError(OmniscienceError):
    """Raised when the server returns 429 Too Many Requests."""

    def __init__(self, message: str = "Rate limit exceeded") -> None:
        super().__init__(message)


class ServerError(OmniscienceError):
    """Raised when the server returns 5xx."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Server error {status_code}: {message}")


class APIError(OmniscienceError):
    """Raised for unexpected HTTP errors not covered by more specific types."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"API error {status_code}: {message}")


__all__ = [
    "APIError",
    "AuthenticationError",
    "NotFoundError",
    "OmniscienceError",
    "PermissionError",
    "RateLimitError",
    "ServerError",
]
