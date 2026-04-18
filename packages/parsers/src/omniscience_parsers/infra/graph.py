"""Infrastructure dependency graph extractor.

Takes a :class:`~omniscience_parsers.base.ParsedDocument` produced by
:class:`~omniscience_parsers.infra.terraform.TerraformParser` or
:class:`~omniscience_parsers.infra.kubernetes.KubernetesParser` and returns
simple data-transfer objects that downstream indexers can write to the
``entities`` and ``edges`` tables without depending on SQLAlchemy models.

Edge types
----------
``"depends_on"``
    Explicit ``depends_on`` declaration in Terraform or equivalent K8s mechanism.

``"references"``
    Implicit reference via attribute interpolation (e.g. ``${aws_s3_bucket.my_bucket.arn}``).

``"selects"``
    Kubernetes label selector matching (Deployment → Pods, Service → Pods).

``"owns"``
    Kubernetes ownerReference (ReplicaSet → Pod, etc.).

``"mounts"``
    Volume or envFrom reference to ConfigMap or Secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omniscience_parsers.base import ParsedDocument, Section

# ---------------------------------------------------------------------------
# Data-transfer objects (NOT SQLAlchemy models)
# ---------------------------------------------------------------------------


@dataclass
class EntityData:
    """A node in the infrastructure graph extracted from a parsed document."""

    symbol: str
    """Canonical identifier, e.g. ``resource.aws_s3_bucket.my_bucket`` or
    ``Deployment/default/nginx``."""

    kind: str
    """High-level kind: ``"terraform_resource"``, ``"terraform_module"``,
    ``"terraform_variable"``, ``"k8s_resource"``."""

    name: str
    """Human-readable short name."""

    namespace: str = ""
    """Kubernetes namespace (empty for Terraform resources)."""

    labels: dict[str, str] = field(default_factory=dict)
    """Key/value labels or Terraform resource tags if present."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Additional metadata (block_type, resource_type, k8s kind, …)."""


@dataclass
class EdgeData:
    """A directed dependency edge between two infrastructure entities."""

    from_symbol: str
    """Symbol of the source entity."""

    to_symbol: str
    """Symbol of the target entity."""

    edge_type: str
    """One of: ``depends_on``, ``references``, ``selects``, ``owns``, ``mounts``."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Optional extra context (e.g. the label selector dict for ``selects`` edges)."""


# ---------------------------------------------------------------------------
# Terraform graph extraction
# ---------------------------------------------------------------------------


def _tf_entity_from_section(section: Section) -> EntityData | None:
    """Convert a Terraform section to an :class:`EntityData` node."""
    if not section.symbol:
        return None

    meta = section.metadata
    block_type = meta.get("block_type", "")

    kind_map = {
        "resource": "terraform_resource",
        "data": "terraform_data_source",
        "module": "terraform_module",
        "variable": "terraform_variable",
        "output": "terraform_output",
        "provider": "terraform_provider",
    }
    entity_kind = kind_map.get(block_type, "terraform_block")

    name_parts = section.symbol.split(".")
    name = name_parts[-1] if name_parts else section.symbol

    return EntityData(
        symbol=section.symbol,
        kind=entity_kind,
        name=name,
        extra={
            "block_type": block_type,
            "resource_type": meta.get("resource_type", ""),
        },
    )


def _tf_edges_from_section(section: Section) -> list[EdgeData]:
    """Extract dependency edges from a Terraform section's metadata."""
    edges: list[EdgeData] = []
    if not section.symbol:
        return edges

    meta = section.metadata

    # Explicit depends_on
    for dep in meta.get("depends_on", []):
        dep_str = str(dep).strip()
        if dep_str:
            edges.append(
                EdgeData(
                    from_symbol=section.symbol,
                    to_symbol=dep_str,
                    edge_type="depends_on",
                )
            )

    # Implicit references via interpolation / attribute values
    for ref in meta.get("refs", []):
        ref_str = str(ref).strip()
        if ref_str and ref_str != section.symbol:
            edges.append(
                EdgeData(
                    from_symbol=section.symbol,
                    to_symbol=ref_str,
                    edge_type="references",
                )
            )

    return edges


