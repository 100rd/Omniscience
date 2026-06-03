"""Tests for per-resource granularity and CRD path resolution in K8sAgenticConnector.

Coverage
--------
- _kind_to_api_path: CRD qualified kind (argoproj.io/Application) → correct path
- _kind_to_api_path: bare core/apps/networking kinds unchanged
- _crd_kind_to_api_path: built-in table lookup + heuristic fallback
- _kind_to_resource_path: appends name to collection path
- _resource_to_text: produces non-empty human-readable output
- discover() deterministic mode (use_llm_kind_selection=False):
    (a) one ref per item in list response
    (b) cluster label propagated from config
    (c) per-instance external_id / uri shape
    (d) 403 on one kind skips it, other kinds still yield refs
    (e) 404 on one kind also skips gracefully
    (f) network error on list skips that kind
- fetch() per-instance ref (metadata has "name"):
    (e) returns single-resource text/plain document
    (e) content contains kind + name
- fetch() kind-level ref (no "name" in metadata):
    returns application/json (backward-compatible LLM-path behaviour)
- argoproj.io/Application list yields one ref per Application (integration shape)

Total: 22 tests
"""

from __future__ import annotations

import json
import warnings
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from omniscience_connectors.agentic.k8s import (
        K8sAgenticConfig,
        K8sAgenticConnector,
        _crd_kind_to_api_path,
        _kind_to_api_path,
        _kind_to_resource_path,
        _resource_to_text,
    )
    from omniscience_connectors.base import DocumentRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_k8s_list(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap items in a Kubernetes list envelope."""
    return {"apiVersion": "v1", "kind": "List", "items": items}


def _make_item(
    name: str, namespace: str = "default", labels: dict[str, str] | None = None
) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "namespace": namespace}
    if labels:
        meta["labels"] = labels
    return {"metadata": meta, "spec": {}, "status": {}}


def _make_httpx_responses(*payloads: Any, status: int = 200) -> AsyncMock:
    """Create a mock httpx client whose .get() returns the given payloads in sequence."""
    responses = []
    for payload in payloads:
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.is_success = status < 400
        mock_resp.json = MagicMock(return_value=payload)
        mock_resp.content = json.dumps(payload).encode()
        mock_resp.raise_for_status = MagicMock()
        responses.append(mock_resp)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=responses)
    return mock_client


def _make_error_response(status: int) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.is_success = False
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            str(status), request=MagicMock(), response=MagicMock(status_code=status)
        )
    )
    return mock_resp


async def _collect_discover(
    connector: K8sAgenticConnector,
    cfg: K8sAgenticConfig,
    secrets: dict[str, str] | None = None,
) -> list[DocumentRef]:
    refs: list[DocumentRef] = []
    async for ref in connector.discover(cfg, secrets or {}):
        refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# 1. _kind_to_api_path — CRD qualified kind
# ---------------------------------------------------------------------------


class TestKindToApiPathCRD:
    def test_argoproj_application_list_path(self) -> None:
        path = _kind_to_api_path("argoproj.io/Application", "")
        assert path == "/apis/argoproj.io/v1alpha1/applications"

    def test_argoproj_application_list_path_namespaced(self) -> None:
        path = _kind_to_api_path("argoproj.io/Application", "argocd")
        assert path == "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"

    def test_argoproj_appproject_list_path(self) -> None:
        path = _kind_to_api_path("argoproj.io/AppProject", "")
        assert path == "/apis/argoproj.io/v1alpha1/appprojects"

    def test_argoproj_rollout_list_path(self) -> None:
        path = _kind_to_api_path("argoproj.io/Rollout", "")
        assert path == "/apis/argoproj.io/v1alpha1/rollouts"

    def test_unknown_crd_heuristic_path(self) -> None:
        path = _kind_to_api_path("custom.example.com/Widget", "")
        # heuristic: /apis/<group>/v1/<plural>
        assert "custom.example.com" in path
        assert "widgets" in path

    def test_bare_deployment_unchanged(self) -> None:
        path = _kind_to_api_path("Deployment", "ns")
        assert path == "/apis/apps/v1/namespaces/ns/deployments"

    def test_bare_service_unchanged(self) -> None:
        path = _kind_to_api_path("Service", "")
        assert path == "/api/v1/services"

    def test_bare_namespace_cluster_scoped(self) -> None:
        path = _kind_to_api_path("Namespace", "")
        assert path == "/api/v1/namespaces"


# ---------------------------------------------------------------------------
# 2. _crd_kind_to_api_path
# ---------------------------------------------------------------------------


class TestCrdKindToApiPath:
    def test_table_lookup_no_namespace(self) -> None:
        path = _crd_kind_to_api_path("argoproj.io/Application", "")
        assert path == "/apis/argoproj.io/v1alpha1/applications"

    def test_table_lookup_with_namespace(self) -> None:
        path = _crd_kind_to_api_path("argoproj.io/Application", "argocd")
        assert "namespaces/argocd" in path

    def test_heuristic_unknown_group(self) -> None:
        path = _crd_kind_to_api_path("foo.io/Bar", "")
        assert path == "/apis/foo.io/v1/bars"


# ---------------------------------------------------------------------------
# 3. _kind_to_resource_path
# ---------------------------------------------------------------------------


class TestKindToResourcePath:
    def test_namespaced_resource_path(self) -> None:
        path = _kind_to_resource_path("Deployment", "default", "my-app")
        assert path == "/apis/apps/v1/namespaces/default/deployments/my-app"

    def test_cluster_scoped_resource_path(self) -> None:
        path = _kind_to_resource_path("Namespace", "", "kube-system")
        assert path == "/api/v1/namespaces/kube-system"

    def test_crd_resource_path(self) -> None:
        path = _kind_to_resource_path("argoproj.io/Application", "argocd", "my-app")
        assert path == "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/my-app"


# ---------------------------------------------------------------------------
# 4. _resource_to_text
# ---------------------------------------------------------------------------


class TestResourceToText:
    def test_basic_fields_present(self) -> None:
        resource = {
            "metadata": {"name": "my-app", "namespace": "prod"},
            "spec": {"replicas": 3},
            "status": {"availableReplicas": 3},
        }
        text = _resource_to_text(resource, "Deployment")
        assert "Kind: Deployment" in text
        assert "Name: my-app" in text
        assert "Namespace: prod" in text
        assert "Spec:" in text

    def test_labels_included(self) -> None:
        resource = {
            "metadata": {
                "name": "svc",
                "labels": {"app": "frontend", "env": "prod"},
            }
        }
        text = _resource_to_text(resource, "Service")
        assert "app=frontend" in text
        assert "env=prod" in text

    def test_kubectl_annotations_filtered(self) -> None:
        resource = {
            "metadata": {
                "name": "cfg",
                "annotations": {
                    "kubectl.kubernetes.io/last-applied-configuration": "{}",
                    "my.company.io/owner": "team-a",
                },
            }
        }
        text = _resource_to_text(resource, "ConfigMap")
        assert "kubectl.kubernetes.io" not in text
        assert "my.company.io/owner=team-a" in text

    def test_empty_resource_does_not_raise(self) -> None:
        text = _resource_to_text({}, "Unknown")
        assert "Kind: Unknown" in text


# ---------------------------------------------------------------------------
# 5. discover() deterministic mode — per-resource refs
# ---------------------------------------------------------------------------


class TestDeterministicDiscoverPerResource:
    async def test_one_ref_per_item(self) -> None:
        """(a) One DocumentRef per item in the list response."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="qbiq-shared",
            use_llm_kind_selection=False,
            default_include_kinds=["Service"],
            default_exclude_kinds=[],
        )
        svc_list = _make_k8s_list(
            [
                _make_item("svc-a", "default"),
                _make_item("svc-b", "kube-system"),
            ]
        )
        mock_client = _make_httpx_responses(svc_list)

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert len(refs) == 2
        names = {r.metadata["name"] for r in refs}
        assert names == {"svc-a", "svc-b"}

    async def test_cluster_label_propagated(self) -> None:
        """(b) cluster metadata comes from config.cluster_name."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="qbiq-shared",
            use_llm_kind_selection=False,
            default_include_kinds=["Deployment"],
            default_exclude_kinds=[],
        )
        dep_list = _make_k8s_list([_make_item("api-server", "default")])
        mock_client = _make_httpx_responses(dep_list)

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert all(r.metadata["cluster"] == "qbiq-shared" for r in refs)

    async def test_external_id_and_uri_shape(self) -> None:
        """(c) external_id = k8s:{cluster}:{ns}:{kind}:{name}, uri = k8s://...."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="prod",
            use_llm_kind_selection=False,
            default_include_kinds=["Service"],
            default_exclude_kinds=[],
        )
        svc_list = _make_k8s_list([_make_item("my-svc", "web")])
        mock_client = _make_httpx_responses(svc_list)

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert len(refs) == 1
        ref = refs[0]
        assert ref.external_id == "k8s:prod:web:Service:my-svc"
        assert ref.uri == "k8s://prod/web/Service/my-svc"

    async def test_403_on_one_kind_skips_it_but_others_yield(self) -> None:
        """(d) 403 on one kind skips it; other kinds still produce refs."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="shared",
            use_llm_kind_selection=False,
            default_include_kinds=["Deployment", "argoproj.io/Application"],
            default_exclude_kinds=[],
        )
        dep_list = _make_k8s_list([_make_item("api", "default")])

        # First call (Deployment) succeeds; second call (Application) returns 403
        forbidden_resp = MagicMock()
        forbidden_resp.status_code = 403
        forbidden_resp.is_success = False
        forbidden_resp.raise_for_status = MagicMock()

        dep_client = AsyncMock()
        dep_client.__aenter__ = AsyncMock(return_value=dep_client)
        dep_client.__aexit__ = AsyncMock(return_value=None)
        dep_client.get = AsyncMock(
            side_effect=[
                MagicMock(
                    status_code=200,
                    is_success=True,
                    raise_for_status=MagicMock(),
                    json=MagicMock(return_value=dep_list),
                    content=json.dumps(dep_list).encode(),
                ),
                forbidden_resp,
            ]
        )

        with patch("httpx.AsyncClient", return_value=dep_client):
            refs = await _collect_discover(connector, cfg)

        # Only the Deployment ref should be present
        assert len(refs) == 1
        assert refs[0].metadata["kind"] == "Deployment"

    async def test_404_on_kind_also_skips(self) -> None:
        """(d variant) 404 skips the kind gracefully."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="shared",
            use_llm_kind_selection=False,
            default_include_kinds=["Ingress"],
            default_exclude_kinds=[],
        )
        not_found = MagicMock()
        not_found.status_code = 404
        not_found.is_success = False
        not_found.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=not_found)

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert refs == []

    async def test_network_error_on_list_skips_kind(self) -> None:
        """(d variant) RequestError during list skips the kind."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="shared",
            use_llm_kind_selection=False,
            default_include_kinds=["ConfigMap"],
            default_exclude_kinds=[],
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert refs == []

    async def test_metadata_source_is_deterministic(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="cl",
            use_llm_kind_selection=False,
            default_include_kinds=["Service"],
            default_exclude_kinds=[],
        )
        svc_list = _make_k8s_list([_make_item("s1", "ns1"), _make_item("s2", "ns2")])
        mock_client = _make_httpx_responses(svc_list)

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert all(r.metadata["source"] == "deterministic" for r in refs)


# ---------------------------------------------------------------------------
# 6. fetch() — per-instance ref returns text/plain
# ---------------------------------------------------------------------------


class TestFetchPerInstanceRef:
    def _make_per_instance_ref(
        self,
        kind: str = "Deployment",
        name: str = "api-server",
        namespace: str = "default",
        cluster: str = "prod",
    ) -> DocumentRef:
        return DocumentRef(
            external_id=f"k8s:{cluster}:{namespace}:{kind}:{name}",
            uri=f"k8s://{cluster}/{namespace}/{kind}/{name}",
            metadata={
                "kind": kind,
                "cluster": cluster,
                "namespace": namespace,
                "name": name,
                "source": "deterministic",
            },
        )

    async def test_fetch_per_instance_ref_returns_text_plain(self) -> None:
        """(e) fetch with name in metadata returns text/plain."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.test", verify_ssl=False)
        ref = self._make_per_instance_ref()

        resource_body = {
            "metadata": {"name": "api-server", "namespace": "default"},
            "spec": {"replicas": 2},
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=resource_body)
        mock_resp.content = json.dumps(resource_body).encode()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            doc = await connector.fetch(cfg, {}, ref)

        assert doc.content_type == "text/plain"

    async def test_fetch_per_instance_text_contains_kind_and_name(self) -> None:
        """(e) text body mentions kind and name."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.test", verify_ssl=False)
        ref = self._make_per_instance_ref(kind="Service", name="my-service")

        resource_body = {
            "metadata": {"name": "my-service", "namespace": "default"},
            "spec": {"type": "ClusterIP"},
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=resource_body)
        mock_resp.content = json.dumps(resource_body).encode()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            doc = await connector.fetch(cfg, {}, ref)

        text = doc.content_bytes.decode()
        assert "Service" in text
        assert "my-service" in text


# ---------------------------------------------------------------------------
# 7. fetch() — kind-level (LLM path) ref returns application/json
# ---------------------------------------------------------------------------


class TestFetchKindLevelRef:
    async def test_fetch_kind_level_ref_returns_json(self) -> None:
        """Backward compat: no 'name' in metadata → raw JSON collection."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.test", verify_ssl=False)
        ref = DocumentRef(
            external_id="k8s:prod:kind:Deployment",
            uri="k8s://prod/Deployment",
            metadata={"kind": "Deployment", "cluster": "prod", "namespace": ""},
        )
        collection = {"items": [{"metadata": {"name": "x"}}]}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=collection)
        mock_resp.content = json.dumps(collection).encode()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            doc = await connector.fetch(cfg, {}, ref)

        assert doc.content_type == "application/json"
        assert json.loads(doc.content_bytes)["items"][0]["metadata"]["name"] == "x"


