"""API token generation, hashing, and database lifecycle management."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from sqlalchemy.ext.asyncio import AsyncSession

from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenCreate, ApiTokenRead

log = structlog.get_logger(__name__)

_ph = PasswordHasher()

# Token format: sk_{env}_{uuid}_{random32}
# prefix = first 8 chars of the full token string
_PREFIX_LEN = 8
_RANDOM_BYTES = 24  # produces ~32 url-safe base64 chars


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
) -> tuple[ApiTokenRead, str]:
    """Create a new API token, persist it, and return the read model + plaintext.

    The plaintext token is returned exactly once — it cannot be recovered from
    the stored hash.  The caller MUST surface it to the user immediately.

    Args:
        session:    Active async SQLAlchemy session.
        name:       Human-readable label for this token.
        scopes:     List of scope strings (e.g. ["search", "sources:read"]).
        expires_at: Optional expiry datetime (UTC).

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
    )

    token_obj = ApiToken(
        name=create_schema.name,
        hashed_token=create_schema.hashed_token,
        token_prefix=create_schema.token_prefix,
        scopes=create_schema.scopes,
        expires_at=create_schema.expires_at,
    )
    session.add(token_obj)
    await session.flush()
    await session.refresh(token_obj)

    read_model = ApiTokenRead.model_validate(token_obj)
    log.info("token_created", token_prefix=prefix, name=name, scopes=scopes)
    return read_model, plaintext


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
    "create_api_token",
    "delete_api_token",
    "generate_token",
    "hash_token",
    "verify_token",
]
