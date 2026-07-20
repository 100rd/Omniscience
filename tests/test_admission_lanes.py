"""Unit tests for server-derived SRE incident vs background admission lane
resolution (issue #350, AC-SCALE-2). Lane identity must come solely from
the authenticated token's scopes — never from any client-controlled input.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from omniscience_core.auth.scopes import Scope
from omniscience_server.admission.lanes import (
    BACKGROUND_LANE,
    INCIDENT_LANE,
    resolve_admission_lane,
)


def _token(scopes: list[str]) -> MagicMock:
    tok = MagicMock()
    tok.scopes = scopes
    return tok


def test_search_only_token_resolves_to_background_lane() -> None:
    assert resolve_admission_lane(_token([Scope.search.value])) == BACKGROUND_LANE


def test_incidents_write_token_resolves_to_incident_lane() -> None:
    assert resolve_admission_lane(_token([Scope.incidents_write.value])) == INCIDENT_LANE


def test_admin_token_resolves_to_incident_lane() -> None:
    assert resolve_admission_lane(_token([Scope.admin.value])) == INCIDENT_LANE


def test_mixed_scopes_including_incidents_write_resolve_to_incident_lane() -> None:
    scopes = [Scope.search.value, Scope.sources_read.value, Scope.incidents_write.value]
    assert resolve_admission_lane(_token(scopes)) == INCIDENT_LANE


def test_no_scopes_resolves_to_background_lane() -> None:
    assert resolve_admission_lane(_token([])) == BACKGROUND_LANE


def test_missing_scopes_attribute_resolves_to_background_lane() -> None:
    """A token without a usable `scopes` value must fail closed to the
    unprivileged lane, never default to the higher-priority one."""
    tok = MagicMock()
    tok.scopes = None
    assert resolve_admission_lane(tok) == BACKGROUND_LANE
