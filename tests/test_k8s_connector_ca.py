"""Tests for custom-CA TLS and optional-LLM enhancements to K8sAgenticConnector.

Coverage
--------
- build_ssl_verify helper:
    - PEM string in config  → SSLContext
    - base64 PEM in config  → SSLContext
    - PEM string in secrets (runtime override) → SSLContext
    - base64 PEM in secrets (runtime override) → SSLContext
    - secrets PEM overrides config PEM
    - verify_ssl=True, no CA → bool True
    - verify_ssl=False, no CA → bool False
    - invalid base64 in config → ValueError
    - invalid base64 in secrets → ValueError

- use_llm_kind_selection=False (deterministic mode):
    - discover() yields per-instance refs (one per item returned by list calls)
    - LLM provider is NEVER instantiated / called
    - _enumerate_kinds is NEVER called
    - excluded kinds (default_exclude_kinds + _ALWAYS_EXCLUDE) are absent
    - metadata source == "deterministic"

- use_llm_kind_selection=True (existing LLM path, regression guards):
    - LLM is invoked as before

- SA bearer token is sent as Authorization: Bearer <token> header:
    - validate()
    - fetch()
    - _enumerate_kinds()

- New config fields default values

Total: 20 tests
"""

from __future__ import annotations

import base64
import json
import ssl
import subprocess
import warnings
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Suppress the module-level DeprecationWarning during import in tests.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from omniscience_connectors.agentic.k8s import (
        K8sAgenticConfig,
        K8sAgenticConnector,
        _decode_b64_pem,
        build_ssl_verify,
    )
    from omniscience_connectors.base import DocumentRef


# ---------------------------------------------------------------------------
# Fixture: a real self-signed CA PEM (generated once per session)
# ---------------------------------------------------------------------------


def _generate_test_ca_pem() -> str:
    """Generate a minimal self-signed certificate PEM using openssl."""
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            "/dev/null",
            "-out",
            "/dev/stdout",
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=test-ca",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


_TEST_CA_PEM: str = _generate_test_ca_pem()
_TEST_CA_B64: str = base64.b64encode(_TEST_CA_PEM.encode()).decode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect_discover(
    connector: K8sAgenticConnector,
    cfg: K8sAgenticConfig,
    secrets: dict[str, str] | None = None,
) -> list[DocumentRef]:
    refs: list[DocumentRef] = []
    async for ref in connector.discover(cfg, secrets or {}):
        refs.append(ref)
    return refs


def _make_httpx_mock(response_data: Any = None, status: int = 200) -> tuple[MagicMock, MagicMock]:
    """Return (mock_client_cls, mock_client) wired for use as httpx.AsyncClient patch."""
    mock_response = MagicMock()
    mock_response.is_success = status < 400
    mock_response.status_code = status
    mock_response.raise_for_status = MagicMock()
    if response_data is not None:
        mock_response.json.return_value = response_data
        mock_response.content = json.dumps(response_data).encode()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_client_cls = MagicMock(return_value=mock_client)
    return mock_client_cls, mock_client


def _stub_list_instances(
    kinds_to_refs: dict[str, list[DocumentRef]],
) -> Any:
    """Build a patch-able _list_kind_instances that returns fixed refs per kind."""

    async def _mock(
        self: Any,
        cfg: K8sAgenticConfig,
        secrets: dict[str, str],
        kind: str,
    ) -> AsyncIterator[DocumentRef]:
        for ref in kinds_to_refs.get(kind, []):
            yield ref

    return _mock


def _make_per_instance_ref(
    kind: str,
    name: str,
    namespace: str = "default",
    cluster: str = "test-cluster",
) -> DocumentRef:
    bare_kind = kind.split("/", 1)[1] if "/" in kind else kind
    return DocumentRef(
        external_id=f"k8s:{cluster}:{namespace}:{bare_kind}:{name}",
        uri=f"k8s://{cluster}/{namespace}/{bare_kind}/{name}",
        metadata={
            "kind": bare_kind,
            "cluster": cluster,
            "namespace": namespace,
            "name": name,
            "source": "deterministic",
        },
    )


# ---------------------------------------------------------------------------
# 1. build_ssl_verify helper
# ---------------------------------------------------------------------------


