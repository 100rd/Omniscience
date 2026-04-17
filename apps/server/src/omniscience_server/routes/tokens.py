"""Token management endpoints.

Provides CRUD operations for API tokens.  These routes are intentionally
unprotected on POST (bootstrap use-case — minting the very first admin token).
List and Delete require an active admin-scoped session in future waves; for
the current wave they are available to any authenticated caller (or
unauthenticated during bootstrap) to keep the implementation self-contained.

POST  /api/v1/tokens        — create a new token (returns plaintext once)
GET   /api/v1/tokens        — list all active tokens (no secrets exposed)
DELETE /api/v1/tokens/{id}  — deactivate a token by id
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from omniscience_core.auth.audit import audit_token_created, audit_token_deleted
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


class TokenCreateRequest(BaseModel):
    """Payload for minting a new API token."""

    name: str
    scopes: list[str]
    expires_at: datetime | None = None


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


@router.post("", response_model=TokenCreateResponse, status_code=201)
async def create_token(
    payload: TokenCreateRequest,
    request: Request,
) -> TokenCreateResponse:
    """Mint a new API token.

    The plaintext secret is returned exactly once in the response body.
    It is not stored and cannot be recovered again.
    """
    factory = _get_db_factory(request)
    env = _get_env(request)

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
