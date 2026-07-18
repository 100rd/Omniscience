"""FastAPI dependencies for token-based authentication and scope enforcement.

Authentication sources (in priority order)
------------------------------------------
1. ``Authorization: Bearer <token>`` header  — CLI / MCP / programmatic clients.
   No CSRF check: Bearer credentials are explicit, not ambient.
2. ``omniscience_admin_session`` httpOnly cookie — admin SPA only.
   State-changing methods (POST/PUT/PATCH/DELETE) additionally require the
   ``X-CSRF-Token`` header to equal the ``csrf_token`` cookie value
   (double-submit cookie pattern, RFC 6265 §8.2.1).

Both paths share the same ``_lookup_token`` helper so token validation logic
(argon2 verify, expiry, active flag) is identical.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_core.auth.scopes import Scope, check_scopes
from omniscience_core.auth.tokens import verify_token
from omniscience_core.db.models import ApiToken

log = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)
# Module-level singleton so FastAPI Depends() calls don't trip ruff B008
_bearer_dep = Depends(_bearer)

_AUTH_ERROR = HTTPException(
    status_code=401,
    detail={"code": "unauthorized", "message": "Token missing or invalid"},
)

_CSRF_ERROR = HTTPException(
    status_code=403,
    detail={"code": "csrf_invalid", "message": "CSRF token missing or invalid"},
)

_PREFIX_LEN = 8
_SESSION_COOKIE = "omniscience_admin_session"
_CSRF_COOKIE = "csrf_token"
_CSRF_HEADER = "x-csrf-token"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


async def _lookup_token(session: AsyncSession, plaintext: str) -> ApiToken | None:
    """Find an active, non-expired token whose prefix matches and hash verifies."""
    if len(plaintext) < _PREFIX_LEN:
        return None
    prefix = plaintext[:_PREFIX_LEN]

    result = await session.execute(
        select(ApiToken).where(
            ApiToken.token_prefix == prefix,
            ApiToken.is_active.is_(True),
        )
    )
    candidates = result.scalars().all()

    for candidate in candidates:
        if not verify_token(plaintext, candidate.hashed_token):
            continue
        if candidate.expires_at and candidate.expires_at.replace(tzinfo=UTC) < _utc_now():
            return None
        return candidate

    return None


async def _update_last_used(session: AsyncSession, token: ApiToken) -> None:
    """Update last_used_at on the token row (best-effort)."""
    try:
        token.last_used_at = _utc_now()
        await session.flush()
    except Exception:
        log.warning("last_used_at_update_failed", token_prefix=token.token_prefix)


def _verify_csrf(request: Request) -> None:
    """Enforce double-submit CSRF for cookie-authenticated mutating requests.

    Compares ``X-CSRF-Token`` header against the ``csrf_token`` cookie using
    a constant-time comparison so timing attacks cannot distinguish missing
    vs wrong values.

    Raises HTTPException 403 when CSRF verification fails.
    """
    if request.method not in _MUTATING_METHODS:
        return

    cookie_val = request.cookies.get(_CSRF_COOKIE, "")
    header_val = request.headers.get(_CSRF_HEADER, "")

    # Both must be present and non-empty before we compare.
    if not cookie_val or not header_val:
        raise _CSRF_ERROR

    # hmac.compare_digest requires equal-length bytes for a meaningful
    # constant-time comparison; secrets.compare_digest is its alias.
    if not secrets.compare_digest(
        cookie_val.encode("utf-8"),
        header_val.encode("utf-8"),
    ):
        raise _CSRF_ERROR


def _extract_plaintext(
    credentials: HTTPAuthorizationCredentials | None,
    request: Request,
) -> tuple[str | None, bool]:
    """Return ``(plaintext, is_bearer)`` from whichever auth source is present.

    Priority: Bearer header > session cookie.
    Returns ``(None, False)`` when neither is present.
    """
    if credentials is not None and credentials.credentials:
        return credentials.credentials, True

    cookie_val = request.cookies.get(_SESSION_COOKIE)
    if cookie_val:
        return cookie_val, False

    return None, False


async def get_current_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _bearer_dep,
) -> ApiToken:
    """FastAPI dependency: validate Bearer header or admin session cookie.

    When the session cookie is used for a mutating request, CSRF is enforced
    via the double-submit pattern before the token lookup runs.

    Raises:
        HTTPException 401 — token missing, invalid, or expired.
        HTTPException 403 — CSRF token missing or invalid (cookie auth only).

    Returns:
        The authenticated ApiToken ORM instance.
    """
    plaintext, is_bearer = _extract_plaintext(credentials, request)

    if plaintext is None:
        raise _AUTH_ERROR

    if not is_bearer:
        _verify_csrf(request)

    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise _AUTH_ERROR

    async with factory() as db:
        token = await _lookup_token(db, plaintext)
        if token is None:
            raise _AUTH_ERROR

        await _update_last_used(db, token)
        await db.commit()

        # Stash the resolved token on request.state so downstream middleware
        # (e.g. TelemetryMiddleware in apps/server, issue #113) can attribute
        # the response to a token without re-running the lookup.
        request.state.api_token = token
        return token


async def get_optional_current_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _bearer_dep,
) -> ApiToken | None:
    """Resolve optional auth while rejecting any supplied invalid credential.

    This is used only by one-way bootstrap endpoints: absence is meaningful,
    but a malformed, expired, or revoked credential is never treated as absent.
    """
    plaintext, is_bearer = _extract_plaintext(credentials, request)
    if plaintext is None:
        return None
    if not is_bearer:
        _verify_csrf(request)
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise _AUTH_ERROR
    async with factory() as db:
        token = await _lookup_token(db, plaintext)
        if token is None:
            raise _AUTH_ERROR
        await _update_last_used(db, token)
        await db.commit()
        request.state.api_token = token
        return token


# Module-level singleton for use inside require_scope closure
_current_token_dep: Any = Depends(get_current_token)


def require_scope(
    *scopes: Scope,
) -> Callable[[ApiToken], Coroutine[Any, Any, ApiToken]]:
    """Return a FastAPI dependency that enforces one or more scopes.

    Usage::

        @router.get("/sources", dependencies=[Depends(require_scope(Scope.sources_read))])
        async def list_sources() -> ...: ...

    Raises:
        HTTPException 403 — token lacks required scope.
    """
    required = set(scopes)

    async def _check(token: ApiToken = _current_token_dep) -> ApiToken:
        granted = {Scope(s) for s in token.scopes if s in Scope.__members__.values()}
        if not check_scopes(required, granted):
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden", "message": "Insufficient token scopes"},
            )
        return token

    return _check


__all__ = [
    "_lookup_token",
    "get_current_token",
    "get_optional_current_token",
    "require_scope",
]
