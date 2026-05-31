"""Calibrated confidence scoring — thin re-export shim (issue #155).

All logic has moved to
:mod:`omniscience_retrieval.incidents.scoring`.  This module re-exports
the public surface so existing importers in ``apps/server`` (REST
handlers, admin endpoints) require no path changes.
"""

from omniscience_retrieval.incidents.scoring import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_WEIGHTS,
    SETTINGS_KEY,
    WEIGHT_SUM_TOLERANCE,
    IncidentScoringConfig,
    IncidentScoringResponse,
    IncidentScoringUpdateRequest,
    IncidentScoringWeights,
    apply_weights,
    compute_components,
    load_workspace_config,
    save_workspace_config,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_WEIGHTS",
    "SETTINGS_KEY",
    "WEIGHT_SUM_TOLERANCE",
    "IncidentScoringConfig",
    "IncidentScoringResponse",
    "IncidentScoringUpdateRequest",
    "IncidentScoringWeights",
    "apply_weights",
    "compute_components",
    "load_workspace_config",
    "save_workspace_config",
]
