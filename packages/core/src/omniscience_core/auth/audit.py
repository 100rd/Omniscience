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


__all__ = ["audit_token_created", "audit_token_deleted"]
