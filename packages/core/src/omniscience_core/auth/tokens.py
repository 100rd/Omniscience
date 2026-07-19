"""API token generation, hashing, and database lifecycle management."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenCreate, ApiTokenRead

log = structlog.get_logger(__name__)

_ph = PasswordHasher()

# Token format: sk_{env}_{uuid}_{random32}
# prefix = first 8 chars of the full token string
_PREFIX_LEN = 8
_RANDOM_BYTES = 24  # produces ~32 url-safe base64 chars

MCP_READ_PROFILE_ID = "omniscience-mcp-read-v1"
MCP_READ_MAX_LIFETIME = timedelta(days=30)
MCP_READ_ROTATION_OVERLAP = timedelta(hours=24)


class McpReadTokenValidationError(PermissionError):
    """A stable rejection reason for a non-conforming MCP v1 token."""


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_mcp_read_token(
    token: Any,
    *,
    now: datetime | None = None,
) -> uuid.UUID:
    """Validate the exact workspace-bound bootstrap profile for MCP v1."""
    checked_at = _aware_utc(now or datetime.now(UTC))
    if getattr(token, "profile_id", None) != MCP_READ_PROFILE_ID:
        raise McpReadTokenValidationError("legacy_token")
    if getattr(token, "scopes", None) != ["search"]:
        raise McpReadTokenValidationError("exact_search_scope_required")
    workspace_id = getattr(token, "workspace_id", None)
    if not isinstance(workspace_id, uuid.UUID):
        raise McpReadTokenValidationError("workspace_required")
    if not bool(getattr(token, "is_active", False)):
        raise McpReadTokenValidationError("token_revoked")
    expires_at = getattr(token, "expires_at", None)
    if not isinstance(expires_at, datetime):
        raise McpReadTokenValidationError("expiry_required")
    expires_at = _aware_utc(expires_at)
    if expires_at <= checked_at:
        raise McpReadTokenValidationError("token_expired")
    created_at = getattr(token, "created_at", None)
    if not isinstance(created_at, datetime):
        raise McpReadTokenValidationError("creation_provenance_required")
    if expires_at > _aware_utc(created_at) + MCP_READ_MAX_LIFETIME:
        raise McpReadTokenValidationError("expiry_exceeds_30_days")
    return workspace_id


def rotation_overlap_expiry(now: datetime, current_expiry: datetime) -> datetime:
    """Return the old token expiry during rotation, capped at 24 hours."""
    checked_at = _aware_utc(now)
    return min(_aware_utc(current_expiry), checked_at + MCP_READ_ROTATION_OVERLAP)


def generate_token(env: str) -> tuple[str, str]:
    """Generate a new plaintext API token.

    Args:
        env: Deployment environment (e.g. "development", "staging", "production").

    Returns:
        A (plaintext, prefix) tuple where prefix is the first 8 characters
        of the token — safe to store and display in logs.
    """
    env_tag = env[:3].lower()
    token_id = uuid.uuid4().hex
    random_part = secrets.token_urlsafe(_RANDOM_BYTES)
    plaintext = f"sk_{env_tag}_{token_id}_{random_part}"
    prefix = plaintext[:_PREFIX_LEN]
    return plaintext, prefix


def hash_token(plaintext: str) -> str:
    """Return the argon2 hash of *plaintext*.

    The plaintext is never stored or logged — only the hash is persisted.
    """
    return _ph.hash(plaintext)


def verify_token(plaintext: str, hashed: str) -> bool:
    """Return True if *plaintext* matches *hashed*.

    Uses argon2's constant-time comparison; logs nothing about the plaintext.
    Returns False on any verification failure (mismatch or corrupted hash).
    """
    try:
        return _ph.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError):
        return False


async def create_api_token(
    session: AsyncSession,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
    workspace_id: uuid.UUID | None = None,
) -> tuple[ApiTokenRead, str]:
    """Create a new API token, persist it, and return the read model + plaintext.

    The plaintext token is returned exactly once — it cannot be recovered from
    the stored hash.  The caller MUST surface it to the user immediately.

    Args:
        session:      Active async SQLAlchemy session.
        name:         Human-readable label for this token.
        scopes:       List of scope strings (e.g. ["search", "sources:read"]).
        expires_at:   Optional expiry datetime (UTC).
        workspace_id: Optional workspace UUID to scope this token.

    Returns:
        (ApiTokenRead, plaintext) — read model for the new token and the
        one-time plaintext secret.
    """
    env = "development"
    plaintext, prefix = generate_token(env)
    hashed = hash_token(plaintext)

    create_schema = ApiTokenCreate(
        name=name,
        hashed_token=hashed,
        token_prefix=prefix,
        scopes=scopes,
        expires_at=expires_at,
        workspace_id=workspace_id,
    )

    token_obj = ApiToken(
        name=create_schema.name,
        hashed_token=create_schema.hashed_token,
        token_prefix=create_schema.token_prefix,
        scopes=create_schema.scopes,
        expires_at=create_schema.expires_at,
        workspace_id=create_schema.workspace_id,
    )
    session.add(token_obj)
    await session.flush()
    await session.refresh(token_obj)

    read_model = ApiTokenRead.model_validate(token_obj)
    log.info(
        "token_created",
        token_prefix=prefix,
        name=name,
        scopes=scopes,
        workspace_id=str(workspace_id) if workspace_id else None,
    )
    return read_model, plaintext


async def create_mcp_read_token(
    session: AsyncSession,
    *,
    name: str,
    workspace_id: uuid.UUID,
    env: str = "development",
    expires_at: datetime | None = None,
    rotated_from_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[ApiTokenRead, str]:
    """Mint and persist an exact ``omniscience-mcp-read-v1`` token."""
    created_at = _aware_utc(now or datetime.now(UTC))
    expiry = _aware_utc(expires_at or (created_at + MCP_READ_MAX_LIFETIME))
    if expiry <= created_at:
        raise ValueError("expiry_must_be_future")
    if expiry > created_at + MCP_READ_MAX_LIFETIME:
        raise ValueError("expiry_exceeds_30_days")

    plaintext, prefix = generate_token(env)
    token_obj = ApiToken(
        name=name,
        hashed_token=hash_token(plaintext),
        token_prefix=prefix,
        scopes=["search"],
        profile_id=MCP_READ_PROFILE_ID,
        rotated_from_id=rotated_from_id,
        workspace_id=workspace_id,
        created_at=created_at,
        expires_at=expiry,
    )
    session.add(token_obj)
    await session.flush()
    await session.refresh(token_obj)
    return ApiTokenRead.model_validate(token_obj), plaintext


async def rotate_mcp_read_token(
    session: AsyncSession,
    *,
    token_id: uuid.UUID,
    env: str = "development",
    now: datetime | None = None,
) -> tuple[ApiTokenRead, str, str, datetime, int]:
    """Mint a successor and retain the old token for at most 24 hours."""
    rotated_at = _aware_utc(now or datetime.now(UTC))
    old = await session.get(ApiToken, token_id, with_for_update=True)
    if old is None:
        raise LookupError("token_not_found")
    workspace_id = validate_mcp_read_token(old, now=rotated_at)
    successor_id = await session.scalar(
        select(ApiToken.id).where(ApiToken.rotated_from_id == token_id).limit(1)
    )
    if successor_id is not None:
        raise McpReadTokenValidationError("token_already_rotated")
    assert old.expires_at is not None  # noqa: S101 - guaranteed by profile validation
    old.expires_at = rotation_overlap_expiry(rotated_at, old.expires_at)
    overlap_seconds = max(0, int((old.expires_at - rotated_at).total_seconds()))
    old_prefix = old.token_prefix
    read_model, plaintext = await create_mcp_read_token(
        session,
        name=f"{old.name} (rotated)",
        workspace_id=workspace_id,
        env=env,
        rotated_from_id=old.id,
        now=rotated_at,
    )
    await session.flush()
    return read_model, plaintext, old_prefix, old.expires_at, overlap_seconds


async def revoke_mcp_read_token(
    session: AsyncSession,
    token_id: uuid.UUID,
) -> tuple[str, uuid.UUID]:
    """Revoke an MCP read-profile token and return its safe prefix."""
    token_obj = await session.get(ApiToken, token_id)
    if token_obj is None:
        raise LookupError("token_not_found")
    if token_obj.profile_id != MCP_READ_PROFILE_ID:
        raise McpReadTokenValidationError("legacy_token")
    if token_obj.workspace_id is None:
        raise McpReadTokenValidationError("workspace_required")
    token_obj.is_active = False
    await session.flush()
    return token_obj.token_prefix, token_obj.workspace_id


async def delete_api_token(session: AsyncSession, token_id: uuid.UUID) -> None:
    """Soft-delete a token by marking it inactive and flushing.

    Args:
        session:  Active async SQLAlchemy session.
        token_id: UUID of the token to deactivate.
    """
    token_obj = await session.get(ApiToken, token_id)
    if token_obj is not None:
        token_obj.is_active = False
        await session.flush()
        log.info("token_deleted", token_prefix=token_obj.token_prefix)


__all__ = [
    "MCP_READ_MAX_LIFETIME",
    "MCP_READ_PROFILE_ID",
    "MCP_READ_ROTATION_OVERLAP",
    "McpReadTokenValidationError",
    "create_api_token",
    "create_mcp_read_token",
    "delete_api_token",
    "generate_token",
    "hash_token",
    "revoke_mcp_read_token",
    "rotate_mcp_read_token",
    "rotation_overlap_expiry",
    "validate_mcp_read_token",
    "verify_token",
]
