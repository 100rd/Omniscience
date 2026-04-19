"""Kubernetes agentic connector.

Uses an LLM agent to decide which Kubernetes resource kinds to index.
The LLM is given the list of available API resource kinds in the cluster and
asked to return a JSON decision about which to include and which to exclude.

Config
------
``K8sAgenticConfig``:
    - ``cluster_name`` — human-readable label stored in document metadata.
    - ``api_server`` — Kubernetes API server URL (e.g. ``https://k8s.example.com``).
    - ``namespace`` — namespace to scope discovery; empty = all namespaces.
    - ``default_include_kinds`` — fallback list used when LLM fails.
    - ``default_exclude_kinds`` — kinds always excluded regardless of LLM output.

Secrets
-------
``kubeconfig`` or ``token`` (service-account JWT).  Passed via the secrets dict.

Discovery flow
--------------
1. Query ``/api`` and ``/apis`` to enumerate available resource kinds.
2. Build a prompt listing the kinds and asking the LLM which to index.
3. Parse the LLM's JSON response (``{"include": [...], "exclude": [...]}``) into
   ``DocumentRef`` objects — one per (kind, namespace/resource name) pair.
4. Fall back to ``default_include_kinds`` minus ``default_exclude_kinds`` on
   parse failure.

Note: ``fetch()`` retrieves the resource as JSON and stores it as
``application/json`` bytes.  The downstream pipeline is responsible for further
parsing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from omniscience_connectors.agentic.base import AgentConfig, AgenticConnector
from omniscience_connectors.base import DocumentRef, FetchedDocument, WebhookHandler

__all__ = ["K8sAgenticConfig", "K8sAgenticConnector"]

logger = logging.getLogger(__name__)

# Resource kinds that are almost always noise or contain secrets — always skip.
_ALWAYS_EXCLUDE: frozenset[str] = frozenset(
    {
        "Secret",
        "Event",
        "TokenReview",
        "SubjectAccessReview",
        "SelfSubjectAccessReview",
        "SelfSubjectRulesReview",
        "LocalSubjectAccessReview",
    }
)

# Safe default set when no LLM guidance is available.
_DEFAULT_INCLUDE_KINDS: list[str] = [
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "CronJob",
    "Job",
    "ConfigMap",
    "Service",
    "Ingress",
    "NetworkPolicy",
    "ResourceQuota",
    "LimitRange",
    "HorizontalPodAutoscaler",
    "PersistentVolumeClaim",
    "ServiceAccount",
    "Role",
    "RoleBinding",
    "ClusterRole",
    "ClusterRoleBinding",
]

_DEFAULT_EXCLUDE_KINDS: list[str] = [
    "Pod",
    "ReplicaSet",
    "Endpoints",
    "EndpointSlice",
]

_DISCOVERY_PROMPT_TEMPLATE = """\
You are a Kubernetes documentation agent.  Your task is to decide which \
Kubernetes resource kinds should be indexed for the engineering knowledge base.

Available resource kinds in the cluster:
{kinds_list}

Rules:
- Include kinds that describe DESIRED STATE (Deployments, Services, ConfigMaps, \
RBAC, HPA, etc.).
- Exclude ephemeral or high-churn resources (Pods, ReplicaSets, Endpoints, Events).
- ALWAYS exclude Secrets and TokenReview (security risk).
- Be selective — more signal, less noise.

Return ONLY a JSON object with exactly two keys: "include" and "exclude".
Each value is a list of kind strings from the available kinds above.
Do not include any explanation outside the JSON object.

