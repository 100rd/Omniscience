"""Audit logging for API token lifecycle events."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


def audit_token_created(
    token_prefix: str,
    scopes: list[str],
    actor: str = "system",
) -> None:
    """Emit a structured audit log entry for token creation.

    Args:
        token_prefix: First 8 chars of the token (safe to log).
        scopes:       Scopes granted to the token.
        actor:        Identity initiating the action (defaults to "system").
    """
    log.info(
        "audit.token.created",
        event_type="token_created",
        token_prefix=token_prefix,
        scopes=scopes,
        actor=actor,
    )


def audit_token_deleted(
    token_prefix: str,
    actor: str = "system",
) -> None:
    """Emit a structured audit log entry for token deletion.

    Args:
        token_prefix: First 8 chars of the deleted token (safe to log).
        actor:        Identity initiating the action (defaults to "system").
    """
    log.info(
        "audit.token.deleted",
        event_type="token_deleted",
        token_prefix=token_prefix,
        actor=actor,
    )


def audit_mcp_token_created(
    token_id: str,
    token_prefix: str,
    workspace_id: str,
    *,
    expires_at: str,
    actor_id: str,
    actor_prefix: str,
) -> None:
    """Audit creation of the constrained MCP v1 read profile."""
    log.info(
        "audit.token.mcp_read.created",
        event_type="mcp_read_token_created",
        token_id=token_id,
        token_prefix=token_prefix,
        profile_id="omniscience-mcp-read-v1",
        scopes=["search"],
        workspace_id=workspace_id,
        expires_at=expires_at,
        actor_id=actor_id,
        actor_prefix=actor_prefix,
    )


def audit_mcp_token_rotated(
    old_token_id: str,
    old_token_prefix: str,
    new_token_id: str,
    new_token_prefix: str,
    *,
    workspace_id: str,
    old_expires_at: str,
    new_expires_at: str,
    overlap_seconds: int,
    actor_id: str,
    actor_prefix: str,
) -> None:
    """Audit a bounded-overlap MCP token rotation."""
    log.info(
        "audit.token.mcp_read.rotated",
        event_type="mcp_read_token_rotated",
        old_token_id=old_token_id,
        old_token_prefix=old_token_prefix,
        new_token_id=new_token_id,
        new_token_prefix=new_token_prefix,
        profile_id="omniscience-mcp-read-v1",
        workspace_id=workspace_id,
        old_expires_at=old_expires_at,
        new_expires_at=new_expires_at,
        overlap_seconds=overlap_seconds,
        overlap_cap_hours=24,
        actor_id=actor_id,
        actor_prefix=actor_prefix,
    )


def audit_mcp_token_revoked(
    token_id: str,
    token_prefix: str,
    *,
    workspace_id: str,
    actor_id: str,
    actor_prefix: str,
) -> None:
    """Audit explicit revocation of an MCP v1 read token."""
    log.info(
        "audit.token.mcp_read.revoked",
        event_type="mcp_read_token_revoked",
        token_id=token_id,
        token_prefix=token_prefix,
        profile_id="omniscience-mcp-read-v1",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_prefix=actor_prefix,
    )


__all__ = [
    "audit_mcp_token_created",
    "audit_mcp_token_revoked",
    "audit_mcp_token_rotated",
    "audit_token_created",
    "audit_token_deleted",
]