# ---------------------------------------------------------------------------
# Kubernetes graph extraction
# ---------------------------------------------------------------------------


def _k8s_entity_from_section(section: Section) -> EntityData | None:
    """Convert a Kubernetes section to an :class:`EntityData` node."""
    if not section.symbol:
        return None

    meta = section.metadata
    kind = meta.get("kind", "")
    namespace = meta.get("namespace", "")
    name = meta.get("name", "")
    labels = meta.get("labels", {}) or {}

    return EntityData(
        symbol=section.symbol,
        kind="k8s_resource",
        name=name,
        namespace=namespace,
        labels=dict(labels),
        extra={"k8s_kind": kind},
    )


def _k8s_edges_from_section(section: Section) -> list[EdgeData]:
    """Extract dependency edges from a Kubernetes section's metadata."""
    edges: list[EdgeData] = []
    if not section.symbol:
        return edges

    meta = section.metadata

    # ownerReferences → owns
    for owner_sym in meta.get("owner_refs", []):
        owner_str = str(owner_sym).strip()
        if owner_str:
            edges.append(
                EdgeData(
                    from_symbol=owner_str,
                    to_symbol=section.symbol,
                    edge_type="owns",
                )
            )

    # Label selectors (Deployment/StatefulSet/etc. → conceptual pod selection)
    selectors = meta.get("selectors", {}) or {}
    if selectors:
        edges.append(
            EdgeData(
                from_symbol=section.symbol,
                to_symbol="pods",  # sentinel — resolved by retrieval layer
                edge_type="selects",
                extra={"selector": selectors},
            )
        )

    # Service → pods via selector
    service_selector = meta.get("service_selector", {}) or {}
    if service_selector:
        edges.append(
            EdgeData(
                from_symbol=section.symbol,
                to_symbol="pods",
                edge_type="selects",
                extra={"selector": service_selector},
            )
        )

    # Volume / envFrom mounts
    for vol_ref in meta.get("volume_refs", []):
        vol_ref_str = str(vol_ref).strip()
        if vol_ref_str:
            edges.append(
                EdgeData(
                    from_symbol=section.symbol,
                    to_symbol=vol_ref_str,
                    edge_type="mounts",
                )
            )

    return edges


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _is_terraform_doc(doc: ParsedDocument) -> bool:
    return doc.content_type in ("application/x-terraform", "text/x-terraform", "application/hcl")


def _is_kubernetes_doc(doc: ParsedDocument) -> bool:
    return doc.content_type in ("application/x-kubernetes", "text/x-kubernetes")


def extract_infra_graph(
    parsed: ParsedDocument,
) -> tuple[list[EntityData], list[EdgeData]]:
    """Extract entities and edges from a parsed infrastructure document.

    Works for both Terraform and Kubernetes documents.  Returns empty lists
    for documents of other content types.

    Parameters
    ----------
    parsed:
        The output of :class:`TerraformParser` or :class:`KubernetesParser`.

    Returns
    -------
    tuple[list[EntityData], list[EdgeData]]
        A pair of (entities, edges).  Entities represent infrastructure resources;
        edges represent dependency relationships between them.
    """
    entities: list[EntityData] = []
    edges: list[EdgeData] = []

    if _is_terraform_doc(parsed):
        for section in parsed.sections:
            entity = _tf_entity_from_section(section)
            if entity is not None:
                entities.append(entity)
            edges.extend(_tf_edges_from_section(section))

    elif _is_kubernetes_doc(parsed):
        for section in parsed.sections:
            entity = _k8s_entity_from_section(section)
            if entity is not None:
                entities.append(entity)
            edges.extend(_k8s_edges_from_section(section))

    return entities, edges


__all__ = ["EdgeData", "EntityData", "extract_infra_graph"]
