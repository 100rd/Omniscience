"""Fail-closed qualification canary for a materialized MCP v1 release."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from mcp.types import CallToolResult, TextContent, Tool
from omniscience_server.mcp import canary
from omniscience_server.mcp.contract import build_contract_info, contract_root

SOURCE_COMMIT = "9" * 40
WORKSPACE_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


@pytest.fixture
def release_bundle(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    shutil.copytree(contract_root(), staging)
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_commit"] = {"git_commit": SOURCE_COMMIT}
    manifest_bytes = _canonical_bytes(manifest)
    (staging / "manifest.json").write_bytes(manifest_bytes)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    bundle = tmp_path / "published" / "sha256" / digest
    bundle.parent.mkdir(parents=True)
    staging.rename(bundle)
    return bundle


@pytest.fixture
def release_pin(release_bundle: Path) -> canary.ReleasePin:
    return canary.load_release_pin(release_bundle)


@pytest.fixture
def runtime_tools(release_pin: canary.ReleasePin) -> list[Tool]:
    return [
        Tool(name=name, inputSchema=copy.deepcopy(schema))
        for name, schema in release_pin.input_schemas.items()
    ]


@pytest.fixture
def contract_result(
    release_pin: canary.ReleasePin,
    monkeypatch: pytest.MonkeyPatch,
) -> CallToolResult:
    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    payload = build_contract_info(WORKSPACE_ID)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        structuredContent=payload,
        isError=False,
    )


def _initialization(*, tools: bool = True) -> SimpleNamespace:
    return SimpleNamespace(capabilities=SimpleNamespace(tools=object() if tools else None))


def test_canary_contract_constants_match_producer_and_materializer() -> None:
    from omniscience_server.mcp import materialize
    from omniscience_server.mcp.contract import CONTRACT_VERSION, TOKEN_PROFILE_ID

    assert canary.CONTRACT_VERSION == CONTRACT_VERSION
    assert canary.TOKEN_PROFILE_ID == TOKEN_PROFILE_ID
    assert canary.EXPECTED_TOOLS == materialize.EXPECTED_TOOLS
    assert canary.EXPECTED_CAPABILITIES == materialize.EXPECTED_CAPABILITIES


def test_load_release_pin_verifies_exact_content_addressed_bundle(
    release_bundle: Path,
) -> None:
    pin = canary.load_release_pin(release_bundle)

    assert pin.bundle_dir == release_bundle.resolve()
    assert pin.manifest_sha256 == release_bundle.name
    assert pin.source_commit == SOURCE_COMMIT
    assert pin.tool_names == tuple(sorted(pin.input_schemas))
    assert len(pin.tool_names) == 15
    assert pin.schema_digests == {row["path"]: row["sha256"] for row in pin.manifest["schemas"]}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda bundle: (bundle / "tool-registry.json").write_bytes(b"{}\n"),
            "tool_registry_sha256_mismatch",
        ),
        (
            lambda bundle: (bundle / "unexpected.json").write_bytes(b"{}\n"),
            "bundle_file_set_invalid",
        ),
        (
            lambda bundle: (bundle / "manifest.json").write_bytes(b"{}\n"),
            "manifest_address_mismatch",
        ),
    ],
)
def test_load_release_pin_rejects_bundle_drift(
    release_bundle: Path,
    mutate: object,
    code: str,
) -> None:
    mutate(release_bundle)  # type: ignore[operator]
    with pytest.raises(canary.CanaryError, match=f"^{code}$"):
        canary.load_release_pin(release_bundle)


def test_load_release_pin_rejects_symlinked_artifact(
    release_bundle: Path,
) -> None:
    schema = next((release_bundle / "schemas").iterdir())
    copy_path = release_bundle / "copied-schema.json"
    copy_path.write_bytes(schema.read_bytes())
    schema.unlink()
    schema.symlink_to(copy_path)

    with pytest.raises(canary.CanaryError, match=r"^bundle_artifact_invalid$"):
        canary.load_release_pin(release_bundle)


def test_runtime_observation_matches_bundle_and_token_workspace(
    release_pin: canary.ReleasePin,
    runtime_tools: list[Tool],
    contract_result: CallToolResult,
) -> None:
    receipt = canary.verify_runtime_observation(
        pin=release_pin,
        initialization=_initialization(),
        tools=runtime_tools,
        contract_result=contract_result,
        expected_workspace_id=WORKSPACE_ID,
    )

    assert receipt.status == "passed"
    assert receipt.contract_version == "1.0.0"
    assert receipt.manifest_sha256 == release_pin.manifest_sha256
    assert receipt.source_commit == SOURCE_COMMIT
    assert receipt.workspace_id == str(WORKSPACE_ID)
    assert receipt.tool_count == 15


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("no_tools_capability", "runtime_tools_capability_missing"),
        ("missing_tool", "runtime_tool_surface_mismatch"),
        ("schema_drift", "runtime_input_schema_mismatch"),
        ("tool_error", "contract_info_call_failed"),
        ("missing_structured", "contract_info_structured_content_missing"),
        ("ambiguous_content", "contract_info_content_mismatch"),
        ("extra_field", "contract_info_shape_mismatch"),
        ("manifest_drift", "contract_info_manifest_sha256_mismatch"),
        ("workspace_drift", "contract_info_workspace_mismatch"),
        ("fallback", "contract_info_fallback_required"),
    ],
)
def test_runtime_observation_fails_closed_on_skew(
    release_pin: canary.ReleasePin,
    runtime_tools: list[Tool],
    contract_result: CallToolResult,
    mutation: str,
    code: str,
) -> None:
    initialization = _initialization()
    tools = copy.deepcopy(runtime_tools)
    result = contract_result.model_copy(deep=True)
    assert result.structuredContent is not None

    if mutation == "no_tools_capability":
        initialization = _initialization(tools=False)
    elif mutation == "missing_tool":
        tools.pop()
    elif mutation == "schema_drift":
        tools[0].inputSchema["unexpected"] = True
    elif mutation == "tool_error":
        result.isError = True
    elif mutation == "missing_structured":
        result.structuredContent = None
    elif mutation == "ambiguous_content":
        assert isinstance(result.content[0], TextContent)
        result.content[0].text = "{}"
    elif mutation == "extra_field":
        result.structuredContent["unexpected"] = True
    elif mutation == "manifest_drift":
        result.structuredContent["manifest_sha256"] = "a" * 64
    elif mutation == "workspace_drift":
        result.structuredContent["meta"]["workspace_id"] = str(uuid.uuid4())
    elif mutation == "fallback":
        result.structuredContent["meta"]["fallback"] = {
            "required": True,
            "reason": "contract_mismatch",
        }

    if mutation in {
        "extra_field",
        "manifest_drift",
        "workspace_drift",
        "fallback",
    }:
        assert isinstance(result.content[0], TextContent)
        result.content[0].text = json.dumps(
            result.structuredContent,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    with pytest.raises(canary.CanaryError, match=f"^{code}$"):
        canary.verify_runtime_observation(
            pin=release_pin,
            initialization=initialization,
            tools=tools,
            contract_result=result,
            expected_workspace_id=WORKSPACE_ID,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/mcp/",
        "https://example.com/mcp",
        "https://user:password@example.com/mcp/",
        "https://example.com/mcp/?token=secret",
        "https://example.com/mcp/#fragment",
    ],
)
def test_endpoint_validation_rejects_unsafe_or_redirecting_urls(endpoint: str) -> None:
    with pytest.raises(canary.CanaryError, match=r"^endpoint_invalid$"):
        canary.validate_endpoint(endpoint)


def test_endpoint_validation_allows_tls_and_numeric_loopback() -> None:
    assert canary.validate_endpoint("https://example.com/mcp/") == ("https://example.com/mcp/")
    assert canary.validate_endpoint("http://127.0.0.1:8000/mcp/") == ("http://127.0.0.1:8000/mcp/")


def test_cli_never_accepts_or_discloses_bearer_token(
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "super-secret-canary-token"
    monkeypatch.setenv("OMNISCIENCE_MCP_CANARY_TOKEN", secret)
    monkeypatch.setenv("OMNISCIENCE_MCP_CANARY_WORKSPACE_ID", str(WORKSPACE_ID))

    async def fail_transport(**_kwargs: object) -> object:
        raise RuntimeError(f"upstream reflected {secret}")

    monkeypatch.setattr(canary, "_observe_streamable_http", fail_transport)
    exit_code = canary.main(
        [
            "--bundle-dir",
            str(release_bundle),
            "--endpoint",
            "https://example.com/mcp/",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "MCP v1 canary failed: transport_failed\n"
    assert secret not in captured.err
    with pytest.raises(SystemExit):
        canary.main(["--token", secret])
    assert secret not in capsys.readouterr().err


def test_cli_emits_only_non_secret_success_receipt(
    release_bundle: Path,
    runtime_tools: list[Tool],
    contract_result: CallToolResult,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "successful-secret-canary-token"
    monkeypatch.setenv("OMNISCIENCE_MCP_CANARY_TOKEN", secret)
    monkeypatch.setenv("OMNISCIENCE_MCP_CANARY_WORKSPACE_ID", str(WORKSPACE_ID))

    async def observe(**_kwargs: object) -> canary.RuntimeObservation:
        return canary.RuntimeObservation(
            _initialization(),
            runtime_tools,
            contract_result,
        )

    monkeypatch.setattr(canary, "_observe_streamable_http", observe)
    assert (
        canary.main(
            [
                "--bundle-dir",
                str(release_bundle),
                "--endpoint",
                "https://example.com/mcp/",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)

    assert captured.err == ""
    assert receipt["status"] == "passed"
    assert receipt["manifest_sha256"] == release_bundle.name
    assert receipt["workspace_id"] == str(WORKSPACE_ID)
    assert secret not in captured.out


def test_token_validation_is_bounded_and_stable() -> None:
    for token in ("", "contains space", "line\nbreak", "x" * 4097):
        with pytest.raises(canary.CanaryError, match=r"^token_invalid$"):
            canary.validate_token(token)
    assert canary.validate_token("safe-token_123") == "safe-token_123"


@pytest.mark.asyncio
async def test_official_streamable_http_lifecycle_qualifies_real_fastmcp_runtime(
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniscience_server.mcp import server
    from omniscience_server.mcp.mount import create_mcp_asgi_app

    secret = "integration-canary-token"
    now = datetime.now(UTC)
    token = SimpleNamespace(
        id=uuid.uuid4(),
        profile_id="omniscience-mcp-read-v1",
        scopes=["search"],
        workspace_id=WORKSPACE_ID,
        created_at=now,
        expires_at=now + timedelta(days=1),
        is_active=True,
        token_prefix="safe-pre",
    )

    async def resolve_token(ctx: object) -> object:
        request_context = ctx._request_context
        request = request_context.request
        assert server._extract_bearer(request) == secret
        return token

    monkeypatch.setattr(server, "_resolve_token", resolve_token)
    monkeypatch.setenv("OMNISCIENCE_RELEASE_BUILD", "true")
    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    monkeypatch.setenv(
        "OMNISCIENCE_MCP_MANIFEST_PATH",
        str(release_bundle / "manifest.json"),
    )

    monkeypatch.setattr(server, "_fastapi_app", server._fastapi_app)
    app = FastAPI()
    app.mount("/mcp", create_mcp_asgi_app(app))
    transport = httpx.ASGITransport(app=app)
    async with (
        server.mcp_server.session_manager.run(),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
            headers={"Authorization": f"Bearer {secret}"},
            follow_redirects=False,
        ) as client,
    ):
        redirect = await client.post("http://127.0.0.1:8000/mcp")
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "http://127.0.0.1:8000/mcp/"
        observation = await canary._observe_with_client(
            endpoint="http://127.0.0.1:8000/mcp/",
            http_client=client,
        )

    receipt = canary.verify_runtime_observation(
        pin=canary.load_release_pin(release_bundle),
        initialization=observation.initialization,
        tools=observation.tools,
        contract_result=observation.contract_result,
        expected_workspace_id=WORKSPACE_ID,
    )
    assert receipt.status == "passed"
    assert receipt.manifest_sha256 == release_bundle.name


def test_environment_does_not_accidentally_contain_fixture_secret() -> None:
    assert "super-secret-canary-token" not in os.environ.values()
