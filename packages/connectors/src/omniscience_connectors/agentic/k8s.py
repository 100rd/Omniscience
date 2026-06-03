"""Kubernetes agentic connector — DEPRECATED in v0.3, scheduled for removal in v0.5.

.. deprecated:: 0.3.0
    This connector is superseded by ``omniscience-operator`` — the in-cluster
    Go operator at ``operator/`` of this repository.  Both paths run in
    parallel through v0.3 with server-side dedup (issue #164, ADR-0010)
    making the operator authoritative for kinds it covers.  The connector
    becomes opt-in default-disabled in v0.4 and is removed in v0.5.

    See the migration guide at
    ``docs/connectors/k8s-agentic-deprecation.md`` and the parity matrix at
    ``docs/connectors/k8s-agentic-vs-operator-parity.md``.

    Importing this module raises a :class:`DeprecationWarning` on first
    import.  The warning is informational only — the connector remains
    fully functional in v0.3.

Uses an LLM agent to decide which Kubernetes resource kinds to index.
The LLM is given the list of available API resource kinds in the cluster and
asked to return a JSON decision about which to include and which to exclude.

Config
------
``K8sAgenticConfig``:
    - ``cluster_name`` — human-readable label stored in document metadata.
    - ``api_server`` — Kubernetes API server URL (e.g. ``https://k8s.example.com``).
    - ``namespace`` — namespace to scope discovery; empty = all namespaces.
    - ``default_include_kinds`` — fallback list used when LLM fails (and the
      sole source of kinds when ``use_llm_kind_selection=False``).  A kind may
      be expressed as a bare ``"Kind"`` (looked up in the built-in tables) or as
      ``"group/Kind"`` (e.g. ``"argoproj.io/Application"``) which resolves to
      ``/apis/<group>/<version>/<plural>``.
    - ``default_exclude_kinds`` — kinds always excluded regardless of LLM output.
    - ``verify_ssl`` — ``True`` (default) verifies with the system CA bundle;
      ``False`` disables verification (insecure escape hatch).  Ignored when a
      CA is supplied — the CA always takes precedence.
    - ``ca_cert_pem`` — PEM-encoded cluster CA certificate (string).  When set,
      TLS verification uses this CA instead of the system bundle.
    - ``ca_cert_b64`` — Base64-encoded PEM certificate.  Decoded and used the
      same way as ``ca_cert_pem``.  ``ca_cert_pem`` takes precedence if both
      are provided.
    - ``use_llm_kind_selection`` — when ``False``, ``discover()`` skips the LLM
      entirely and yields ONE ref PER RESOURCE INSTANCE (deterministic,
      per-resource granularity, no LLM dependency).

Secrets
-------
``kubeconfig`` or ``token`` (service-account JWT).  Passed via the secrets dict.
The CA certificate may also be supplied as ``ca_cert_pem`` or ``ca_cert_b64``
in the secrets dict; that takes precedence over the config fields.

Discovery flow
--------------
When ``use_llm_kind_selection=True`` (default):
1. Query ``/api`` and ``/apis`` to enumerate available resource kinds.
2. Build a prompt listing the kinds and asking the LLM which to index.
3. Parse the LLM's JSON response (``{"include": [...], "exclude": [...]}``) into
   ``DocumentRef`` objects — one per (kind, namespace/resource name) pair.
4. Fall back to ``default_include_kinds`` minus ``default_exclude_kinds`` on
   parse failure.

When ``use_llm_kind_selection=False``:
1. For each kind in ``default_include_kinds`` minus ``default_exclude_kinds`` and
   ``_ALWAYS_EXCLUDE``, list all instances via the Kubernetes API.
2. Yield ONE ``DocumentRef`` per instance with metadata
   ``{kind, cluster, namespace, name}``, a stable ``external_id``
   ``k8s:{cluster}:{namespace}:{kind}:{name}``, and ``uri``
   ``k8s://{cluster}/{namespace}/{kind}/{name}``.
3. If the SA cannot list a kind (HTTP 403 / 404), log the error and skip that
   kind — enumeration of the other kinds continues.

``fetch()`` behaviour:
- When the ref has a ``name`` key in metadata (per-resource ref from deterministic
  discover), fetches that single resource and returns a concise human-readable
  text body so embeddings are meaningful.
- When the ref has no ``name`` key (LLM-path / legacy kind-level ref), falls
  back to the previous list-all behaviour (returns raw JSON of the collection).

Note: ``fetch()`` retrieves the resource as JSON and stores it as
``application/json`` bytes.  The downstream pipeline is responsible for further
parsing.
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import warnings
from collections.abc import AsyncIterator
from typing import Any, ClassVar, Final

import httpx
from pydantic import BaseModel, Field

from omniscience_connectors.agentic.base import AgentConfig, AgenticConnector
from omniscience_connectors.base import DocumentRef, FetchedDocument, WebhookHandler

__all__ = ["K8sAgenticConfig", "K8sAgenticConnector", "build_ssl_verify"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deprecation announcement (issue #168, ADR-0011)
# ---------------------------------------------------------------------------
#
# This connector is deprecated as of v0.3.  The replacement is the
# ``omniscience-operator`` co-located at ``operator/`` of this repo.
# Removal is scheduled for v0.5 per docs/decisions/0011-k8s-agentic-deprecation-schedule.md.
#
# We emit a single ``DeprecationWarning`` at module import time.  Exactly-once
# semantics are achieved via Python's default warning filter
# (``"default"`` action) plus the ``stacklevel=2`` argument so the warning
# is attributed to the caller's import site, not to this module's own line.
#
# The warning's message is the load-bearing signal:
#   * it carries the removal version string ``v0.5``
#   * it carries the migration target name ``omniscience-operator``
#   * it carries a relative URL to the migration guide
# All three are asserted by ``tests/test_agentic_deprecation.py`` so a
# wording change cannot regress the contract.
_DEPRECATION_REMOVAL_VERSION: Final[str] = "v0.5.0"
_DEPRECATION_MIGRATION_TARGET: Final[str] = "omniscience-operator"
_DEPRECATION_MIGRATION_GUIDE_URL: Final[str] = (
    "https://github.com/100rd/Omniscience/blob/main/docs/connectors/k8s-agentic-deprecation.md"
)

DEPRECATION_MESSAGE: Final[str] = (
    "K8sAgenticConnector is deprecated as of Omniscience v0.3 and will be "
    f"removed in {_DEPRECATION_REMOVAL_VERSION}.  "
    f"Migrate to {_DEPRECATION_MIGRATION_TARGET} (the in-cluster Go operator "
    "at operator/ of this repo).  "
    f"Migration guide: {_DEPRECATION_MIGRATION_GUIDE_URL}"
)

warnings.warn(
    DEPRECATION_MESSAGE,
    DeprecationWarning,
    stacklevel=2,
)

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
    # ArgoCD CRD — expressed as "group/Kind" so the path resolver handles it.
    "argoproj.io/Application",
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

# CRD kinds expressed as "group/Kind" → (group, version, plural).
# The resolver tries this table first before falling back to heuristics.
_KIND_CRD: dict[str, tuple[str, str, str]] = {
    "argoproj.io/Application": ("argoproj.io", "v1alpha1", "applications"),
    "argoproj.io/AppProject": ("argoproj.io", "v1alpha1", "appprojects"),
    "argoproj.io/ApplicationSet": ("argoproj.io", "v1alpha1", "applicationsets"),
    "argoproj.io/Rollout": ("argoproj.io", "v1alpha1", "rollouts"),
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
        description=(
            "Fallback list of resource kinds used when the LLM fails, "
            "or the complete kind list when use_llm_kind_selection=False. "
            "A kind may be a bare name (e.g. 'Deployment') or a 'group/Kind' "
            "qualified name (e.g. 'argoproj.io/Application')."
        ),
    )

    default_exclude_kinds: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_EXCLUDE_KINDS),
        description="Resource kinds always excluded (merged with LLM exclusions).",
    )

    verify_ssl: bool = Field(
        default=True,
        description=(
            "Whether to verify TLS certificates when calling the API server. "
            "Set to False only as an explicit insecure escape hatch. "
            "Ignored when ca_cert_pem or ca_cert_b64 is provided."
        ),
    )

    ca_cert_pem: str | None = Field(
        default=None,
        description=(
            "PEM-encoded CA certificate used to verify the API server TLS. "
            "Takes precedence over ca_cert_b64 and verify_ssl."
        ),
    )

    ca_cert_b64: str | None = Field(
        default=None,
        description=(
            "Base64-encoded PEM CA certificate. Decoded and treated identically "
            "to ca_cert_pem. ca_cert_pem takes precedence if both are set."
        ),
    )

    use_llm_kind_selection: bool = Field(
        default=True,
        description=(
            "When True (default), the LLM selects which resource kinds to index. "
            "When False, discover() skips the LLM entirely and enumerates "
            "individual resource instances from default_include_kinds minus "
            "default_exclude_kinds (deterministic, per-resource granularity)."
        ),
    )


# ---------------------------------------------------------------------------
# TLS helper
# ---------------------------------------------------------------------------


def build_ssl_verify(
    cfg: K8sAgenticConfig,
    secrets: dict[str, str],
) -> ssl.SSLContext | bool:
    """Return the correct ``verify=`` value for an httpx client.

    Resolution order:
    1. ``secrets["ca_cert_pem"]`` (runtime-injected PEM string)
    2. ``secrets["ca_cert_b64"]`` (runtime-injected base64 PEM)
    3. ``cfg.ca_cert_pem``
    4. ``cfg.ca_cert_b64``
    5. ``cfg.verify_ssl`` (``True`` = system CA bundle, ``False`` = insecure)

    When a PEM is resolved, an :class:`ssl.SSLContext` is built via
    ``ssl.create_default_context(cadata=pem)`` so httpx uses the cluster CA
    without needing a temp file on disk.

    Args:
        cfg: Validated K8sAgenticConfig.
        secrets: Runtime secrets dict (may contain ``ca_cert_pem``/``ca_cert_b64``).

    Returns:
        An :class:`ssl.SSLContext` when a CA PEM is available,
        or a bool (``True``/``False``) otherwise.
    """
    pem: str | None = (
        secrets.get("ca_cert_pem")
        or (secrets.get("ca_cert_b64") and _decode_b64_pem(secrets["ca_cert_b64"]))
        or cfg.ca_cert_pem
        or (cfg.ca_cert_b64 and _decode_b64_pem(cfg.ca_cert_b64))
        or None
    )
    if pem:
        ctx = ssl.create_default_context(cadata=pem)
        return ctx
    return cfg.verify_ssl


def _decode_b64_pem(b64_value: str) -> str:
    """Decode a base64-encoded PEM string and return the decoded text.

    Args:
        b64_value: Base64-encoded PEM certificate bytes.

    Returns:
        Decoded PEM string (UTF-8).

    Raises:
        ValueError: If the value is not valid base64.
    """
    try:
        return base64.b64decode(b64_value).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"ca_cert_b64 is not valid base64: {exc}") from exc


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

    When ``config.use_llm_kind_selection=False``, the LLM is never called;
    ``default_include_kinds`` is used to enumerate individual resource instances
    directly (deterministic per-resource mode).
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
        verify = build_ssl_verify(cfg, secrets)

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(
                verify=verify,
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
        """Yield refs for the configured default include kinds.

        Used as the LLM fallback path (kind-level refs, not per-instance).
        The deterministic discover path calls ``_list_kind_instances`` instead.
        """
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
        """Enumerate resource instances (deterministic) or run the LLM loop.

        When ``config.use_llm_kind_selection`` is ``False``, the LLM is never
        called; for each kind in ``default_include_kinds`` minus exclusions, all
        instances are listed and one ``DocumentRef`` is yielded per instance.
        A kind that the SA cannot access (403/404) is skipped with a warning.

        Otherwise, overrides ``AgenticConnector.discover`` to first query the
        Kubernetes API for available resource kinds and inject them into the LLM
        context before calling the parent loop.
        """
        cfg: K8sAgenticConfig = config  # type: ignore[assignment]

        # ------------------------------------------------------------------
        # Fast path: deterministic mode — per-resource enumeration, no LLM
        # ------------------------------------------------------------------
        if not cfg.use_llm_kind_selection:
            logger.info("k8s_agentic.discover.deterministic_mode")
            effective_exclude = _ALWAYS_EXCLUDE | set(cfg.default_exclude_kinds)
            active_kinds = sorted(
                k for k in cfg.default_include_kinds if k not in effective_exclude
            )
            for kind in active_kinds:
                async for ref in self._list_kind_instances(cfg, secrets, kind):
                    yield ref
            return

        # ------------------------------------------------------------------
        # LLM path: enumerate available kinds, then run the LLM loop
        # ------------------------------------------------------------------
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
        """Fetch a single resource (per-instance ref) or a kind collection.

        When ``ref.metadata`` contains a ``"name"`` key (set by the deterministic
        discover path), fetches that individual resource and returns a concise
        human-readable text body suitable for embedding.

        When ``ref.metadata`` has no ``"name"`` key (LLM-path / legacy kind-level
        ref), falls back to the previous behaviour of listing all instances and
        returning the raw JSON collection.
        """
        cfg: K8sAgenticConfig = config  # type: ignore[assignment]
        kind = ref.metadata.get("kind", "")
        token = secrets.get("token") or secrets.get("kubeconfig", "")
        verify = build_ssl_verify(cfg, secrets)

        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        name = ref.metadata.get("name", "")
        namespace = ref.metadata.get("namespace", cfg.namespace)

        if name:
            path = _kind_to_resource_path(kind, namespace, name)
        else:
            path = _kind_to_api_path(kind, cfg.namespace)

        url = f"{cfg.api_server}{path}"

        try:
            async with httpx.AsyncClient(
                verify=verify,
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

        if name:
            resource = resp.json()
            text = _resource_to_text(resource, kind)
            return FetchedDocument(
                ref=ref,
                content_bytes=text.encode("utf-8"),
                content_type="text/plain",
            )

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
        verify = build_ssl_verify(cfg, secrets)
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        kinds: set[str] = set()
        try:
            async with httpx.AsyncClient(
                verify=verify,
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

    async def _list_kind_instances(
        self,
        cfg: K8sAgenticConfig,
        secrets: dict[str, str],
        kind: str,
    ) -> AsyncIterator[DocumentRef]:
        """List all instances of ``kind`` and yield one ``DocumentRef`` per item.

        Gracefully skips the kind and logs a warning on HTTP 403/404 so
        enumeration of the remaining kinds continues uninterrupted.

        Args:
            cfg: Validated connector config.
            secrets: Runtime secrets.
            kind: Kind name — may be bare (``"Deployment"``) or qualified
                (``"argoproj.io/Application"``).

        Yields:
            One :class:`DocumentRef` per resource instance.
        """
        token = secrets.get("token") or secrets.get("kubeconfig", "")
        verify = build_ssl_verify(cfg, secrets)
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        list_path = _kind_to_api_path(kind, cfg.namespace)
        url = f"{cfg.api_server}{list_path}"

        try:
            async with httpx.AsyncClient(
                verify=verify,
                headers=headers,
                timeout=30.0,
            ) as client:
                resp = await client.get(url)
        except httpx.RequestError as exc:
            logger.warning(
                "k8s_agentic.list_instances.request_error",
                extra={"kind": kind, "url": url, "error": str(exc)},
            )
            return

        if resp.status_code in (403, 404):
            logger.warning(
                "k8s_agentic.list_instances.skipped",
                extra={"kind": kind, "status": resp.status_code},
            )
            return

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "k8s_agentic.list_instances.http_error",
                extra={"kind": kind, "status": exc.response.status_code},
            )
            return

        try:
            data = resp.json()
            items: list[dict[str, Any]] = data.get("items", [])
        except Exception as exc:
            logger.warning(
                "k8s_agentic.list_instances.parse_error",
                extra={"kind": kind, "error": str(exc)},
            )
            return

        # Extract bare kind name for metadata (strip group prefix if qualified)
        bare_kind = kind.split("/", 1)[1] if "/" in kind else kind

        for item in items:
            meta: dict[str, Any] = item.get("metadata", {})
            name: str = meta.get("name", "")
            namespace: str = meta.get("namespace", "")
            if not name:
                continue

            external_id = f"k8s:{cfg.cluster_name}:{namespace}:{bare_kind}:{name}"
            uri = f"k8s://{cfg.cluster_name}/{namespace}/{bare_kind}/{name}"
            yield DocumentRef(
                external_id=external_id,
                uri=uri,
                metadata={
                    "kind": bare_kind,
                    "cluster": cfg.cluster_name,
                    "namespace": namespace,
                    "name": name,
                    "source": "deterministic",
                },
            )


def _kind_to_api_path(kind: str, namespace: str) -> str:
    """Convert a Kubernetes resource kind to a REST API list path.

    Supports bare kind names (e.g. ``"Deployment"``) and qualified
    ``"group/Kind"`` CRD names (e.g. ``"argoproj.io/Application"``).

    For CRD kinds not in the built-in ``_KIND_CRD`` table, falls back to a
    heuristic: ``/apis/<group>/<version>/<plural>`` where version defaults to
    ``v1`` and plural is the lowercased kind with an ``s`` suffix.

    Args:
        kind: Kubernetes resource kind.  May be ``"Kind"`` or ``"group/Kind"``.
        namespace: Namespace filter; empty = all namespaces.

    Returns:
        API server path string (without base URL).
    """
    # Handle qualified CRD kinds first ("group/Kind")
    if "/" in kind:
        return _crd_kind_to_api_path(kind, namespace)

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

    # Unknown bare kind — best-effort: lowercase plural under /apis/
    plural_fallback = kind.lower() + "s"
    return f"/apis/{ns_segment}{plural_fallback}"


def _crd_kind_to_api_path(qualified_kind: str, namespace: str) -> str:
    """Resolve a ``"group/Kind"`` CRD spec to its REST list path.

    Checks the built-in ``_KIND_CRD`` table first.  Falls back to a heuristic
    for unknown CRDs: ``/apis/<group>/v1/<plural>`` where plural is the
    lowercase kind name with an ``s`` suffix.

    Args:
        qualified_kind: CRD kind string in ``"group/Kind"`` format.
        namespace: Namespace filter; empty = all namespaces (cluster-scoped CRDs
            also receive ``""`` here and produce a path without a namespace
            segment).

    Returns:
        API server list path (without base URL).
    """
    ns_segment = f"namespaces/{namespace}/" if namespace else ""

    if qualified_kind in _KIND_CRD:
        group, version, plural = _KIND_CRD[qualified_kind]
        return f"/apis/{group}/{version}/{ns_segment}{plural}"

    # Heuristic for unknown CRD groups
    group, raw_kind = qualified_kind.split("/", 1)
    plural = raw_kind.lower() + "s"
    return f"/apis/{group}/v1/{ns_segment}{plural}"


def _kind_to_resource_path(kind: str, namespace: str, name: str) -> str:
    """Build the path for fetching a single named resource.

    This is the single-resource equivalent of ``_kind_to_api_path``:
    appends ``/<name>`` to the namespaced or cluster-scoped collection path.

    Args:
        kind: Resource kind (bare or qualified).
        namespace: Namespace the resource lives in (may be empty for
            cluster-scoped resources).
        name: Resource name.

    Returns:
        API server path string for the individual resource (without base URL).
    """
    collection_path = _kind_to_api_path(kind, namespace)
    return f"{collection_path}/{name}"


def _resource_to_text(resource: dict[str, Any], kind: str) -> str:
    """Produce a concise human-readable summary of a Kubernetes resource.

    The text is intentionally terse — just enough signal for embeddings to be
    meaningful without carrying the full YAML verbatim (which bloats vectors).

    Args:
        resource: Parsed JSON of a single Kubernetes resource.
        kind: Resource kind (used as a label in the output).

    Returns:
        Plain-text summary string.
    """
    meta: dict[str, Any] = resource.get("metadata", {})
    name: str = meta.get("name", "<unknown>")
    namespace: str = meta.get("namespace", "")
    labels: dict[str, str] = meta.get("labels", {})
    annotations: dict[str, str] = meta.get("annotations", {})

    lines: list[str] = [
        f"Kind: {kind}",
        f"Name: {name}",
    ]
    if namespace:
        lines.append(f"Namespace: {namespace}")
    if labels:
        label_str = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
        lines.append(f"Labels: {label_str}")
    if annotations:
        # Include only non-system annotations (skip kubectl.kubernetes.io/* noise)
        user_annotations = {
            k: v for k, v in annotations.items() if not k.startswith("kubectl.kubernetes.io/")
        }
        if user_annotations:
            ann_str = ", ".join(f"{k}={v}" for k, v in sorted(user_annotations.items()))
            lines.append(f"Annotations: {ann_str}")

    spec: dict[str, Any] = resource.get("spec", {})
    if spec:
        # Include a JSON snippet of spec capped at 500 chars to stay concise
        spec_snippet = json.dumps(spec, separators=(",", ":"))[:500]
        lines.append(f"Spec: {spec_snippet}")

    status: dict[str, Any] = resource.get("status", {})
    if status:
        status_snippet = json.dumps(status, separators=(",", ":"))[:200]
        lines.append(f"Status: {status_snippet}")

    return "\n".join(lines)
