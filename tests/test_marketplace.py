"""Tests for Issue #75: Third-party connector marketplace.

Covers:
- ConnectorPlugin model validation
- PluginLoader: discovery, loading, registration
- PluginManifest: parse from path, from package, validate_connector_class
- SandboxedConnector: timeout, exception isolation, concurrency, discover limit
- CLI commands: plugins list, plugins install, plugins info
- ExampleConnector: validate, discover, fetch
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument
from omniscience_connectors.plugins.loader import (
    ConnectorPlugin,
    PluginLoader,
    PluginLoadError,
)
from omniscience_connectors.plugins.manifest import ManifestValidationError, PluginManifest
from omniscience_connectors.plugins.sandbox import (
    PluginTimeoutError,
    SandboxConfig,
    SandboxedConnector,
    SandboxError,
)
from omniscience_connectors.registry import ConnectorRegistry
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Fixtures and helper connectors
# ---------------------------------------------------------------------------


class _MinimalConfig(BaseModel):
    url: str = "http://localhost"


class _GoodConnector(Connector):
    connector_type: ClassVar[str] = "test-good"
    config_schema: ClassVar[type[BaseModel]] = _MinimalConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        pass

    async def discover(
        self, config: BaseModel, secrets: dict[str, str]
    ) -> AsyncIterator[DocumentRef]:
        yield DocumentRef(external_id="doc-1", uri="http://localhost/doc-1")
        yield DocumentRef(external_id="doc-2", uri="http://localhost/doc-2")

    async def fetch(
        self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
    ) -> FetchedDocument:
        return FetchedDocument(ref=ref, content_bytes=b"content", content_type="text/plain")


class _SlowConnector(Connector):
    """Connector that always sleeps, used to test timeout enforcement."""

    connector_type: ClassVar[str] = "test-slow"
    config_schema: ClassVar[type[BaseModel]] = _MinimalConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        await asyncio.sleep(10)

    async def discover(
        self, config: BaseModel, secrets: dict[str, str]
    ) -> AsyncIterator[DocumentRef]:
        await asyncio.sleep(10)
        yield DocumentRef(external_id="x", uri="x")  # pragma: no cover

    async def fetch(
        self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
    ) -> FetchedDocument:
        await asyncio.sleep(10)
        raise RuntimeError("unreachable")  # pragma: no cover


class _ErrorConnector(Connector):
    """Connector that always raises RuntimeError."""

    connector_type: ClassVar[str] = "test-error"
    config_schema: ClassVar[type[BaseModel]] = _MinimalConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        raise RuntimeError("plugin error from validate")

    async def discover(
        self, config: BaseModel, secrets: dict[str, str]
    ) -> AsyncIterator[DocumentRef]:
        raise RuntimeError("plugin error from discover")
        yield  # pragma: no cover

    async def fetch(
        self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
    ) -> FetchedDocument:
        raise RuntimeError("plugin error from fetch")


def _make_plugin(
    *,
    name: str = "test-plugin",
    connector_type: str = "test-good",
    module_path: str = "omniscience_connectors.base",
    class_name: str = "Connector",
    version: str = "1.0.0",
) -> ConnectorPlugin:
    return ConnectorPlugin(
        name=name,
        version=version,
        connector_type=connector_type,
        module_path=module_path,
        class_name=class_name,
    )


def _extract_json(output: str) -> str:
    """Find the first JSON array or object in CLI output (may contain log lines)."""
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        idx = output.find(start_char)
        if idx != -1:
            end_idx = output.rfind(end_char)
            if end_idx != -1 and end_idx > idx:
                return output[idx : end_idx + 1]
    return output


# ---------------------------------------------------------------------------
# 1. ConnectorPlugin model
# ---------------------------------------------------------------------------


class TestConnectorPlugin:
    def test_required_fields(self) -> None:
        p = ConnectorPlugin(
            name="myplugin",
            version="2.0.0",
            connector_type="my-type",
            module_path="mypackage.connector",
            class_name="MyConnector",
        )
        assert p.name == "myplugin"
        assert p.version == "2.0.0"
        assert p.connector_type == "my-type"
        assert p.module_path == "mypackage.connector"
        assert p.class_name == "MyConnector"

    def test_optional_fields_default(self) -> None:
        p = ConnectorPlugin(
            name="x",
            version="0.1",
            connector_type="x",
            module_path="x",
            class_name="X",
        )
        assert p.description == ""
        assert p.author == ""
        assert p.homepage is None

    def test_optional_fields_set(self) -> None:
        p = ConnectorPlugin(
            name="x",
            version="0.1",
            connector_type="x",
            module_path="x",
            class_name="X",
            description="A plugin",
            author="Author",
            homepage="https://example.com",
        )
        assert p.description == "A plugin"
        assert p.homepage == "https://example.com"

    def test_serialization(self) -> None:
        p = ConnectorPlugin(
            name="x",
            version="0.1",
            connector_type="x",
            module_path="x.m",
            class_name="XC",
        )
        d = p.model_dump()
        assert d["name"] == "x"
        assert d["module_path"] == "x.m"


# ---------------------------------------------------------------------------
# 2. PluginLoader — load_plugin validation
# ---------------------------------------------------------------------------


class TestPluginLoaderLoadPlugin:
    def test_load_good_connector(self) -> None:
        """_GoodConnector is importable in this test module; verify load works."""
        plugin = ConnectorPlugin(
            name="test-good",
            version="1.0.0",
            connector_type="test-good",
            module_path=f"{__name__}",
            class_name="_GoodConnector",
        )
        loader = PluginLoader()
        cls = loader.load_plugin(plugin)
        assert cls is _GoodConnector

    def test_load_bad_module(self) -> None:
        plugin = _make_plugin(module_path="nonexistent.module.xyz")
        loader = PluginLoader()
        with pytest.raises(PluginLoadError) as exc_info:
            loader.load_plugin(plugin)
        assert "cannot import module" in str(exc_info.value)

    def test_load_missing_class(self) -> None:
        plugin = _make_plugin(
            module_path="omniscience_connectors.base",
            class_name="NonExistentClass",
        )
        loader = PluginLoader()
        with pytest.raises(PluginLoadError) as exc_info:
            loader.load_plugin(plugin)
        assert "NonExistentClass" in str(exc_info.value)

    def test_load_non_connector_class(self) -> None:
        """Loading a class that does not subclass Connector must fail."""
        plugin = _make_plugin(
            module_path="pydantic",
            class_name="BaseModel",
        )
        loader = PluginLoader()
        with pytest.raises(PluginLoadError) as exc_info:
            loader.load_plugin(plugin)
        assert "subclass Connector" in str(exc_info.value)

    def test_load_connector_missing_type(self) -> None:
        """A Connector subclass without connector_type must fail validation."""

        class _NakedConnector(Connector):
            config_schema: ClassVar[type[BaseModel]] = _MinimalConfig
            # Note: no connector_type defined

            async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
                pass

            async def discover(
                self, config: BaseModel, secrets: dict[str, str]
            ) -> AsyncIterator[DocumentRef]:
                yield  # pragma: no cover

            async def fetch(
                self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
            ) -> FetchedDocument:
                raise NotImplementedError  # pragma: no cover

        # Patch the module namespace so the loader can import it
        import tests.test_marketplace as _self

        _self._NakedConnector = _NakedConnector  # type: ignore[attr-defined]
        plugin = _make_plugin(module_path="tests.test_marketplace", class_name="_NakedConnector")
        loader = PluginLoader()
        with pytest.raises(PluginLoadError) as exc_info:
            loader.load_plugin(plugin)
        assert "connector_type" in str(exc_info.value)

    def test_load_connector_missing_config_schema(self) -> None:
        """A Connector subclass without config_schema must fail validation."""

        class _NoSchema(Connector):
            connector_type: ClassVar[str] = "no-schema"
            # no config_schema

            async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
                pass

            async def discover(
                self, config: BaseModel, secrets: dict[str, str]
            ) -> AsyncIterator[DocumentRef]:
                yield  # pragma: no cover

            async def fetch(
                self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
            ) -> FetchedDocument:
                raise NotImplementedError  # pragma: no cover

        import tests.test_marketplace as _self

        _self._NoSchema = _NoSchema  # type: ignore[attr-defined]
        plugin = _make_plugin(module_path="tests.test_marketplace", class_name="_NoSchema")
        loader = PluginLoader()
        with pytest.raises(PluginLoadError) as exc_info:
            loader.load_plugin(plugin)
        assert "config_schema" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. PluginLoader — discover_plugins and register_all
# ---------------------------------------------------------------------------


class TestPluginLoaderDiscoverAndRegister:
    def test_discover_no_plugins(self) -> None:
        """When no entry points are installed, discover_plugins returns empty list."""
        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[]):
            loader = PluginLoader()
            plugins = loader.discover_plugins()
        assert plugins == []

    def test_discover_skips_bad_entry_point(self) -> None:
        """Entry points with malformed values are skipped with a warning."""
        bad_ep = MagicMock()
        bad_ep.name = "bad-plugin"
        bad_ep.value = "no_colon_here"  # missing ":"

        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[bad_ep]):
            loader = PluginLoader()
            plugins = loader.discover_plugins()
        assert plugins == []

    def test_register_all_empty(self) -> None:
        """register_all returns 0 when no plugins are discovered."""
        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[]):
            loader = PluginLoader()
            registry = ConnectorRegistry()
            count = loader.register_all(registry)
        assert count == 0

    def test_register_all_success(self) -> None:
        """register_all loads and registers a valid connector."""
        ep = MagicMock()
        ep.name = "good"
        ep.value = f"{__name__}:_GoodConnector"
        ep.dist = None

        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[ep]):
            loader = PluginLoader()
            registry = ConnectorRegistry()
            count = loader.register_all(registry)

        assert count == 1
        assert "test-good" in registry.registered_types()

    def test_register_all_bad_plugin_skipped(self) -> None:
        """register_all skips connectors that fail to load and logs an error."""
        ep = MagicMock()
        ep.name = "broken"
        ep.value = "nonexistent.module:SomeClass"
        ep.dist = None

        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[ep]):
            loader = PluginLoader()
            registry = ConnectorRegistry()
            count = loader.register_all(registry)

        assert count == 0
        assert registry.registered_types() == []


# ---------------------------------------------------------------------------
# 4. PluginManifest
# ---------------------------------------------------------------------------


class TestPluginManifest:
    def _write_manifest(self, tmp_path: Path, data: dict) -> Path:  # type: ignore[type-arg]
        manifest_path = tmp_path / "omniscience-plugin.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        return manifest_path

    def test_from_path_valid(self, tmp_path: Path) -> None:
        data = {
            "name": "my-plugin",
            "version": "1.0.0",
            "connector_type": "my-type",
            "description": "A plugin",
            "author": "Dev",
        }
        path = self._write_manifest(tmp_path, data)
        manifest = PluginManifest.from_path(path)
        assert manifest.name == "my-plugin"
        assert manifest.connector_type == "my-type"

    def test_from_path_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestValidationError, match="not found"):
            PluginManifest.from_path(tmp_path / "does-not-exist.json")

    def test_from_path_invalid_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "omniscience-plugin.json"
        bad.write_text("{not valid json}", encoding="utf-8")
        with pytest.raises(ManifestValidationError, match="not valid JSON"):
            PluginManifest.from_path(bad)

    def test_from_path_missing_required_fields(self, tmp_path: Path) -> None:
        path = self._write_manifest(tmp_path, {"name": "x"})
        with pytest.raises(ManifestValidationError, match="missing required fields"):
            PluginManifest.from_path(path)

    def test_from_path_optional_fields(self, tmp_path: Path) -> None:
        data = {
            "name": "p",
            "version": "0.1.0",
            "connector_type": "p",
            "capabilities": ["webhook"],
            "homepage": "https://example.com",
        }
        path = self._write_manifest(tmp_path, data)
        manifest = PluginManifest.from_path(path)
        assert manifest.capabilities == ["webhook"]
        assert manifest.homepage == "https://example.com"

    def test_validate_connector_class_ok(self, tmp_path: Path) -> None:
        data = {"name": "good", "version": "1.0.0", "connector_type": "test-good"}
        path = self._write_manifest(tmp_path, data)
        manifest = PluginManifest.from_path(path)
        manifest.validate_connector_class(_GoodConnector)  # should not raise

    def test_validate_connector_class_type_mismatch(self, tmp_path: Path) -> None:
        data = {"name": "x", "version": "1.0.0", "connector_type": "wrong-type"}
        path = self._write_manifest(tmp_path, data)
        manifest = PluginManifest.from_path(path)
        with pytest.raises(ManifestValidationError, match="connector_type mismatch"):
            manifest.validate_connector_class(_GoodConnector)

    def test_validate_connector_class_not_a_connector(self, tmp_path: Path) -> None:
        data = {"name": "x", "version": "1.0.0", "connector_type": "x"}
        path = self._write_manifest(tmp_path, data)
        manifest = PluginManifest.from_path(path)
        with pytest.raises(ManifestValidationError, match="Connector subclass"):
            manifest.validate_connector_class(BaseModel)  # type: ignore[arg-type]

    def test_from_package_not_importable(self) -> None:
        with pytest.raises(ManifestValidationError, match="Cannot import package"):
            PluginManifest.from_package("nonexistent_plugin_package_xyz")


# ---------------------------------------------------------------------------
# 5. SandboxedConnector
# ---------------------------------------------------------------------------


class TestSandboxedConnector:
    def _make_sandboxed(
        self,
        inner: Connector,
        timeout: float = 5.0,
        max_concurrent: int = 5,
    ) -> SandboxedConnector:
        cfg = SandboxConfig(timeout_seconds=timeout, max_concurrent_requests=max_concurrent)
        return SandboxedConnector(inner=inner, plugin_name="test-plugin", config=cfg)

    def test_mirrors_connector_type(self) -> None:
        sandboxed = self._make_sandboxed(_GoodConnector())
        assert sandboxed.connector_type == "test-good"

    def test_mirrors_config_schema(self) -> None:
        sandboxed = self._make_sandboxed(_GoodConnector())
        assert sandboxed.config_schema is _MinimalConfig

    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        sandboxed = self._make_sandboxed(_GoodConnector())
        config = _MinimalConfig()
        await sandboxed.validate(config, {})  # should not raise

    @pytest.mark.asyncio
    async def test_validate_timeout(self) -> None:
        sandboxed = self._make_sandboxed(_SlowConnector(), timeout=0.05)
        config = _MinimalConfig()
        with pytest.raises(PluginTimeoutError) as exc_info:
            await sandboxed.validate(config, {})
        assert "test-plugin" in str(exc_info.value)
        assert "validate" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_exception_isolation(self) -> None:
        sandboxed = self._make_sandboxed(_ErrorConnector())
        config = _MinimalConfig()
        with pytest.raises(SandboxError) as exc_info:
            await sandboxed.validate(config, {})
        assert "validate" in str(exc_info.value)
        assert isinstance(exc_info.value.cause, RuntimeError)

    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        sandboxed = self._make_sandboxed(_GoodConnector())
        config = _MinimalConfig()
        ref = DocumentRef(external_id="doc-1", uri="http://localhost/doc-1")
        doc = await sandboxed.fetch(config, {}, ref)
        assert doc.content_bytes == b"content"

    @pytest.mark.asyncio
    async def test_fetch_timeout(self) -> None:
        sandboxed = self._make_sandboxed(_SlowConnector(), timeout=0.05)
        config = _MinimalConfig()
        ref = DocumentRef(external_id="x", uri="x")
        with pytest.raises(PluginTimeoutError):
            await sandboxed.fetch(config, {}, ref)

    @pytest.mark.asyncio
    async def test_fetch_exception_isolation(self) -> None:
        sandboxed = self._make_sandboxed(_ErrorConnector())
        config = _MinimalConfig()
        ref = DocumentRef(external_id="x", uri="x")
        with pytest.raises(SandboxError) as exc_info:
            await sandboxed.fetch(config, {}, ref)
        assert "fetch" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_discover_yields_items(self) -> None:
        sandboxed = self._make_sandboxed(_GoodConnector())
        config = _MinimalConfig()
        refs = [ref async for ref in sandboxed.discover(config, {})]
        assert len(refs) == 2
        assert refs[0].external_id == "doc-1"

    @pytest.mark.asyncio
    async def test_discover_item_limit(self) -> None:
        """discover() must stop after max_discover_items items."""

        class _ManyDocsConnector(Connector):
            connector_type: ClassVar[str] = "many-docs"
            config_schema: ClassVar[type[BaseModel]] = _MinimalConfig

            async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
                pass

            async def discover(
                self, config: BaseModel, secrets: dict[str, str]
            ) -> AsyncIterator[DocumentRef]:
                for i in range(1000):
                    yield DocumentRef(external_id=f"doc-{i}", uri=f"http://x/{i}")

            async def fetch(
                self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
            ) -> FetchedDocument:
                raise NotImplementedError  # pragma: no cover

        cfg = SandboxConfig(
            timeout_seconds=5.0,
            max_concurrent_requests=5,
            max_discover_items=5,
        )
        sandboxed = SandboxedConnector(
            inner=_ManyDocsConnector(), plugin_name="many-docs", config=cfg
        )
        refs = [ref async for ref in sandboxed.discover(_MinimalConfig(), {})]
        assert len(refs) == 5

    @pytest.mark.asyncio
    async def test_discover_exception_isolation(self) -> None:
        sandboxed = self._make_sandboxed(_ErrorConnector())
        config = _MinimalConfig()
        with pytest.raises(SandboxError) as exc_info:
            async for _ in sandboxed.discover(config, {}):
                pass  # pragma: no cover
        assert "discover" in str(exc_info.value)

    def test_webhook_handler_passthrough(self) -> None:
        sandboxed = self._make_sandboxed(_GoodConnector())
        assert sandboxed.webhook_handler() is None

    @pytest.mark.asyncio
    async def test_concurrency_limit_enforced(self) -> None:
        """Multiple concurrent calls are bounded by the semaphore."""
        call_count = 0
        max_seen = 0

        class _CountingConnector(Connector):
            connector_type: ClassVar[str] = "counting"
            config_schema: ClassVar[type[BaseModel]] = _MinimalConfig

            async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
                nonlocal call_count, max_seen
                call_count += 1
                max_seen = max(max_seen, call_count)
                await asyncio.sleep(0.05)
                call_count -= 1

            async def discover(
                self, config: BaseModel, secrets: dict[str, str]
            ) -> AsyncIterator[DocumentRef]:
                yield  # pragma: no cover

            async def fetch(
                self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
            ) -> FetchedDocument:
                raise NotImplementedError  # pragma: no cover

        cfg = SandboxConfig(timeout_seconds=5.0, max_concurrent_requests=2)
        sandboxed = SandboxedConnector(
            inner=_CountingConnector(), plugin_name="counting", config=cfg
        )
        config = _MinimalConfig()
        # Fire 5 concurrent validate calls
        await asyncio.gather(*[sandboxed.validate(config, {}) for _ in range(5)])
        assert max_seen <= 2


# ---------------------------------------------------------------------------
# 6. ExampleConnector (example plugin)
# ---------------------------------------------------------------------------


class TestExampleConnector:
    @pytest.fixture()
    def connector(self):  # type: ignore[no-untyped-def]
        from omniscience_plugin_example.connector import ExampleConnector

        return ExampleConnector()

    @pytest.fixture()
    def config(self):  # type: ignore[no-untyped-def]
        from omniscience_plugin_example.connector import ExampleConfig

        return ExampleConfig()

    @pytest.mark.asyncio
    async def test_connector_type(self, connector) -> None:  # type: ignore[no-untyped-def]
        assert connector.connector_type == "example"

    @pytest.mark.asyncio
    async def test_validate_ok(self, connector, config) -> None:  # type: ignore[no-untyped-def]
        await connector.validate(config, {})  # should not raise

    @pytest.mark.asyncio
    async def test_discover_yields_docs(self, connector, config) -> None:  # type: ignore[no-untyped-def]
        refs = [ref async for ref in connector.discover(config, {})]
        assert len(refs) == 3
        assert all(isinstance(r, DocumentRef) for r in refs)

    @pytest.mark.asyncio
    async def test_discover_respects_max(self, connector) -> None:  # type: ignore[no-untyped-def]
        from omniscience_plugin_example.connector import ExampleConfig

        config = ExampleConfig(max_documents=1)
        refs = [ref async for ref in connector.discover(config, {})]
        assert len(refs) == 1

    @pytest.mark.asyncio
    async def test_fetch_returns_markdown(self, connector, config) -> None:  # type: ignore[no-untyped-def]
        refs = [ref async for ref in connector.discover(config, {})]
        doc = await connector.fetch(config, {}, refs[0])
        assert doc.content_type == "text/markdown"
        assert b"Introduction" in doc.content_bytes

    @pytest.mark.asyncio
    async def test_fetch_unknown_ref_raises(self, connector, config) -> None:  # type: ignore[no-untyped-def]
        ref = DocumentRef(external_id="does-not-exist", uri="http://x")
        with pytest.raises(KeyError):
            await connector.fetch(config, {}, ref)


# ---------------------------------------------------------------------------
# 7. CLI: plugins commands
# ---------------------------------------------------------------------------


class TestPluginsCLI:
    def _make_runner(self):  # type: ignore[no-untyped-def]
        from typer.testing import CliRunner

        return CliRunner()

    def _make_app(self):  # type: ignore[no-untyped-def]
        from omniscience_cli.main import app

        return app

    def test_plugins_list_no_plugins(self) -> None:
        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[]):
            runner = self._make_runner()
            result = runner.invoke(self._make_app(), ["plugins", "list"])
        assert result.exit_code == 0
        assert "No connector plugins" in result.output

    def test_plugins_list_with_plugin(self) -> None:
        ep = MagicMock()
        ep.name = "my-plugin"
        ep.value = f"{__name__}:_GoodConnector"
        ep.dist = None

        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[ep]):
            runner = self._make_runner()
            result = runner.invoke(self._make_app(), ["plugins", "list"])
        assert result.exit_code == 0
        assert "my-plugin" in result.output

    def test_plugins_list_json(self) -> None:
        ep = MagicMock()
        ep.name = "my-plugin"
        ep.value = f"{__name__}:_GoodConnector"
        ep.dist = None

        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[ep]):
            runner = self._make_runner()
            result = runner.invoke(self._make_app(), ["plugins", "list", "--json"])
        assert result.exit_code == 0
        # Log lines may precede the JSON output; extract the JSON portion.
        json_str = _extract_json(result.output)
        data = json.loads(json_str)
        assert isinstance(data, list)
        assert data[0]["name"] == "my-plugin"

    def test_plugins_install_calls_pip(self) -> None:
        with (
            patch("subprocess.run") as mock_run,
            patch("omniscience_connectors.plugins.loader.entry_points", return_value=[]),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner = self._make_runner()
            result = runner.invoke(
                self._make_app(), ["plugins", "install", "omniscience-plugin-example"]
            )
        assert result.exit_code == 0
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "pip" in call_args
        assert "install" in call_args
        assert "omniscience-plugin-example" in call_args

    def test_plugins_install_pip_failure(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            runner = self._make_runner()
            result = runner.invoke(self._make_app(), ["plugins", "install", "bad-package"])
        assert result.exit_code != 0

    def test_plugins_info_not_found(self) -> None:
        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[]):
            runner = self._make_runner()
            result = runner.invoke(self._make_app(), ["plugins", "info", "nonexistent"])
        assert result.exit_code != 0

    def test_plugins_info_found(self) -> None:
        ep = MagicMock()
        ep.name = "good"
        ep.value = f"{__name__}:_GoodConnector"
        ep.dist = None

        with patch("omniscience_connectors.plugins.loader.entry_points", return_value=[ep]):
            runner = self._make_runner()
            result = runner.invoke(self._make_app(), ["plugins", "info", "good"])
        assert result.exit_code == 0
        assert "good" in result.output
        assert "_GoodConnector" in result.output
