"""Terraform config <-> state drift (Gap C).

Drift is the gap between what Terraform *configuration* declares and what the
*state* file records as actually provisioned.  The infra graph already links the
two: :func:`omniscience_parsers.infra.graph.extract_tfstate_graph` emits a
``tfstate_of`` edge from every ``tfstate_instance`` to its ``terraform_resource``
configuration symbol (e.g. ``resource.aws_s3_bucket.logs``).  This module walks
those edges and classifies three drift categories:

* **state-not-in-config** — a ``tfstate_instance`` whose ``tfstate_of`` target
  has no matching ``terraform_resource`` node (resource exists in state but is
  no longer declared — an orphan / pending-destroy or out-of-band import).
* **config-not-in-state** — a ``terraform_resource`` node that no
  ``tfstate_instance`` points at (declared but never applied / not yet created).
* **attribute divergence** — a resource present on BOTH sides whose comparable
  attributes differ between config and state.

Attribute comparison
--------------------
Config and state expose attributes in different shapes (HCL body vs. provider
JSON), so this engine compares only a **normalised attribute map** each
``EntityData`` may carry at ``extra["attributes"]`` (``dict[str, str]``).  When
either side lacks that map, attribute comparison for that pair is skipped (the
membership categories above still apply) — the engine never fabricates a
divergence from an absent attribute.  Populating ``extra["attributes"]`` on both
parser sides is parser-side follow-up; the engine is correct and tested today
for whatever attributes the parsers surface.

The engine is pure (no I/O) and deterministic (sorted output) so it is trivially
unit-testable and safe to call from a tool/REST handler.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from omniscience_parsers.infra.graph import EdgeData, EntityData

# Graph kinds / edge types this engine reasons over.
_TFSTATE_INSTANCE_KIND = "tfstate_instance"
_TERRAFORM_RESOURCE_KIND = "terraform_resource"
_TFSTATE_OF_EDGE = "tfstate_of"

# Attribute map carried on EntityData.extra when the parser normalises it.
_ATTRIBUTES_KEY = "attributes"


@dataclass(slots=True)
class AttributeDivergence:
    """One attribute that differs between config and state for one resource."""

    config_symbol: str
    """The ``terraform_resource`` symbol (e.g. ``resource.aws_s3_bucket.logs``)."""

    state_symbol: str
    """The ``tfstate_instance`` symbol bound to the config via ``tfstate_of``."""

    attribute: str
    """Name of the diverging attribute."""

    config_value: str
    """Value declared in configuration."""

    state_value: str
    """Value recorded in state."""


@dataclass(slots=True)
class DriftReport:
    """Structured config <-> state drift over the ``tfstate_of`` graph."""

    state_not_in_config: list[str] = field(default_factory=list)
    """``tfstate_instance`` symbols whose ``tfstate_of`` target is not a known
    ``terraform_resource`` (sorted)."""

    config_not_in_state: list[str] = field(default_factory=list)
    """``terraform_resource`` symbols no ``tfstate_instance`` points at (sorted)."""

    attribute_divergences: list[AttributeDivergence] = field(default_factory=list)
    """Per-attribute config/state mismatches for resources present on both sides
    (sorted by ``(config_symbol, attribute)``)."""

    @property
    def has_drift(self) -> bool:
        """True when any drift category is non-empty."""
        return bool(
            self.state_not_in_config or self.config_not_in_state or self.attribute_divergences
        )


def _attributes_of(entity: EntityData) -> dict[str, str]:
    """Return the normalised attribute map of *entity*, or an empty dict.

    Only string-coercible scalar values are compared; nested structures are
    stringified so the engine stays format-agnostic.
    """
    raw = entity.extra.get(_ATTRIBUTES_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _diff_attributes(
    *,
    config_symbol: str,
    state_symbol: str,
    config_attrs: dict[str, str],
    state_attrs: dict[str, str],
) -> list[AttributeDivergence]:
    """Compare the intersection of attribute keys between config and state.

    Only keys present on BOTH sides are compared — a key that exists in state
    but not in config (provider-computed defaults like ``arn``/``id``) is not
    drift, and vice versa.  Returns divergences sorted by attribute name.
    """
    shared = set(config_attrs) & set(state_attrs)
    out: list[AttributeDivergence] = []
    for attr in sorted(shared):
        cfg = config_attrs[attr]
        sta = state_attrs[attr]
        if cfg != sta:
            out.append(
                AttributeDivergence(
                    config_symbol=config_symbol,
                    state_symbol=state_symbol,
                    attribute=attr,
                    config_value=cfg,
                    state_value=sta,
                )
            )
    return out


def compute_tf_drift(
    entities: Iterable[EntityData],
    edges: Iterable[EdgeData],
) -> DriftReport:
    """Compute config <-> state drift from an infra graph.

    Parameters
    ----------
    entities:
        ``EntityData`` nodes — a mix of ``terraform_resource`` (config) and
        ``tfstate_instance`` (state) kinds, e.g. the concatenation of
        :func:`extract_infra_graph` over the Terraform config docs and
        :func:`extract_tfstate_graph` over the state doc.
    edges:
        ``EdgeData`` edges; only ``tfstate_of`` edges are consulted.

    Returns
    -------
    DriftReport
        Deterministic, sorted drift classification.
    """
    entity_list = list(entities)
    edge_list = list(edges)

    config_by_symbol: dict[str, EntityData] = {
        e.symbol: e for e in entity_list if e.kind == _TERRAFORM_RESOURCE_KIND
    }
    state_by_symbol: dict[str, EntityData] = {
        e.symbol: e for e in entity_list if e.kind == _TFSTATE_INSTANCE_KIND
    }

    # Map each state instance -> its config target via the tfstate_of edge.
    state_to_config: dict[str, str] = {}
    for edge in edge_list:
        if edge.edge_type != _TFSTATE_OF_EDGE:
            continue
        # Only trust edges whose source is a known state instance.
        if edge.from_symbol in state_by_symbol:
            state_to_config[edge.from_symbol] = edge.to_symbol

    state_not_in_config: list[str] = []
    matched_config_symbols: set[str] = set()
    attribute_divergences: list[AttributeDivergence] = []

    for state_symbol in state_by_symbol:
        config_symbol = state_to_config.get(state_symbol)
        if config_symbol is None or config_symbol not in config_by_symbol:
            # In state, but the config target is unknown / missing.
            state_not_in_config.append(state_symbol)
            continue
        matched_config_symbols.add(config_symbol)
        attribute_divergences.extend(
            _diff_attributes(
                config_symbol=config_symbol,
                state_symbol=state_symbol,
                config_attrs=_attributes_of(config_by_symbol[config_symbol]),
                state_attrs=_attributes_of(state_by_symbol[state_symbol]),
            )
        )

    config_not_in_state = [
        symbol for symbol in config_by_symbol if symbol not in matched_config_symbols
    ]

    return DriftReport(
        state_not_in_config=sorted(state_not_in_config),
        config_not_in_state=sorted(config_not_in_state),
        attribute_divergences=sorted(
            attribute_divergences, key=lambda d: (d.config_symbol, d.attribute)
        ),
    )


def drift_report_to_dict(report: DriftReport) -> dict[str, object]:
    """Serialise a :class:`DriftReport` to a JSON-safe dict for tool/REST output."""
    return {
        "has_drift": report.has_drift,
        "state_not_in_config": list(report.state_not_in_config),
        "config_not_in_state": list(report.config_not_in_state),
        "attribute_divergences": [
            {
                "config_symbol": d.config_symbol,
                "state_symbol": d.state_symbol,
                "attribute": d.attribute,
                "config_value": d.config_value,
                "state_value": d.state_value,
            }
            for d in report.attribute_divergences
        ],
    }


__all__ = [
    "AttributeDivergence",
    "DriftReport",
    "compute_tf_drift",
    "drift_report_to_dict",
]