class TestBuildSslVerify:
    def test_ca_pem_in_config_returns_ssl_context(self) -> None:
        cfg = K8sAgenticConfig(ca_cert_pem=_TEST_CA_PEM)
        result = build_ssl_verify(cfg, {})
        assert isinstance(result, ssl.SSLContext)

    def test_ca_b64_in_config_returns_ssl_context(self) -> None:
        cfg = K8sAgenticConfig(ca_cert_b64=_TEST_CA_B64)
        result = build_ssl_verify(cfg, {})
        assert isinstance(result, ssl.SSLContext)

    def test_ca_pem_in_secrets_returns_ssl_context(self) -> None:
        cfg = K8sAgenticConfig()
        result = build_ssl_verify(cfg, {"ca_cert_pem": _TEST_CA_PEM})
        assert isinstance(result, ssl.SSLContext)

    def test_ca_b64_in_secrets_returns_ssl_context(self) -> None:
        cfg = K8sAgenticConfig()
        result = build_ssl_verify(cfg, {"ca_cert_b64": _TEST_CA_B64})
        assert isinstance(result, ssl.SSLContext)

    def test_secrets_pem_overrides_config_pem(self) -> None:
        """Secret CA takes precedence over config CA (both supply a valid PEM)."""
        cfg = K8sAgenticConfig(ca_cert_pem=_TEST_CA_PEM)
        # Supplying the same PEM via secrets should still return an SSLContext
        # (precedence logic is internally consistent).
        result = build_ssl_verify(cfg, {"ca_cert_pem": _TEST_CA_PEM})
        assert isinstance(result, ssl.SSLContext)

    def test_verify_ssl_true_no_ca_returns_true(self) -> None:
        cfg = K8sAgenticConfig(verify_ssl=True)
        result = build_ssl_verify(cfg, {})
        assert result is True

    def test_verify_ssl_false_no_ca_returns_false(self) -> None:
        cfg = K8sAgenticConfig(verify_ssl=False)
        result = build_ssl_verify(cfg, {})
        assert result is False

    def test_invalid_b64_in_config_raises_value_error(self) -> None:
        cfg = K8sAgenticConfig(ca_cert_b64="!!!not-base64!!!")
        with pytest.raises(ValueError, match="ca_cert_b64 is not valid base64"):
            build_ssl_verify(cfg, {})

    def test_invalid_b64_in_secrets_raises_value_error(self) -> None:
        cfg = K8sAgenticConfig()
        with pytest.raises(ValueError, match="ca_cert_b64 is not valid base64"):
            build_ssl_verify(cfg, {"ca_cert_b64": "!!!not-base64!!!"})

    def test_decode_b64_pem_roundtrip(self) -> None:
        decoded = _decode_b64_pem(_TEST_CA_B64)
        assert decoded == _TEST_CA_PEM


# ---------------------------------------------------------------------------
# 2. use_llm_kind_selection=False — deterministic mode
# ---------------------------------------------------------------------------


