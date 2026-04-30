"""Scope definitions and authorization checking for Omniscience API tokens."""

from __future__ import annotations

import enum


class Scope(enum.StrEnum):
    """Valid permission scopes for API tokens."""

    search = "search"
    sources_read = "sources:read"
    sources_write = "sources:write"
    stats_read = "stats:read"
    otel_write = "otel:write"
    incidents_write = "incidents:write"
    # operator:read — read-only access to operator-emitted entities for the
    # in-cluster reconciliation worker (issue #163). Distinct from
    # ``sources:read`` because the operator endpoint is workspace-scoped at
    # the protocol level (token's workspace_id MUST match query param) and
    # filters server-side to ``emitter=k8s-operator``. Granting
    # ``sources:read`` on its own would expose entities from every connector
    # — narrower scope is the principle-of-least-privilege fix.
    operator_read = "operator:read"
    admin = "admin"


# admin scope implies all other scopes
SCOPE_HIERARCHY: dict[Scope, set[Scope]] = {
    Scope.admin: {
        Scope.search,
        Scope.sources_read,
        Scope.sources_write,
        Scope.stats_read,
        Scope.otel_write,
        Scope.incidents_write,
        Scope.operator_read,
        Scope.admin,
    },
    Scope.sources_write: {Scope.sources_write},
    Scope.sources_read: {Scope.sources_read},
    Scope.stats_read: {Scope.stats_read},
    Scope.otel_write: {Scope.otel_write},
    Scope.incidents_write: {Scope.incidents_write},
    Scope.operator_read: {Scope.operator_read},
    Scope.search: {Scope.search},
}


def _expand_scopes(granted: set[Scope]) -> set[Scope]:
    """Expand granted scopes by applying hierarchy rules."""
    expanded: set[Scope] = set()
    for scope in granted:
        expanded.update(SCOPE_HIERARCHY.get(scope, {scope}))
    return expanded


def check_scopes(required: set[Scope], granted: set[Scope]) -> bool:
    """Return True if granted scopes satisfy all required scopes.

    Applies the scope hierarchy (admin implies all others).
    """
    effective = _expand_scopes(granted)
    return required.issubset(effective)


__all__ = ["SCOPE_HIERARCHY", "Scope", "check_scopes"]
