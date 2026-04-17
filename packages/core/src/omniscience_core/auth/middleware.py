"""FastAPI dependencies for token-based authentication and scope enforcement."""

from __future__ import annotations

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

_PREFIX_LEN = 8


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


async def get_current_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = _bearer_dep,
) -> ApiToken:
    """FastAPI dependency: extract and validate the Bearer token.

    Raises:
        HTTPException 401 — token missing, invalid, or expired.

    Returns:
        The authenticated ApiToken ORM instance.
    """
    if credentials is None or not credentials.credentials:
        raise _AUTH_ERROR

    plaintext = credentials.credentials

    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise _AUTH_ERROR

    async with factory() as db:
        token = await _lookup_token(db, plaintext)
        if token is None:
            raise _AUTH_ERROR

        await _update_last_used(db, token)
        await db.commit()

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


__all__ = ["get_current_token", "require_scope"]