# ---------------------------------------------------------------------------
# 8. argoproj.io/Application yields one ref per Application
# ---------------------------------------------------------------------------


class TestArgoprojApplicationDiscover:
    async def test_argo_applications_yield_one_ref_each(self) -> None:
        """One DocumentRef per ArgoCD Application in the list response."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="qbiq-shared",
            use_llm_kind_selection=False,
            default_include_kinds=["argoproj.io/Application"],
            default_exclude_kinds=[],
        )
        app_list = _make_k8s_list(
            [
                _make_item("guestbook", "argocd"),
                _make_item("monitoring", "argocd"),
                _make_item("ingress-nginx", "argocd"),
            ]
        )
        mock_client = _make_httpx_responses(app_list)

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert len(refs) == 3
        names = {r.metadata["name"] for r in refs}
        assert names == {"guestbook", "monitoring", "ingress-nginx"}

    async def test_argo_application_external_id_shape(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="qbiq-shared",
            use_llm_kind_selection=False,
            default_include_kinds=["argoproj.io/Application"],
            default_exclude_kinds=[],
        )
        app_list = _make_k8s_list([_make_item("my-app", "argocd")])
        mock_client = _make_httpx_responses(app_list)

        with patch("httpx.AsyncClient", return_value=mock_client):
            refs = await _collect_discover(connector, cfg)

        assert len(refs) == 1
        ref = refs[0]
        assert ref.metadata["kind"] == "Application"
        assert ref.metadata["cluster"] == "qbiq-shared"
        assert ref.external_id == "k8s:qbiq-shared:argocd:Application:my-app"
        assert ref.uri == "k8s://qbiq-shared/argocd/Application/my-app"

    async def test_argo_list_url_hits_correct_endpoint(self) -> None:
        """Verify the HTTP GET is made to the argoproj.io/v1alpha1 URL."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="qbiq-shared",
            api_server="https://k8s.example.com",
            use_llm_kind_selection=False,
            default_include_kinds=["argoproj.io/Application"],
            default_exclude_kinds=[],
        )
        app_list = _make_k8s_list([_make_item("app1", "argocd")])

        captured_urls: list[str] = []

        async def fake_get(url: str, **kwargs: Any) -> Any:
            captured_urls.append(url)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.is_success = True
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value=app_list)
            mock_resp.content = json.dumps(app_list).encode()
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = fake_get

        with patch("httpx.AsyncClient", return_value=mock_client):
            await _collect_discover(connector, cfg)

        assert len(captured_urls) == 1
        assert captured_urls[0] == "https://k8s.example.com/apis/argoproj.io/v1alpha1/applications"