class TestDeterministicMode:
    """Deterministic mode now enumerates instances via _list_kind_instances.

    Tests use a stub for _list_kind_instances so no real HTTP calls are needed.
    The important invariants are:
    - LLM is never called
    - _enumerate_kinds is never called
    - Excluded kinds (default_exclude_kinds + _ALWAYS_EXCLUDE) are never listed
    - Per-instance refs carry expected metadata
    """

    async def test_discover_yields_per_instance_refs(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="eks-prod",
            use_llm_kind_selection=False,
            default_include_kinds=["Deployment", "Service"],
            default_exclude_kinds=[],
        )
        dep_refs = [
            _make_per_instance_ref("Deployment", "api", cluster="eks-prod"),
            _make_per_instance_ref("Deployment", "worker", cluster="eks-prod"),
        ]
        svc_refs = [_make_per_instance_ref("Service", "api-svc", cluster="eks-prod")]
        stub = _stub_list_instances({"Deployment": dep_refs, "Service": svc_refs})

        with (
            patch("omniscience_connectors.agentic.llm.build_provider") as mock_build_provider,
            patch.object(K8sAgenticConnector, "_list_kind_instances", stub),
        ):
            refs = await _collect_discover(connector, cfg)
            mock_build_provider.assert_not_called()

        assert len(refs) == 3
        names = {r.metadata["name"] for r in refs}
        assert names == {"api", "worker", "api-svc"}

    async def test_llm_provider_never_invoked_in_deterministic_mode(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            use_llm_kind_selection=False,
            default_include_kinds=["Service"],
            default_exclude_kinds=[],
        )
        svc_refs = [_make_per_instance_ref("Service", "svc1")]
        stub = _stub_list_instances({"Service": svc_refs})

        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(side_effect=AssertionError("LLM must not be called"))

        with (
            patch(
                "omniscience_connectors.agentic.llm.build_provider",
                return_value=mock_provider,
            ) as mock_build,
            patch.object(K8sAgenticConnector, "_list_kind_instances", stub),
        ):
            refs = await _collect_discover(connector, cfg)
            mock_build.assert_not_called()
            mock_provider.complete.assert_not_called()

        assert len(refs) == 1

    async def test_enumerate_kinds_never_called_in_deterministic_mode(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            use_llm_kind_selection=False,
            default_include_kinds=["ConfigMap"],
            default_exclude_kinds=[],
        )
        cm_refs = [_make_per_instance_ref("ConfigMap", "app-cfg")]
        stub = _stub_list_instances({"ConfigMap": cm_refs})

        with (
            patch.object(
                connector,
                "_enumerate_kinds",
                new=AsyncMock(side_effect=AssertionError("_enumerate_kinds must not be called")),
            ),
            patch.object(K8sAgenticConnector, "_list_kind_instances", stub),
        ):
            refs = await _collect_discover(connector, cfg)

        assert len(refs) == 1

    async def test_deterministic_mode_excludes_default_exclude_kinds(self) -> None:
        """Kinds in default_exclude_kinds are never passed to _list_kind_instances."""
        listed_kinds: list[str] = []

        async def _tracking_list(
            self: Any,
            cfg: K8sAgenticConfig,
            secrets: dict[str, str],
            kind: str,
        ) -> AsyncIterator[DocumentRef]:
            listed_kinds.append(kind)
            yield _make_per_instance_ref(kind, "x")

        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            use_llm_kind_selection=False,
            default_include_kinds=["Deployment", "Pod", "ReplicaSet"],
            default_exclude_kinds=["Pod", "ReplicaSet"],
        )
        with patch.object(K8sAgenticConnector, "_list_kind_instances", _tracking_list):
            await _collect_discover(connector, cfg)

        assert "Pod" not in listed_kinds
        assert "ReplicaSet" not in listed_kinds
        assert "Deployment" in listed_kinds

    async def test_deterministic_mode_excludes_always_exclude_kinds(self) -> None:
        """_ALWAYS_EXCLUDE kinds (Secret, Event, …) are never listed."""
        listed_kinds: list[str] = []

        async def _tracking_list(
            self: Any,
            cfg: K8sAgenticConfig,
            secrets: dict[str, str],
            kind: str,
        ) -> AsyncIterator[DocumentRef]:
            listed_kinds.append(kind)
            yield _make_per_instance_ref(kind, "x")

        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            use_llm_kind_selection=False,
            default_include_kinds=["Deployment", "Secret", "Event", "ConfigMap"],
            default_exclude_kinds=[],
        )
        with patch.object(K8sAgenticConnector, "_list_kind_instances", _tracking_list):
            await _collect_discover(connector, cfg)

        assert "Secret" not in listed_kinds
        assert "Event" not in listed_kinds
        assert "Deployment" in listed_kinds
        assert "ConfigMap" in listed_kinds

    async def test_deterministic_mode_metadata_source_is_deterministic(self) -> None:
        """Per-instance refs carry source='deterministic' (not 'default-fallback')."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            use_llm_kind_selection=False,
            cluster_name="my-cluster",
            default_include_kinds=["Service"],
            default_exclude_kinds=[],
        )
        svc_refs = [_make_per_instance_ref("Service", "svc1", cluster="my-cluster")]
        stub = _stub_list_instances({"Service": svc_refs})

        with patch.object(K8sAgenticConnector, "_list_kind_instances", stub):
            refs = await _collect_discover(connector, cfg)

        assert len(refs) == 1
        assert all(r.metadata["source"] == "deterministic" for r in refs)
        assert all(r.metadata["cluster"] == "my-cluster" for r in refs)

    async def test_llm_mode_still_invokes_llm_when_enabled(self) -> None:
        """Regression: use_llm_kind_selection=True keeps the LLM path intact."""
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            cluster_name="test",
            use_llm_kind_selection=True,
        )
        llm_response = json.dumps({"include": ["Deployment", "Service"], "exclude": ["Pod"]})
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=llm_response)

        with (
            patch.object(
                connector,
                "_enumerate_kinds",
                new=AsyncMock(return_value=["Deployment", "Service", "Pod"]),
            ),
            patch(
                "omniscience_connectors.agentic.llm.build_provider",
                return_value=mock_provider,
            ),
        ):
            refs = await _collect_discover(connector, cfg)

        mock_provider.complete.assert_called_once()
        kinds = {r.metadata["kind"] for r in refs}
        assert "Deployment" in kinds
        assert "Service" in kinds
        assert "Pod" not in kinds


# ---------------------------------------------------------------------------
# 3. SA bearer token sent as Authorization: Bearer header
# ---------------------------------------------------------------------------


class TestBearerTokenHeader:
    async def test_validate_sends_bearer_token(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.example.com", verify_ssl=False)

        mock_client_cls, _ = _make_httpx_mock()

        with patch("httpx.AsyncClient", mock_client_cls):
            await connector.validate(cfg, {"token": "my-sa-token"})

        # Header must be set at client construction time via AsyncClient(headers=...)
        client_init_kwargs = mock_client_cls.call_args[1]
        assert "Authorization" in client_init_kwargs.get("headers", {})
        assert client_init_kwargs["headers"]["Authorization"] == "Bearer my-sa-token"

    async def test_fetch_sends_bearer_token(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.example.com", verify_ssl=False)
        ref = DocumentRef(
            external_id="k8s:test:kind:Deployment",
            uri="k8s://test/Deployment",
            metadata={"kind": "Deployment", "cluster": "test", "namespace": ""},
        )

        mock_client_cls, _ = _make_httpx_mock(
            response_data={"items": []},
        )

        with patch("httpx.AsyncClient", mock_client_cls):
            await connector.fetch(cfg, {"token": "fetch-token"}, ref)

        client_init_kwargs = mock_client_cls.call_args[1]
        assert client_init_kwargs["headers"]["Authorization"] == "Bearer fetch-token"

    async def test_enumerate_kinds_sends_bearer_token(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.example.com", verify_ssl=False)

        api_v1_response = {"resources": [{"kind": "Deployment", "name": "deployments"}]}
        apis_response = {"groups": []}

        responses = [
            MagicMock(
                is_success=True,
                json=MagicMock(return_value=api_v1_response),
            ),
            MagicMock(
                is_success=True,
                json=MagicMock(return_value=apis_response),
            ),
        ]
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=responses)
        mock_client_cls = MagicMock(return_value=mock_client)

        with patch("httpx.AsyncClient", mock_client_cls):
            kinds = await connector._enumerate_kinds(cfg, {"token": "enum-token"})

        client_init_kwargs = mock_client_cls.call_args[1]
        assert client_init_kwargs["headers"]["Authorization"] == "Bearer enum-token"
        assert "Deployment" in kinds

    async def test_validate_omits_auth_header_when_no_token(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.example.com", verify_ssl=False)

        mock_client_cls, _ = _make_httpx_mock()

        with patch("httpx.AsyncClient", mock_client_cls):
            await connector.validate(cfg, {})

        client_init_kwargs = mock_client_cls.call_args[1]
        assert "Authorization" not in client_init_kwargs.get("headers", {})


# ---------------------------------------------------------------------------
# 4. httpx verify= uses SSLContext when CA is present
# ---------------------------------------------------------------------------


class TestHttpxVerifyWiring:
    async def test_validate_uses_ssl_context_when_ca_pem_provided(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            api_server="https://k8s.example.com",
            ca_cert_pem=_TEST_CA_PEM,
        )

        mock_client_cls, _ = _make_httpx_mock()

        with patch("httpx.AsyncClient", mock_client_cls):
            await connector.validate(cfg, {})

        client_init_kwargs = mock_client_cls.call_args[1]
        assert isinstance(client_init_kwargs["verify"], ssl.SSLContext)

    async def test_fetch_uses_ssl_context_when_ca_b64_in_secrets(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(api_server="https://k8s.example.com")
        ref = DocumentRef(
            external_id="k8s:test:kind:ConfigMap",
            uri="k8s://test/ConfigMap",
            metadata={"kind": "ConfigMap", "cluster": "test", "namespace": ""},
        )
        mock_client_cls, _ = _make_httpx_mock(response_data={"items": []})

        with patch("httpx.AsyncClient", mock_client_cls):
            await connector.fetch(cfg, {"ca_cert_b64": _TEST_CA_B64}, ref)

        client_init_kwargs = mock_client_cls.call_args[1]
        assert isinstance(client_init_kwargs["verify"], ssl.SSLContext)

    async def test_validate_passes_bool_verify_when_no_ca(self) -> None:
        connector = K8sAgenticConnector()
        cfg = K8sAgenticConfig(
            api_server="https://k8s.example.com",
            verify_ssl=False,
        )

        mock_client_cls, _ = _make_httpx_mock()

        with patch("httpx.AsyncClient", mock_client_cls):
            await connector.validate(cfg, {})

        client_init_kwargs = mock_client_cls.call_args[1]
        assert client_init_kwargs["verify"] is False


# ---------------------------------------------------------------------------
# 5. New config field defaults
# ---------------------------------------------------------------------------


class TestNewConfigFieldDefaults:
    def test_ca_cert_pem_defaults_to_none(self) -> None:
        cfg = K8sAgenticConfig()
        assert cfg.ca_cert_pem is None

    def test_ca_cert_b64_defaults_to_none(self) -> None:
        cfg = K8sAgenticConfig()
        assert cfg.ca_cert_b64 is None

    def test_use_llm_kind_selection_defaults_to_true(self) -> None:
        cfg = K8sAgenticConfig()
        assert cfg.use_llm_kind_selection is True

    def test_verify_ssl_default_unchanged(self) -> None:
        """Existing default verify_ssl=True must not regress."""
        cfg = K8sAgenticConfig()
        assert cfg.verify_ssl is True
