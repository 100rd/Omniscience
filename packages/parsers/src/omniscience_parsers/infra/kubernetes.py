"""Kubernetes manifest parser — extracts workloads, services, and config resources from YAML.

Each Kubernetes resource in a multi-document YAML file becomes a
:class:`~omniscience_parsers.base.Section` with:
- ``symbol``:   ``"kind/namespace/name"``   e.g. ``"Deployment/default/nginx"``
- ``metadata``: ``{"kind": ..., "namespace": ..., "name": ..., "labels": {...},
                   "owner_refs": [...], "selectors": [...], "service_target": ...}``

The parser handles both single-document and multi-document (``---`` separated) YAML files.
It is intentionally permissive — malformed documents produce a warning in
``ParsedDocument.metadata`` but do not raise.

Only files containing a ``kind:`` field are claimed by :meth:`KubernetesParser.can_handle`.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
import yaml  # type: ignore[import-untyped]

from omniscience_parsers.base import ParsedDocument, Section

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Content-type / extension detection
# ---------------------------------------------------------------------------

_K8S_CONTENT_TYPES = frozenset(
    {
        "application/x-kubernetes",
        "text/x-kubernetes",
    }
)
_YAML_EXTENSIONS = frozenset({".yaml", ".yml"})

# Kubernetes resource kinds we treat as first-class sections
_KNOWN_KINDS = frozenset(
    {
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ReplicaSet",
        "Job",
        "CronJob",
        "Pod",
        "Service",
        "Ingress",
        "ConfigMap",
        "Secret",
        "ServiceAccount",
        "ClusterRole",
        "ClusterRoleBinding",
        "Role",
        "RoleBinding",
        "HorizontalPodAutoscaler",
        "NetworkPolicy",
        "PersistentVolumeClaim",
        "PersistentVolume",
        "Namespace",
        "CustomResourceDefinition",
    }
)

# Quick probe: does the file look like a Kubernetes manifest?
_KIND_RE = re.compile(r"^\s*kind\s*:", re.MULTILINE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _symbol(kind: str, namespace: str, name: str) -> str:
    """Canonical symbol for a Kubernetes resource: ``kind/namespace/name``."""
    ns = namespace or "cluster"
    return f"{kind}/{ns}/{name}"


def _extract_owner_refs(resource: dict[str, Any]) -> list[str]:
    """Return ownerReferences as symbol strings."""
    refs: list[str] = []
    owner_refs = resource.get("metadata", {}).get("ownerReferences", []) or []
    for ref in owner_refs:
        if not isinstance(ref, dict):
            continue
        kind = _safe_str(ref.get("kind", ""))
        name = _safe_str(ref.get("name", ""))
        # ownerReferences are always in the same namespace
        ns = _safe_str(resource.get("metadata", {}).get("namespace", ""))
        if kind and name:
            refs.append(_symbol(kind, ns, name))
    return refs


def _extract_label_selector(selector: Any) -> dict[str, str]:
    """Normalise a matchLabels / selector to a flat dict."""
    if not isinstance(selector, dict):
        return {}
    # Support both `selector: {app: foo}` and `selector: {matchLabels: {app: foo}}`
    match_labels = selector.get("matchLabels")
    if isinstance(match_labels, dict):
        return {_safe_str(k): _safe_str(v) for k, v in match_labels.items()}
    return {_safe_str(k): _safe_str(v) for k, v in selector.items() if k != "matchExpressions"}


def _extract_pod_template_selector(resource: dict[str, Any]) -> dict[str, str]:
    """Return the selector used by a Deployment/StatefulSet/etc. to select pods."""
    spec = resource.get("spec", {}) or {}
    raw_selector = spec.get("selector", {})
    return _extract_label_selector(raw_selector)


def _extract_service_selector(resource: dict[str, Any]) -> dict[str, str]:
    """Return the pod selector from a Service spec."""
    spec = resource.get("spec", {}) or {}
    raw_selector = spec.get("selector", {})
    if not isinstance(raw_selector, dict):
        return {}
    return {_safe_str(k): _safe_str(v) for k, v in raw_selector.items()}


def _extract_volume_config_refs(resource: dict[str, Any]) -> list[str]:
    """Return ConfigMap and Secret names referenced via volumes or envFrom."""
    refs: list[str] = []
    ns = _safe_str(resource.get("metadata", {}).get("namespace", ""))

    def _scan_spec(spec: Any) -> None:
        if not isinstance(spec, dict):
            return

        # volumes
        for vol in spec.get("volumes", []) or []:
            if not isinstance(vol, dict):
                continue
            if "configMap" in vol:
                cm = vol["configMap"]
                if isinstance(cm, dict) and cm.get("name"):
                    refs.append(_symbol("ConfigMap", ns, _safe_str(cm["name"])))
            if "secret" in vol:
                sec = vol["secret"]
                if isinstance(sec, dict) and sec.get("secretName"):
                    refs.append(_symbol("Secret", ns, _safe_str(sec["secretName"])))

        # containers + initContainers envFrom
        for container_key in ("containers", "initContainers"):
            for container in spec.get(container_key, []) or []:
                if not isinstance(container, dict):
                    continue
                for env_from in container.get("envFrom", []) or []:
                    if not isinstance(env_from, dict):
                        continue
                    if "configMapRef" in env_from:
                        cm = env_from["configMapRef"]
                        if isinstance(cm, dict) and cm.get("name"):
                            refs.append(_symbol("ConfigMap", ns, _safe_str(cm["name"])))
                    if "secretRef" in env_from:
                        sec = env_from["secretRef"]
                        if isinstance(sec, dict) and sec.get("name"):
                            refs.append(_symbol("Secret", ns, _safe_str(sec["name"])))

    # For pods, the spec is directly at spec level
    resource_spec = resource.get("spec", {}) or {}
    _scan_spec(resource_spec)

    # For Deployments/StatefulSets, containers live under spec.template.spec
    template_spec = resource_spec.get("template", {})
    if isinstance(template_spec, dict):
        _scan_spec(template_spec.get("spec", {}))

    return refs


# ---------------------------------------------------------------------------
# Per-resource section builder
# ---------------------------------------------------------------------------


def _resource_to_section(
    resource: dict[str, Any],
    raw_text: str,
    line_start: int,
    line_end: int,
) -> Section | None:
    """Convert a single Kubernetes resource dict to a :class:`Section`.

    Returns None if the resource is missing required metadata.
    """
    kind = _safe_str(resource.get("kind", ""))
    meta_block = resource.get("metadata", {}) or {}
    name = _safe_str(meta_block.get("name", ""))
    namespace = _safe_str(meta_block.get("namespace", "default"))
    labels: dict[str, str] = {
        _safe_str(k): _safe_str(v) for k, v in (meta_block.get("labels") or {}).items()
    }

    if not kind or not name:
        return None

    symbol = _symbol(kind, namespace, name)
    owner_refs = _extract_owner_refs(resource)
    selectors = _extract_pod_template_selector(resource)
    service_selector = _extract_service_selector(resource) if kind == "Service" else {}
    volume_refs = _extract_volume_config_refs(resource)

    section_meta: dict[str, Any] = {
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "labels": labels,
        "owner_refs": owner_refs,
        "selectors": selectors,
        "service_selector": service_selector,
        "volume_refs": volume_refs,
    }

    return Section(
        heading_path=[kind, namespace, name],
        text=raw_text,
        line_start=line_start,
        line_end=line_end,
        symbol=symbol,
        metadata=section_meta,
    )


# ---------------------------------------------------------------------------
# KubernetesParser
# ---------------------------------------------------------------------------


class KubernetesParser:
    """Parse Kubernetes YAML manifests into per-resource sections.

    Handles single-document and multi-document (``---`` separated) YAML.
    Only claims files that contain a ``kind:`` field — plain YAML that is not
    a Kubernetes manifest is left to :class:`~omniscience_parsers.plaintext.PlainTextParser`.
    """

    def can_handle(self, content_type: str, file_extension: str) -> bool:
        """Return True for Kubernetes-specific content-types or YAML extensions."""
        if content_type in _K8S_CONTENT_TYPES:
            return True
        return file_extension.lower() in _YAML_EXTENSIONS

    def _is_kubernetes_content(self, content: bytes) -> bool:
        """Quick probe: does the content look like a Kubernetes manifest?"""
        try:
            text = content.decode(errors="replace")
        except Exception:
            return False
        return bool(_KIND_RE.search(text))

    def parse(self, content: bytes, file_path: str = "") -> ParsedDocument:
        """Parse *content* as a Kubernetes YAML manifest.

        Non-Kubernetes YAML falls back to a single plain-text section.
        Errors in individual documents are captured in metadata and do not abort
        processing of the remaining documents.
        """
        if not self._is_kubernetes_content(content):
            # Not a k8s manifest — return minimal document so dispatch can route elsewhere
            from omniscience_parsers.plaintext import PlainTextParser

            return PlainTextParser().parse(content, file_path=file_path)

        raw_text = content.decode(errors="replace")
        sections: list[Section] = []
        parse_warnings: list[str] = []

        # Split on YAML document separator; each sub-document is parsed independently
        raw_docs = self._split_yaml_docs(raw_text)
        line_cursor = 1

        for doc_text in raw_docs:
            doc_lines = doc_text.splitlines()
            doc_line_end = line_cursor + len(doc_lines) - 1

            try:
                resource = yaml.safe_load(doc_text)
            except yaml.YAMLError as exc:
                warning = f"yaml_parse_error at line {line_cursor}: {exc}"
                log.warning("kubernetes_yaml_error", file_path=file_path, error=str(exc))
                parse_warnings.append(warning)
                line_cursor = doc_line_end + 1
                continue

            if not isinstance(resource, dict):
                line_cursor = doc_line_end + 1
                continue

            section = _resource_to_section(
                resource,
                raw_text=doc_text,
                line_start=line_cursor,
                line_end=doc_line_end,
            )
            if section is not None:
                sections.append(section)

            line_cursor = doc_line_end + 1

        doc_meta: dict[str, Any] = {}
        if parse_warnings:
            doc_meta["parse_warnings"] = parse_warnings

        return ParsedDocument(
            sections=sections,
            content_type="application/x-kubernetes",
            language="yaml",
            metadata=doc_meta,
        )

    @staticmethod
    def _split_yaml_docs(text: str) -> list[str]:
        """Split a YAML file into individual documents on ``---`` separators."""
        # Split on lines that are exactly '---' (possibly with trailing whitespace)
        doc_separator = re.compile(r"^---\s*$", re.MULTILINE)
        parts = doc_separator.split(text)
        return [p for p in parts if p.strip()]


__all__ = ["KubernetesParser"]
