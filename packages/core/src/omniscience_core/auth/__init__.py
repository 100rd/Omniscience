"""Auth package: token generation, hashing, scope enforcement, and audit logging."""

from __future__ import annotations

from omniscience_core.auth.audit import audit_token_created, audit_token_deleted
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope, check_scopes
from omniscience_core.auth.tokens import (
    create_api_token,
    delete_api_token,
    generate_token,
    hash_token,
    verify_token,
)

__all__ = [
    "Scope",
    "audit_token_created",
    "audit_token_deleted",
    "check_scopes",
    "create_api_token",
    "delete_api_token",
    "generate_token",
    "get_current_token",
    "hash_token",
    "require_scope",
    "verify_token",
]
