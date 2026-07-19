"""Token management endpoints.

Provides CRUD operations for API tokens.  POST permits one unauthenticated
bootstrap request only while the token table is empty, and that request must
mint exactly an admin-scoped token.  After bootstrap, POST, GET, and DELETE
require an active admin-scoped token.

POST  /api/v1/tokens        — create a new token (returns plaintext once)
GET   /api/v1/tokens        — list all active tokens (no secrets exposed)
DELETE /api/v1/tokens/{id}  — deactivate a token by id
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.audit import (
    audit_mcp_token_created,
    audit_mcp_token_revoked,
    audit_mcp_token_rotated,
    audit_token_created,
    audit_token_deleted,
)
from omniscience_core.auth.middleware import get_optional_current_token, require_scope
from omniscience_core.auth.scopes import Scope, check_scopes
from omniscience_core.auth.tokens import (
    MCP_READ_PROFILE_ID,
    McpReadTokenValidationError,
    create_mcp_read_token,
    delete_api_token,
    generate_token,
    hash_token,
    revoke_mcp_read_token,
    rotate_mcp_read_token,
)
from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenRead
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/tokens", tags=["tokens"])
_admin_dep = Depends(require_scope(Scope.admin))
_optional_token_dep = Depends(get_optional_current_token)

# Arbitrary but fixed Postgres advisory-lock key used to serialize concurrent
# admin-token bootstrap attempts. Under READ COMMITTED, N concurrent
# unauthenticated requests hitting an empty api_tokens table would otherwise
# all observe bootstrap_open=True and each mint an admin token; taking this
# xact-lock before the emptiness check forces them to run one at a time, so
# only the first can see an empty table (the lock auto-releases on
# commit/rollback). The value carries no meaning beyond being a stable,
# collision-unlikely mutex key within this process's advisory-lock keyspace.
_BOOTSTRAP_ADVISORY_LOCK_KEY = 350_100_350_100


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


class McpReadTokenCreateRequest(BaseModel):
    """Admin request for a workspace-bound MCP v1 read token."""

    name: str
    workspace_id: uuid.UUID
    expires_at: datetime | None = None


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
    current_token: ApiToken | None = _optional_token_dep,
) -> TokenCreateResponse:
    """Mint a new API token.

    The plaintext secret is returned exactly once in the response body.
    It is not stored and cannot be recovered again.

    If ``workspace_id`` is provided it is persisted on the token row,
    which allows the token to satisfy workspace-scoped endpoints (e.g.
    stats, graph retrieval).
    """
    factory = _get_db_factory(request)
    env = _get_env(request)

    db: AsyncSession
    async with factory() as db:
        if current_token is None:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _BOOTSTRAP_ADVISORY_LOCK_KEY},
            )
            any_token = await db.execute(select(ApiToken.id).limit(1))
            bootstrap_open = any_token.scalars().first() is None
            if not bootstrap_open:
                raise HTTPException(status_code=401, detail="Admin token required")
            if payload.scopes != ["admin"]:
                raise HTTPException(
                    status_code=403,
                    detail="Bootstrap requires exact admin scope",
                )
        else:
            granted = {
                Scope(scope)
                for scope in current_token.scopes
                if scope in Scope.__members__.values()
            }
            if not check_scopes({Scope.admin}, granted):
                raise HTTPException(status_code=403, detail="Admin token required")
        plaintext, prefix = generate_token(env)
        hashed = hash_token(plaintext)
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


@router.post(
    "/profiles/omniscience-mcp-read-v1",
    response_model=TokenCreateResponse,
    status_code=201,
)
async def create_mcp_read_profile_token(
    payload: McpReadTokenCreateRequest,
    request: Request,
    admin_token: ApiToken = _admin_dep,
) -> TokenCreateResponse:
    """Mint the exact bounded MCP read profile; admin authorization required."""
    factory = _get_db_factory(request)
    async with factory() as db:
        try:
            read_model, plaintext = await create_mcp_read_token(
                db,
                name=payload.name,
                workspace_id=payload.workspace_id,
                env=_get_env(request),
                expires_at=payload.expires_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await db.commit()
    if read_model.expires_at is None:
        raise RuntimeError("MCP read profile must have a finite expiry")
    audit_mcp_token_created(
        str(read_model.id),
        read_model.token_prefix,
        str(payload.workspace_id),
        expires_at=read_model.expires_at.isoformat(),
        actor_id=str(admin_token.id),
        actor_prefix=admin_token.token_prefix,
    )
    return TokenCreateResponse(token=read_model, secret=plaintext)


@router.post(
    "/profiles/omniscience-mcp-read-v1/{token_id}/rotate",
    response_model=TokenCreateResponse,
    status_code=201,
)
async def rotate_mcp_read_profile_token(
    token_id: uuid.UUID,
    request: Request,
    admin_token: ApiToken = _admin_dep,
) -> TokenCreateResponse:
    """Rotate an MCP read token with at most 24 hours of old-token overlap."""
    factory = _get_db_factory(request)
    async with factory() as db:
        try:
            (
                read_model,
                plaintext,
                old_prefix,
                old_expiry,
                overlap_seconds,
            ) = await rotate_mcp_read_token(
                db,
                token_id=token_id,
                env=_get_env(request),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Token not found") from exc
        except McpReadTokenValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await db.commit()
    if read_model.expires_at is None:
        raise RuntimeError("MCP read profile must have a finite expiry")
    audit_mcp_token_rotated(
        str(token_id),
        old_prefix,
        str(read_model.id),
        read_model.token_prefix,
        workspace_id=str(read_model.workspace_id),
        old_expires_at=old_expiry.isoformat(),
        new_expires_at=read_model.expires_at.isoformat(),
        overlap_seconds=overlap_seconds,
        actor_id=str(admin_token.id),
        actor_prefix=admin_token.token_prefix,
    )
    return TokenCreateResponse(token=read_model, secret=plaintext)


@router.delete(
    "/profiles/omniscience-mcp-read-v1/{token_id}",
    status_code=204,
)
async def revoke_mcp_read_profile_token(
    token_id: uuid.UUID,
    request: Request,
    admin_token: ApiToken = _admin_dep,
) -> None:
    """Revoke a read-profile token immediately; admin authorization required."""
    factory = _get_db_factory(request)
    async with factory() as db:
        try:
            prefix, workspace_id = await revoke_mcp_read_token(db, token_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Token not found") from exc
        except McpReadTokenValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await db.commit()
    audit_mcp_token_revoked(
        str(token_id),
        prefix,
        workspace_id=str(workspace_id),
        actor_id=str(admin_token.id),
        actor_prefix=admin_token.token_prefix,
    )


@router.get("", response_model=list[ApiTokenRead])
async def list_tokens(
    request: Request,
    _admin_token: ApiToken = _admin_dep,
) -> list[ApiTokenRead]:
    """List all active API tokens (secrets never exposed)."""
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        result = await db.execute(select(ApiToken).where(ApiToken.is_active.is_(True)))
        tokens = result.scalars().all()
        return [ApiTokenRead.model_validate(t) for t in tokens]


@router.delete("/{token_id}", status_code=204)
async def delete_token(
    token_id: uuid.UUID,
    request: Request,
    _admin_token: ApiToken = _admin_dep,
) -> None:
    """Deactivate an API token by id."""
    factory = _get_db_factory(request)

    db: AsyncSession
    async with factory() as db:
        token_obj = await db.get(ApiToken, token_id)
        if token_obj is None:
            raise HTTPException(status_code=404, detail="Token not found")
        if token_obj.profile_id == MCP_READ_PROFILE_ID:
            raise HTTPException(
                status_code=409,
                detail="Use the admin-authenticated MCP profile revoke endpoint",
            )

        prefix: str = token_obj.token_prefix
        await delete_api_token(db, token_id)
        await db.commit()

    audit_token_deleted(prefix)
    log.info("token_deleted_via_api", token_prefix=prefix)
