"""Admin session endpoints — httpOnly cookie-based auth for the admin SPA.

POST   /admin/session  — validate a scoped API token, set session+CSRF cookies
DELETE /admin/session  — clear both cookies (logout)

Design decision: the session cookie carries the validated plaintext token
directly (no server-side session store exists in this deployment).  This is
documented explicitly below.  The CSRF token is an opaque random value stored
in a *non*-httpOnly cookie so JavaScript can read and forward it as a header
(double-submit cookie pattern).

Decision note for reviewers
----------------------------
The ``omniscience_admin_session`` cookie value equals the plaintext API token.
A server-side session store (e.g. Redis) would let us issue opaque IDs and
revoke sessions independently of the token, but no session store is wired in
this service.  The trade-off:

  - Pro (current): zero new infrastructure; revocation is token-level (delete
    the ApiToken row); validated on every request via argon2 (same as Bearer).
  - Con: cookie size is ~80 chars; cannot invalidate one browser session
    without revoking the token globally.

If a session store is added later, swap ``_set_session_cookies`` to store an
opaque ID and add a session lookup step in ``get_current_token_from_cookie``.
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from omniscience_core.auth.middleware import _lookup_token
from omniscience_core.auth.scopes import Scope, check_scopes
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-session"])

_SESSION_COOKIE = "omniscience_admin_session"
_CSRF_COOKIE = "csrf_token"
_CSRF_BYTES = 32
_COOKIE_MAX_AGE = 86_400 * 7  # 7 days


class _SessionCreateRequest(BaseModel):
    token: str

    @field_validator("token")
    @classmethod
    def _token_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("token must not be empty")
        return v


def _require_db_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return factory


def _set_session_cookies(
    response: Response,
    plaintext: str,
    csrf_token: str,
    is_secure: bool,
) -> None:
    """Attach the session and CSRF cookies to *response*."""
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=plaintext,
        httponly=True,
        secure=is_secure,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
        path="/",
    )
    # CSRF cookie is intentionally NOT httpOnly — JS must read it.
    response.set_cookie(
        key=_CSRF_COOKIE,
        value=csrf_token,
        httponly=False,
        secure=is_secure,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
        path="/",
    )


def _clear_session_cookies(response: Response, is_secure: bool) -> None:
    """Expire both cookies immediately."""
    for name in (_SESSION_COOKIE, _CSRF_COOKIE):
        response.set_cookie(
            key=name,
            value="",
            httponly=(name == _SESSION_COOKIE),
            secure=is_secure,
            samesite="strict",
            max_age=0,
            path="/",
        )


def _is_secure(request: Request) -> bool:
    """Return True when the request arrived over HTTPS or in production."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        env = str(getattr(settings, "environment", "")).lower()
        if env not in ("development", "dev", "test"):
            return True
    return request.url.scheme == "https"


@router.post("/session", status_code=204)
async def create_session(
    payload: _SessionCreateRequest,
    request: Request,
    response: Response,
) -> None:
    """Validate *token* and set httpOnly session + CSRF cookies.

    Raises 401 when the token is missing, invalid, expired, or lacks
    ``admin`` scope.
    """
    factory = _require_db_factory(request)

    async with factory() as db:
        api_token = await _lookup_token(db, payload.token)

    if api_token is None:
        log.warning("admin_session_create_rejected", reason="invalid_token")
        raise HTTPException(status_code=401, detail="Token invalid or expired")

    granted = {Scope(s) for s in api_token.scopes if s in Scope.__members__.values()}
    if not check_scopes({Scope.admin}, granted):
        log.warning(
            "admin_session_create_rejected",
            reason="insufficient_scope",
            token_prefix=api_token.token_prefix,
        )
        raise HTTPException(status_code=403, detail="Token lacks admin scope")

    csrf_token = secrets.token_urlsafe(_CSRF_BYTES)
    _set_session_cookies(response, payload.token, csrf_token, _is_secure(request))
    log.info("admin_session_created", token_prefix=api_token.token_prefix)


@router.delete("/session", status_code=204)
async def delete_session(request: Request, response: Response) -> None:
    """Clear both session cookies, effectively logging the user out."""
    _clear_session_cookies(response, _is_secure(request))
    log.info("admin_session_deleted")
