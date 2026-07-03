"""Token management endpoints.

Provides CRUD operations for API tokens.

POST  /api/v1/tokens        — create a new token (returns plaintext once)
GET   /api/v1/tokens        — list all active tokens (no secrets exposed)
DELETE /api/v1/tokens/{id}  — deactivate a token by id

Bootstrap vs. steady-state authz
--------------------------------
``POST`` is unauthenticated ONLY while no active token exists yet in the
database (the bootstrap use-case — minting the very first admin token).
Once at least one active token exists, ``POST`` requires a caller-presented
token with the ``admin`` scope — otherwise any anonymous caller could mint
an admin token scoped to an arbitrary workspace at any time, not just during
bootstrap.

``GET``/``DELETE`` are unchanged from the prior wave (available to any
authenticated-or-bootstrap caller); tightening them to ``admin``-only is
tracked as follow-up work, not bundled into this change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from omniscience_core.auth.audit import audit_token_created, audit_token_deleted
from omniscience_core.auth.middleware import get_current_token
from omniscience_core.auth.scopes import Scope, check_scopes
from omniscience_core.auth.tokens import (
    delete_api_token,
    generate_token,
    hash_token,
)
from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenRead
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/tokens", tags=["tokens"])

_bearer = HTTPBearer(auto_error=False)


class TokenCreateRequest(BaseModel):
    """Payload for minting a new API token."""

    name: str
    scopes: list[str]
    expires_at: datetime | None = None
    workspace_id: uuid.UUID | None = None


class TokenCreateResponse(BaseModel):
    """Response after minting — includes the one-time plaintext secret."""

    token: ApiTokenRead
    secret: str  # shown exactly once; cannot be recovered


def _get_db_factory(request: Request) -> Any:
    """Pull the session factory off app.state, raise 503 if not configured."""
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return factory


def _get_env(request: Request) -> str:
    """Return the deployment environment string from app settings."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return str(settings.environment)
    return "development"


async def _any_active_token_exists(db: AsyncSession) -> bool:
    """True if at least one active token already exists.

    Used to gate the unauthenticated bootstrap path on ``POST`` — once the
    very first token has been minted, subsequent creates must not be
    reachable by anonymous callers.
    """
    result = await db.execute(select(ApiToken).where(ApiToken.is_active.is_(True)))
    return result.scalars().first() is not None


async def _require_admin_for_non_bootstrap(request: Request, factory: Any) -> None:
    """Enforce admin-scoped auth on ``POST /tokens`` once bootstrap is done.

    While the database has zero active tokens, creation stays open
    (bootstrap use-case). As soon as one exists, this requires a valid
    bearer token carrying the ``admin`` scope — otherwise any anonymous
    caller could keep minting arbitrary admin tokens after bootstrap.
    """
    db: AsyncSession
    async with factory() as db:
        bootstrapped = await _any_active_token_exists(db)

    if not bootstrapped:
        return

    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    caller = await get_current_token(request, credentials)
    granted = {Scope(s) for s in caller.scopes if s in Scope.__members__.values()}
    if not check_scopes({Scope.admin}, granted):
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "Insufficient token scopes"},
        )


@router.post("", response_model=TokenCreateResponse, status_code=201)
async def create_token(
    payload: TokenCreateRequest,
    request: Request,
) -> TokenCreateResponse:
    """Mint a new API token.

    The plaintext secret is returned exactly once in the response body.
    It is not stored and cannot be recovered again.

    If ``workspace_id`` is provided it is persisted on the token row,
    which allows the token to satisfy workspace-scoped endpoints (e.g.
    stats, graph retrieval).

    Unauthenticated only while no active token exists yet (bootstrap);
    once any token exists, requires the caller to present an
    ``admin``-scoped token.
    """
    factory = _get_db_factory(request)
    env = _get_env(request)

    await _require_admin_for_non_bootstrap(request, factory)

    if payload.scopes and any(s not in Scope.__members__.values() for s in payload.scopes):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_scope", "message": "Unknown scope in request"},
        )

    plaintext, prefix = generate_token(env)
    hashed = hash_token(plaintext)

    db: AsyncSession
    async with factory() as db:
        token_obj = ApiToken(
            name=payload.name,
            hashed_token=hashed,
            token_prefix=prefix,
            scopes=payload.scopes,
            expires_at=payload.expires_at,
            workspace_id=payload.workspace_id,
        )
        db.add(token_obj)
        await db.flush()
        await db.refresh(token_obj)
        await db.commit()

        read_model = ApiTokenRead.model_validate(token_obj)

    audit_token_created(prefix, payload.scopes)
    log.info("token_created_via_api", token_prefix=prefix, name=payload.name)

    return TokenCreateResponse(token=read_model, secret=plaintext)


@router.get("", response_model=list[ApiTokenRead])
async def list_tokens(request: Request) -> list[ApiTokenRead]:
    """List all active API tokens (secrets never exposed)."""
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        result = await db.execute(select(ApiToken).where(ApiToken.is_active.is_(True)))
        tokens = result.scalars().all()
        return [ApiTokenRead.model_validate(t) for t in tokens]


@router.delete("/{token_id}", status_code=204)
async def delete_token(token_id: uuid.UUID, request: Request) -> None:
    """Deactivate an API token by id."""
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        token_obj = await db.get(ApiToken, token_id)
        if token_obj is None:
            raise HTTPException(status_code=404, detail="Token not found")

        prefix: str = token_obj.token_prefix
        await delete_api_token(db, token_id)
        await db.commit()

    audit_token_deleted(prefix)
    log.info("token_deleted_via_api", token_prefix=prefix)