Example:
{{
  "include": ["Deployment", "Service", "ConfigMap"],
  "exclude": ["Pod", "ReplicaSet", "Endpoints"]
}}
"""

# ---------------------------------------------------------------------------
# Kind → REST path lookup tables (module-level to satisfy naming conventions)
# ---------------------------------------------------------------------------

_KIND_CORE: dict[str, str] = {
    "ConfigMap": "configmaps",
    "Endpoints": "endpoints",
    "Event": "events",
    "LimitRange": "limitranges",
    "Namespace": "namespaces",
    "Node": "nodes",
    "PersistentVolume": "persistentvolumes",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "Pod": "pods",
    "ReplicationController": "replicationcontrollers",
    "ResourceQuota": "resourcequotas",
    "Secret": "secrets",
    "Service": "services",
    "ServiceAccount": "serviceaccounts",
}

_KIND_APPS: dict[str, str] = {
    "Deployment": "deployments",
    "DaemonSet": "daemonsets",
    "ReplicaSet": "replicasets",
    "StatefulSet": "statefulsets",
}

_KIND_BATCH: dict[str, str] = {
    "CronJob": "cronjobs",
    "Job": "jobs",
}

_KIND_NETWORKING: dict[str, str] = {
    "Ingress": "ingresses",
    "NetworkPolicy": "networkpolicies",
}

_KIND_RBAC: dict[str, str] = {
    "ClusterRole": "clusterroles",
    "ClusterRoleBinding": "clusterrolebindings",
    "Role": "roles",
    "RoleBinding": "rolebindings",
}

_KIND_AUTOSCALING: dict[str, str] = {
    "HorizontalPodAutoscaler": "horizontalpodautoscalers",
}

# Core kinds that are cluster-scoped (no namespace segment in path).
_CORE_CLUSTER_SCOPED: frozenset[str] = frozenset({"Namespace", "Node", "PersistentVolume"})

# RBAC kinds that are cluster-scoped.
_RBAC_CLUSTER_SCOPED: frozenset[str] = frozenset({"ClusterRole", "ClusterRoleBinding"})


class K8sAgenticConfig(BaseModel):
    """Public configuration for the K8s agentic connector (no secrets)."""

    cluster_name: str = Field(
        default="default",
        description="Human-readable cluster identifier stored in document metadata.",
    )

    api_server: str = Field(
        default="https://kubernetes.default.svc",
        description="Kubernetes API server base URL.",
    )

    namespace: str = Field(
        default="",
        description="Namespace to scope resource listing. Empty = all namespaces.",
    )

    default_include_kinds: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_INCLUDE_KINDS),
        description="Fallback list of resource kinds used when the LLM fails.",
    )

    default_exclude_kinds: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_EXCLUDE_KINDS),
        description="Resource kinds always excluded (merged with LLM exclusions).",
    )

    verify_ssl: bool = Field(
        default=True,
        description="Whether to verify TLS certificates when calling the API server.",
    )


class K8sAgenticConnector(AgenticConnector):
    """Kubernetes connector whose discovery scope is decided by an LLM agent.

    The LLM receives the full list of available API resource kinds in the cluster
    and returns JSON specifying which to include and which to exclude.  One
    ``DocumentRef`` is yielded per discovered kind (the *kind* is the document
    unit — individual resource instances are enumerated during fetch or in a
    separate pipeline stage).

    Fallback: if the LLM returns unparsable output after all iterations, the
    connector falls back to ``config.default_include_kinds`` minus
    ``config.default_exclude_kinds`` minus ``_ALWAYS_EXCLUDE``.
    """

    connector_type: ClassVar[str] = "k8s-agentic"
    config_schema: ClassVar[type[BaseModel]] = K8sAgenticConfig

    agent_config: ClassVar[AgentConfig] = AgentConfig(
        instructions=(
            "You are a Kubernetes documentation agent deciding which resource "
            "kinds to index for the engineering knowledge base.  Return JSON."
        ),
        model_name="llama3",
        max_iterations=3,
        provider="ollama",
    )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the API server is reachable and the token is valid."""
        cfg: K8sAgenticConfig = config  # type: ignore[assignment]
        token = secrets.get("token") or secrets.get("kubeconfig", "")

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(
                verify=cfg.verify_ssl,
                headers=headers,
                timeout=10.0,
            ) as client:
                resp = await client.get(f"{cfg.api_server}/api")
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"K8s API server returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Cannot reach K8s API server at {cfg.api_server}: {exc}") from exc

    # ------------------------------------------------------------------
    # AgenticConnector abstract implementations
    # ------------------------------------------------------------------

    def _build_discovery_prompt(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        context: dict[str, Any],
    ) -> str:
        """Build the prompt from available resource kinds stored in context."""
        kinds: list[str] = context.get("available_kinds", [])
        if not kinds:
            # Fallback to default kinds when live enumeration wasn't available
            kinds = list(_DEFAULT_INCLUDE_KINDS) + list(_DEFAULT_EXCLUDE_KINDS)

        kinds_list = "\n".join(f"- {k}" for k in sorted(set(kinds)))
        return _DISCOVERY_PROMPT_TEMPLATE.format(kinds_list=kinds_list)

    def _parse_llm_response(
        self,
        response: str,
        config: BaseModel,
    ) -> list[DocumentRef]:
        """Parse LLM JSON response ``{"include": [...], "exclude": [...]}`` into refs.

        Returns an empty list on any parse error so the loop can retry or fall
        back to the default set.
        """
        cfg: K8sAgenticConfig = config  # type: ignore[assignment]

        # Strip markdown code fences if the LLM wrapped the JSON
        text = response.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(line for line in lines if not line.startswith("```")).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug(
                "k8s_agentic.parse.json_error",
                extra={"response_preview": text[:200]},
            )
            return []

        if not isinstance(data, dict):
            logger.debug("k8s_agentic.parse.not_dict")
            return []

        include_kinds: list[str] = data.get("include", [])
        exclude_kinds: list[str] = data.get("exclude", [])

        if not isinstance(include_kinds, list) or not isinstance(exclude_kinds, list):
            logger.debug("k8s_agentic.parse.bad_lists")
            return []

        effective_exclude = set(exclude_kinds) | _ALWAYS_EXCLUDE | set(cfg.default_exclude_kinds)
        selected = [k for k in include_kinds if k not in effective_exclude]

        if not selected:
            logger.debug("k8s_agentic.parse.empty_selection")
            return []

        refs = [
            DocumentRef(
                external_id=f"k8s:{cfg.cluster_name}:kind:{kind}",
                uri=f"k8s://{cfg.cluster_name}/{kind}",
                metadata={
                    "kind": kind,
                    "cluster": cfg.cluster_name,
                    "namespace": cfg.namespace,
                    "source": "llm-driven",
                },
            )
            for kind in sorted(selected)
        ]
        logger.info(
            "k8s_agentic.parse.ok",
            extra={"selected_kinds": selected},
        )
        return refs

    async def _default_document_refs(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield refs for the configured default include kinds."""
        cfg: K8sAgenticConfig = config  # type: ignore[assignment]
        effective_exclude = _ALWAYS_EXCLUDE | set(cfg.default_exclude_kinds)
        for kind in sorted(k for k in cfg.default_include_kinds if k not in effective_exclude):
            yield DocumentRef(
                external_id=f"k8s:{cfg.cluster_name}:kind:{kind}",
                uri=f"k8s://{cfg.cluster_name}/{kind}",
                metadata={
                    "kind": kind,
                    "cluster": cfg.cluster_name,
                    "namespace": cfg.namespace,
                    "source": "default-fallback",
                },
            )

    # ------------------------------------------------------------------
    # Discovery override — enumerate kinds before running LLM loop
    # ------------------------------------------------------------------

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate available API resource kinds, then run the LLM loop.

        Overrides ``AgenticConnector.discover`` to first query the Kubernetes
        API for available resource kinds and inject them into the LLM context
        before calling the parent loop.
        """
        cfg: K8sAgenticConfig = config  # type: ignore[assignment]
        available_kinds = await self._enumerate_kinds(cfg, secrets)

        from omniscience_connectors.agentic.llm import build_provider

        agent_cfg = self.__class__.agent_config
        provider = build_provider(agent_cfg)

        context: dict[str, Any] = {"available_kinds": available_kinds}

        for iteration in range(agent_cfg.max_iterations):
            prompt = self._build_discovery_prompt(config, secrets, context)
            logger.debug(
                "k8s_agentic.discover.iteration",
                extra={"iteration": iteration, "kind_count": len(available_kinds)},
            )

            try:
                response = await provider.complete(prompt)
            except Exception as exc:
                logger.warning(
                    "k8s_agentic.discover.llm_error",
                    extra={"iteration": iteration, "error": str(exc)},
                )
                context["last_error"] = str(exc)
                continue

            refs = self._parse_llm_response(response, config)
            if refs:
                for ref in refs:
                    yield ref
                return

            context["last_response"] = response[:500]

        # Fallback
        logger.warning(
            "k8s_agentic.discover.fallback",
            extra={"max_iterations": agent_cfg.max_iterations},
        )
        async for ref in self._default_document_refs(config, secrets):
            yield ref

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch all instances of the resource kind as a JSON document."""
        cfg: K8sAgenticConfig = config  # type: ignore[assignment]
        kind = ref.metadata.get("kind", "")
        token = secrets.get("token") or secrets.get("kubeconfig", "")

        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        path = _kind_to_api_path(kind, cfg.namespace)
        url = f"{cfg.api_server}{path}"

        try:
            async with httpx.AsyncClient(
                verify=cfg.verify_ssl,
                headers=headers,
                timeout=30.0,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"K8s API returned HTTP {exc.response.status_code} for {url}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"K8s fetch request failed for {url}: {exc}") from exc

        return FetchedDocument(
            ref=ref,
            content_bytes=resp.content,
            content_type="application/json",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        """Kubernetes does not support push webhooks; returns None."""
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _enumerate_kinds(
        self,
        cfg: K8sAgenticConfig,
        secrets: dict[str, str],
    ) -> list[str]:
        """Query /api and /apis to enumerate all available resource kinds.

        Returns an empty list on failure (the LLM loop will use defaults).
        """
        token = secrets.get("token") or secrets.get("kubeconfig", "")
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        kinds: set[str] = set()
        try:
            async with httpx.AsyncClient(
                verify=cfg.verify_ssl,
                headers=headers,
                timeout=15.0,
            ) as client:
                # Core API group
                resp = await client.get(f"{cfg.api_server}/api/v1")
                if resp.is_success:
                    data = resp.json()
                    for resource in data.get("resources", []):
                        name: str = resource.get("kind", "")
                        if name and "/" not in resource.get("name", "/"):
                            kinds.add(name)

                # Named API groups
                resp = await client.get(f"{cfg.api_server}/apis")
                if resp.is_success:
                    groups = resp.json().get("groups", [])
                    for group in groups:
                        preferred = group.get("preferredVersion", {})
                        group_version = preferred.get("groupVersion", "")
                        if not group_version:
                            continue
                        gv_resp = await client.get(f"{cfg.api_server}/apis/{group_version}")
                        if gv_resp.is_success:
                            for resource in gv_resp.json().get("resources", []):
                                name = resource.get("kind", "")
                                if name and "/" not in resource.get("name", "/"):
                                    kinds.add(name)

        except Exception as exc:
            logger.warning(
                "k8s_agentic.enumerate_kinds.error",
                extra={"error": str(exc)},
            )
            return []

        return sorted(kinds)


def _kind_to_api_path(kind: str, namespace: str) -> str:
    """Convert a Kubernetes resource kind to a REST API list path.

    This is a best-effort mapping for well-known kinds.  Unknown kinds fall
    back to the lowercase-plural convention under ``/apis/``.

    Args:
        kind: Kubernetes resource kind (e.g. ``"Deployment"``).
        namespace: Namespace filter; empty = all namespaces.

    Returns:
        API server path string (without base URL).
    """
    ns_segment = f"namespaces/{namespace}/" if namespace else ""

    if kind in _KIND_CORE:
        plural = _KIND_CORE[kind]
        if kind in _CORE_CLUSTER_SCOPED:
            return f"/api/v1/{plural}"
        return f"/api/v1/{ns_segment}{plural}"

    if kind in _KIND_APPS:
        plural = _KIND_APPS[kind]
        return f"/apis/apps/v1/{ns_segment}{plural}"

    if kind in _KIND_BATCH:
        plural = _KIND_BATCH[kind]
        return f"/apis/batch/v1/{ns_segment}{plural}"

    if kind in _KIND_NETWORKING:
        plural = _KIND_NETWORKING[kind]
        return f"/apis/networking.k8s.io/v1/{ns_segment}{plural}"

    if kind in _KIND_RBAC:
        plural = _KIND_RBAC[kind]
        if kind in _RBAC_CLUSTER_SCOPED:
            return f"/apis/rbac.authorization.k8s.io/v1/{plural}"
        return f"/apis/rbac.authorization.k8s.io/v1/{ns_segment}{plural}"

    if kind in _KIND_AUTOSCALING:
        plural = _KIND_AUTOSCALING[kind]
        return f"/apis/autoscaling/v2/{ns_segment}{plural}"

    # Unknown kind — best-effort: lowercase plural under /apis/
    plural_fallback = kind.lower() + "s"
    return f"/apis/{ns_segment}{plural_fallback}"
