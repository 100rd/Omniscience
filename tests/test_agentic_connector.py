"""Tests for the AgenticConnector framework and K8s agentic connector.

Coverage:
- AgentConfig model validation (5 tests)
- LLMProvider protocol and OllamaLLMProvider (6 tests)
- build_provider factory (3 tests)
- AgenticConnector base class discovery loop (8 tests)
- K8sAgenticConfig model (3 tests)
- K8sAgenticConnector._parse_llm_response (6 tests)
- K8sAgenticConnector._default_document_refs (2 tests)
- K8sAgenticConnector.discover with mocked LLM (5 tests)
- K8sAgenticConnector.validate (3 tests)
- K8sAgenticConnector.fetch (3 tests)
- Registry integration (2 tests)

Total: 46 tests
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from omniscience_connectors.agentic.base import AgentConfig, AgenticConnector
from omniscience_connectors.agentic.k8s import (
    K8sAgenticConfig,
    K8sAgenticConnector,
    _kind_to_api_path,
)
from omniscience_connectors.agentic.llm import (
    LLMProvider,
    OllamaLLMProvider,
    build_provider,
)
from omniscience_connectors.base import DocumentRef, FetchedDocument
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _SimpleConfig(BaseModel):
    """Minimal config used by the stub agentic connector."""

    source_url: str = "https://example.com"


class _StubAgenticConnector(AgenticConnector):
    """Minimal concrete AgenticConnector for testing the base loop."""

    connector_type: ClassVar[str] = "stub-agentic"
    config_schema: ClassVar[type[BaseModel]] = _SimpleConfig

    agent_config: ClassVar[AgentConfig] = AgentConfig(
        instructions="Stub instructions.",
        model_name="test-model",
        max_iterations=3,
        provider="ollama",
    )

    def _build_discovery_prompt(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        context: dict[str, Any],
    ) -> str:
        return f"Discover things for {config}.  Context: {context}"

    def _parse_llm_response(
        self,
        response: str,
        config: BaseModel,
    ) -> list[DocumentRef]:
        try:
            data = json.loads(response)
            return [
                DocumentRef(
                    external_id=item["id"],
                    uri=item["uri"],
                )
                for item in data.get("refs", [])
            ]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

    async def _default_document_refs(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        yield DocumentRef(external_id="fallback-1", uri="stub://fallback-1")
        yield DocumentRef(external_id="fallback-2", uri="stub://fallback-2")

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        pass

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        return FetchedDocument(
            ref=ref,
            content_bytes=b"stub content",
            content_type="text/plain",
        )


# ---------------------------------------------------------------------------
# 1. AgentConfig model tests
# ---------------------------------------------------------------------------


class TestAgentConfig:
    def test_defaults(self) -> None:
        cfg = AgentConfig()
        assert cfg.model_name == "llama3"
        assert cfg.max_iterations == 3
        assert cfg.provider == "ollama"
        assert isinstance(cfg.instructions, str)
        assert len(cfg.instructions) > 10

    def test_custom_values(self) -> None:
        cfg = AgentConfig(
            instructions="Custom instructions.",
            model_name="mistral",
            max_iterations=5,
            provider="ollama",
        )
        assert cfg.model_name == "mistral"
        assert cfg.max_iterations == 5
        assert cfg.instructions == "Custom instructions."

    def test_max_iterations_minimum(self) -> None:
        cfg = AgentConfig(max_iterations=1)
        assert cfg.max_iterations == 1

    def test_max_iterations_maximum(self) -> None:
        cfg = AgentConfig(max_iterations=20)
        assert cfg.max_iterations == 20

    def test_max_iterations_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(max_iterations=0)

        with pytest.raises(ValidationError):
            AgentConfig(max_iterations=21)


# ---------------------------------------------------------------------------
# 2. LLMProvider protocol and OllamaLLMProvider
# ---------------------------------------------------------------------------


class TestLLMProvider:
    def test_ollama_is_runtime_checkable(self) -> None:
        provider = OllamaLLMProvider(model_name="llama3")
        assert isinstance(provider, LLMProvider)

    def test_ollama_default_base_url(self) -> None:
        provider = OllamaLLMProvider(model_name="llama3")
        assert "localhost:11434" in provider._base_url

    def test_ollama_custom_base_url(self) -> None:
        provider = OllamaLLMProvider(model_name="mistral", base_url="http://custom-host:11434")
        assert "custom-host" in provider._base_url

    async def test_ollama_complete_success(self) -> None:
        provider = OllamaLLMProvider(model_name="llama3")
        fake_response_data = {"model": "llama3", "response": '{"include": ["Deployment"]}'}

        mock_response = MagicMock()
        mock_response.json.return_value = fake_response_data
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await provider.complete("test prompt")

        assert result == '{"include": ["Deployment"]}'

    async def test_ollama_complete_http_error(self) -> None:
        provider = OllamaLLMProvider(model_name="llama3")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.text = "Service Unavailable"
            mock_client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "503", request=MagicMock(), response=mock_response
                )
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="503"):
                await provider.complete("test prompt")

    async def test_ollama_complete_network_error(self) -> None:
        provider = OllamaLLMProvider(model_name="llama3")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="Ollama request failed"):
                await provider.complete("test prompt")


# ---------------------------------------------------------------------------
# 3. build_provider factory
# ---------------------------------------------------------------------------


class TestBuildProvider:
    def test_builds_ollama_provider(self) -> None:
        cfg = AgentConfig(provider="ollama", model_name="llama3")
        provider = build_provider(cfg)
        assert isinstance(provider, OllamaLLMProvider)

    def test_unknown_provider_raises(self) -> None:
        cfg = AgentConfig(provider="unknown-provider")
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            build_provider(cfg)

    def test_provider_uses_configured_model(self) -> None:
        cfg = AgentConfig(provider="ollama", model_name="mistral")
        provider = build_provider(cfg)
        assert isinstance(provider, OllamaLLMProvider)
        assert provider._model_name == "mistral"


# ---------------------------------------------------------------------------
# 4. AgenticConnector base class discovery loop
# ---------------------------------------------------------------------------


class TestAgenticConnectorBase:
    def _make_connector(self) -> _StubAgenticConnector:
        return _StubAgenticConnector()

    def _make_mock_provider(self, responses: list[str]) -> AsyncMock:
        """Build a mock provider that returns responses in order."""
        provider = AsyncMock(spec=LLMProvider)
        provider.complete = AsyncMock(side_effect=responses)
        return provider

    async def _collect(
        self,
        connector: _StubAgenticConnector,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> list[DocumentRef]:
        refs = []
        async for ref in connector.discover(config, secrets):
            refs.append(ref)
        return refs

    async def test_discover_yields_refs_on_first_success(self) -> None:
        connector = self._make_connector()
        config = _SimpleConfig()
        good_response = json.dumps(
            {"refs": [{"id": "a", "uri": "stub://a"}, {"id": "b", "uri": "stub://b"}]}
        )
        mock_provider = self._make_mock_provider([good_response])

        with patch(
            "omniscience_connectors.agentic.llm.build_provider",
            return_value=mock_provider,
        ):
            refs = await self._collect(connector, config, {})

        assert len(refs) == 2
        assert refs[0].external_id == "a"
        assert refs[1].external_id == "b"

    async def test_discover_retries_on_empty_parse(self) -> None:
        connector = self._make_connector()
        config = _SimpleConfig()
        empty_response = "not json at all"
        good_response = json.dumps({"refs": [{"id": "c", "uri": "stub://c"}]})
        mock_provider = self._make_mock_provider([empty_response, good_response])

        with patch(
            "omniscience_connectors.agentic.llm.build_provider",
            return_value=mock_provider,
        ):
            refs = await self._collect(connector, config, {})

        assert len(refs) == 1
        assert refs[0].external_id == "c"
        assert mock_provider.complete.call_count == 2

    async def test_discover_falls_back_when_all_iterations_fail(self) -> None:
        connector = self._make_connector()
        config = _SimpleConfig()
        mock_provider = self._make_mock_provider(
            ["bad", "bad", "bad"]  # 3 bad responses = max_iterations
        )

        with patch(
            "omniscience_connectors.agentic.llm.build_provider",
            return_value=mock_provider,
        ):
            refs = await self._collect(connector, config, {})

        # Falls back to _default_document_refs
        assert len(refs) == 2
        ids = {r.external_id for r in refs}
        assert "fallback-1" in ids
        assert "fallback-2" in ids

    async def test_discover_falls_back_on_llm_error(self) -> None:
        connector = self._make_connector()
        config = _SimpleConfig()
        mock_provider = AsyncMock(spec=LLMProvider)
        mock_provider.complete = AsyncMock(side_effect=RuntimeError("LLM offline"))

        with patch(
            "omniscience_connectors.agentic.llm.build_provider",
            return_value=mock_provider,
        ):
            refs = await self._collect(connector, config, {})

        assert len(refs) == 2
        assert all(r.external_id.startswith("fallback-") for r in refs)

    async def test_discover_stops_after_first_success(self) -> None:
        connector = self._make_connector()
        config = _SimpleConfig()
        good_response = json.dumps({"refs": [{"id": "x", "uri": "stub://x"}]})
        mock_provider = self._make_mock_provider([good_response, good_response, good_response])

        with patch(
            "omniscience_connectors.agentic.llm.build_provider",
            return_value=mock_provider,
        ):
            refs = await self._collect(connector, config, {})

        # Should stop after first successful parse
        assert mock_provider.complete.call_count == 1
        assert len(refs) == 1

    async def test_agent_config_class_var_accessible(self) -> None:
        connector = self._make_connector()
        cfg = connector.__class__.agent_config
        assert isinstance(cfg, AgentConfig)
        assert cfg.model_name == "test-model"

    def test_connector_type_is_set(self) -> None:
        connector = _StubAgenticConnector()
        assert connector.connector_type == "stub-agentic"

    def test_config_schema_is_base_model_subclass(self) -> None:
        assert issubclass(_StubAgenticConnector.config_schema, BaseModel)


# ---------------------------------------------------------------------------
# 5. K8sAgenticConfig model
# ---------------------------------------------------------------------------


class TestK8sAgenticConfig:
    def test_defaults(self) -> None:
        cfg = K8sAgenticConfig()
        assert cfg.cluster_name == "default"
        assert "Deployment" in cfg.default_include_kinds
        assert "Pod" in cfg.default_exclude_kinds
        assert cfg.verify_ssl is True

    def test_custom_config(self) -> None:
        cfg = K8sAgenticConfig(
            cluster_name="prod",
            api_server="https://prod.k8s.example.com",
            namespace="app-namespace",
        )
        assert cfg.cluster_name == "prod"
        assert cfg.namespace == "app-namespace"

    def test_default_include_kinds_mutable_isolation(self) -> None:
        cfg1 = K8sAgenticConfig()
        cfg2 = K8sAgenticConfig()
        cfg1.default_include_kinds.append("CustomKind")
        assert "CustomKind" not in cfg2.default_include_kinds


# ---------------------------------------------------------------------------
# 6. K8sAgenticConnector._parse_llm_response
# ---------------------------------------------------------------------------


class TestK8sParseResponse:
    def _connector(self) -> K8sAgenticConnector:
        return K8sAgenticConnector()

    def _cfg(self) -> K8sAgenticConfig:
        return K8sAgenticConfig(cluster_name="test")

    def test_valid_json_returns_refs(self) -> None:
        connector = self._connector()
        response = json.dumps({"include": ["Deployment", "Service"], "exclude": ["Pod"]})
        refs = connector._parse_llm_response(response, self._cfg())
        kinds = {r.metadata["kind"] for r in refs}
        assert "Deployment" in kinds
        assert "Service" in kinds
        assert "Pod" not in kinds

    def test_excludes_always_excluded_kinds(self) -> None:
        connector = self._connector()
        response = json.dumps({"include": ["Deployment", "Secret", "Event"], "exclude": []})
        refs = connector._parse_llm_response(response, self._cfg())
        kinds = {r.metadata["kind"] for r in refs}
        assert "Secret" not in kinds
        assert "Event" not in kinds
        assert "Deployment" in kinds

    def test_invalid_json_returns_empty(self) -> None:
        connector = self._connector()
        refs = connector._parse_llm_response("not json", self._cfg())
        assert refs == []

    def test_markdown_code_fence_stripped(self) -> None:
        connector = self._connector()
        response = "```json\n" + json.dumps({"include": ["ConfigMap"], "exclude": []}) + "\n```"
        refs = connector._parse_llm_response(response, self._cfg())
        assert len(refs) == 1
        assert refs[0].metadata["kind"] == "ConfigMap"

    def test_empty_include_returns_empty(self) -> None:
        connector = self._connector()
        response = json.dumps({"include": [], "exclude": []})
        refs = connector._parse_llm_response(response, self._cfg())
        assert refs == []

    def test_ref_metadata_contains_cluster_and_kind(self) -> None:
        connector = self._connector()
        cfg = K8sAgenticConfig(cluster_name="staging", namespace="ops")
        response = json.dumps({"include": ["Deployment"], "exclude": []})
        refs = connector._parse_llm_response(response, cfg)
        assert len(refs) == 1
        ref = refs[0]
        assert ref.metadata["cluster"] == "staging"
        assert ref.metadata["kind"] == "Deployment"
        assert ref.metadata["namespace"] == "ops"
        assert ref.metadata["source"] == "llm-driven"
        assert ref.external_id == "k8s:staging:kind:Deployment"
        assert ref.uri == "k8s://staging/Deployment"


# ---------------------------------------------------------------------------
# 7. K8sAgenticConnector._default_document_refs
# ---------------------------------------------------------------------------


class TestK8sDefaultRefs:
    async def test_yields_default_include_kinds(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(cluster_name="fallback-cluster")
        refs = []
        async for ref in connector._default_document_refs(cfg, {}):
            refs.append(ref)

        kinds = {r.metadata["kind"] for r in refs}
        assert "Deployment" in kinds
        assert "Service" in kinds
        # Always-excluded and default-excluded must not appear
        assert "Secret" not in kinds
        assert "Pod" not in kinds

    async def test_fallback_refs_have_fallback_source(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig()
        refs = []
        async for ref in connector._default_document_refs(cfg, {}):
            refs.append(ref)

        assert all(r.metadata["source"] == "default-fallback" for r in refs)
        assert len(refs) > 0


# ---------------------------------------------------------------------------
# 8. K8sAgenticConnector.discover with mocked LLM
# ---------------------------------------------------------------------------


class TestK8sDiscover:
    def _mock_enumerate(self, kinds: list[str]) -> AsyncMock:
        return AsyncMock(return_value=kinds)

    def _mock_provider(self, response: str) -> AsyncMock:
        provider = AsyncMock(spec=LLMProvider)
        provider.complete = AsyncMock(return_value=response)
        return provider

    async def _collect(
        self, connector: K8sAgenticConnector, cfg: K8sAgenticConfig
    ) -> list[DocumentRef]:
        refs = []
        async for ref in connector.discover(cfg, {}):
            refs.append(ref)
        return refs

    async def test_discover_yields_llm_selected_kinds(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(cluster_name="test")
        available = ["Deployment", "Service", "Pod", "Secret"]
        llm_response = json.dumps({"include": ["Deployment", "Service"], "exclude": ["Pod"]})

        with (
            patch.object(connector, "_enumerate_kinds", new=AsyncMock(return_value=available)),
            patch(
                "omniscience_connectors.agentic.llm.build_provider",
                return_value=self._mock_provider(llm_response),
            ),
        ):
            refs = await self._collect(connector, cfg)

        kinds = {r.metadata["kind"] for r in refs}
        assert "Deployment" in kinds
        assert "Service" in kinds
        assert "Pod" not in kinds

    async def test_discover_falls_back_when_llm_fails(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig()

        with (
            patch.object(
                connector,
                "_enumerate_kinds",
                new=AsyncMock(return_value=["Deployment", "Pod"]),
            ),
            patch(
                "omniscience_connectors.agentic.llm.build_provider",
                return_value=self._mock_provider("not json at all"),
            ),
        ):
            refs = await self._collect(connector, cfg)

        assert len(refs) > 0
        # Fallback refs have 'default-fallback' source
        assert all(r.metadata["source"] == "default-fallback" for r in refs)

    async def test_discover_falls_back_when_llm_raises(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig()
        failing_provider = AsyncMock(spec=LLMProvider)
        failing_provider.complete = AsyncMock(side_effect=RuntimeError("Offline"))

        with (
            patch.object(
                connector,
                "_enumerate_kinds",
                new=AsyncMock(return_value=["Deployment"]),
            ),
            patch(
                "omniscience_connectors.agentic.llm.build_provider",
                return_value=failing_provider,
            ),
        ):
            refs = await self._collect(connector, cfg)

        assert len(refs) > 0
        assert all(r.metadata["source"] == "default-fallback" for r in refs)

    async def test_discover_uses_available_kinds_in_prompt(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig()
        available = ["Deployment", "Service", "CustomResource"]
        captured_prompts: list[str] = []

        async def capturing_complete(prompt: str) -> str:
            captured_prompts.append(prompt)
            return json.dumps({"include": ["Deployment"], "exclude": []})

        provider = AsyncMock(spec=LLMProvider)
        provider.complete = capturing_complete

        with (
            patch.object(
                connector,
                "_enumerate_kinds",
                new=AsyncMock(return_value=available),
            ),
            patch(
                "omniscience_connectors.agentic.llm.build_provider",
                return_value=provider,
            ),
        ):
            await self._collect(connector, cfg)

        assert len(captured_prompts) > 0
        assert "CustomResource" in captured_prompts[0]

    async def test_discover_connector_type(self) -> None:
        connector = K8sAgenticConnector()
        assert connector.connector_type == "k8s-agentic"


# ---------------------------------------------------------------------------
# 9. K8sAgenticConnector.validate
# ---------------------------------------------------------------------------


class TestK8sValidate:
    async def test_validate_success(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.test", verify_ssl=False)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.is_success = True

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            await connector.validate(cfg, {"token": "test-token"})

    async def test_validate_http_error_raises(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.test", verify_ssl=False)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.text = "Unauthorized"
            mock_client.get = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "401", request=MagicMock(), response=mock_response
                )
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="401"):
                await connector.validate(cfg, {})

    async def test_validate_network_error_raises(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://unreachable.test", verify_ssl=False)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="Cannot reach"):
                await connector.validate(cfg, {})


# ---------------------------------------------------------------------------
# 10. K8sAgenticConnector.fetch
# ---------------------------------------------------------------------------


class TestK8sFetch:
    def _make_ref(self, kind: str, cluster: str = "test") -> DocumentRef:
        return DocumentRef(
            external_id=f"k8s:{cluster}:kind:{kind}",
            uri=f"k8s://{cluster}/{kind}",
            metadata={"kind": kind, "cluster": cluster, "namespace": ""},
        )

    async def test_fetch_returns_json_document(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.test", verify_ssl=False)
        ref = self._make_ref("Deployment")

        fake_body = json.dumps({"items": [{"metadata": {"name": "my-app"}}]}).encode()
        mock_response = MagicMock()
        mock_response.content = fake_body
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            doc = await connector.fetch(cfg, {"token": "tok"}, ref)

        assert isinstance(doc, FetchedDocument)
        assert doc.content_type == "application/json"
        assert json.loads(doc.content_bytes)["items"][0]["metadata"]["name"] == "my-app"

    async def test_fetch_raises_on_http_error(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.test", verify_ssl=False)
        ref = self._make_ref("Deployment")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_resp = MagicMock()
            mock_resp.status_code = 403
            mock_resp.text = "Forbidden"
            mock_client.get = AsyncMock(
                side_effect=httpx.HTTPStatusError("403", request=MagicMock(), response=mock_resp)
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="403"):
                await connector.fetch(cfg, {}, ref)

    def test_webhook_handler_returns_none(self) -> None:
        connector = K8sAgenticConnector()
        assert connector.webhook_handler() is None


# ---------------------------------------------------------------------------
# 11. _kind_to_api_path helper
# ---------------------------------------------------------------------------


class TestKindToApiPath:
    def test_deployment_namespaced(self) -> None:
        path = _kind_to_api_path("Deployment", "my-ns")
        assert "apps/v1" in path
        assert "namespaces/my-ns" in path
        assert "deployments" in path

    def test_configmap_namespaced(self) -> None:
        path = _kind_to_api_path("ConfigMap", "kube-system")
        assert "/api/v1/" in path
        assert "namespaces/kube-system" in path

    def test_cluster_scoped_no_namespace_segment(self) -> None:
        path = _kind_to_api_path("Namespace", "")
        assert "namespaces/" not in path
        assert "/api/v1/namespaces" in path

    def test_cluster_role_is_cluster_scoped(self) -> None:
        path = _kind_to_api_path("ClusterRole", "my-ns")
        # ClusterRole is cluster-scoped — should not include namespace segment
        assert "namespaces/my-ns" not in path
        assert "clusterroles" in path

    def test_unknown_kind_returns_fallback_path(self) -> None:
        path = _kind_to_api_path("MyCustomResource", "")
        assert "mycustomresources" in path


# ---------------------------------------------------------------------------
# 12. Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    def test_k8s_agentic_registered_in_default_registry(self) -> None:
        from omniscience_connectors import get_connector

        connector = get_connector("k8s-agentic")
        assert isinstance(connector, K8sAgenticConnector)

    def test_k8s_agentic_in_registered_types(self) -> None:
        from omniscience_connectors.registry import _registry

        types = _registry.registered_types()
        assert "k8s-agentic" in types
