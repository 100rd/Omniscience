"""Unit tests for the cross-vendor benchmark adapters (issue #241).

Scope:

* Each adapter respects mock-mode (the CI default) and never touches the
  network without explicit ``BENCH_LIVE_VENDORS=1`` opt-in.
* Each adapter's normaliser flattens the vendor's native response into
  the IR-shaped :class:`bench.runner.MCPResponse` correctly.
* The pinned ``bench/vendors/versions.json`` loads and exposes every
  vendor the matrix runner expects.
* The ``runner_matrix`` CLI runs end-to-end against mock adapters.

Live-mode is *not* exercised here (no live external services in CI);
that path is covered by the nightly job in
``.github/workflows/benchmark.yml``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bench.runner import MCPResponse
from bench.vendors import (
    GitHubMCPAdapter,
    HolmesGPTAdapter,
    OmniscienceAdapter,
    SourcegraphMCPAdapter,
    load_vendor_pins,
)
from bench.vendors._base import (
    LIVE_ENV_VAR,
    MockTable,
    is_live_mode,
    load_mock_table,
    stable_key,
)
from bench.vendors.github_mcp_adapter import _github_to_response
from bench.vendors.holmesgpt_adapter import _holmes_to_response
from bench.vendors.sourcegraph_mcp_adapter import _sourcegraph_to_response

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test runs in mock-mode; live-mode requires explicit opt-in."""
    monkeypatch.delenv(LIVE_ENV_VAR, raising=False)
    yield


def _mock_table_with(payload: dict[str, Any], response: MCPResponse) -> MockTable:
    return MockTable(by_key={stable_key(payload): response})


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class TestSharedBase:
    def test_stable_key_is_order_independent(self) -> None:
        a = stable_key({"a": 1, "b": 2})
        b = stable_key({"b": 2, "a": 1})
        assert a == b

    def test_is_live_mode_false_by_default(self) -> None:
        assert is_live_mode() is False

    def test_is_live_mode_truthy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for truthy in ("1", "true", "YES", "on"):
            monkeypatch.setenv(LIVE_ENV_VAR, truthy)
            assert is_live_mode() is True

    def test_mock_table_miss_raises(self) -> None:
        table = MockTable(by_key={})
        with pytest.raises(KeyError):
            table.lookup({"x": 1})

    def test_mock_table_on_miss_callback(self) -> None:
        table = MockTable(by_key={}, on_miss=lambda _p: MCPResponse(("fallback",), 0.1))
        resp = table.lookup({"x": 1})
        assert resp.retrieved_entities == ("fallback",)

    def test_load_mock_table_round_trip(self, tmp_path: Path) -> None:
        payload = {"a": 1}
        fixture = {
            stable_key(payload): {
                "retrieved_entities": ["x", "y"],
                "confidence": 0.7,
            }
        }
        path = tmp_path / "m.json"
        path.write_text(json.dumps(fixture))
        table = load_mock_table(path)
        resp = table.lookup(payload)
        assert resp.retrieved_entities == ("x", "y")
        assert resp.confidence == pytest.approx(0.7)

    def test_load_mock_table_rejects_non_string_entities(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps({stable_key({"a": 1}): {"retrieved_entities": [1, 2], "confidence": 0.5}})
        )
        with pytest.raises(ValueError, match="retrieved_entities"):
            load_mock_table(path)


# ---------------------------------------------------------------------------
# Pinned versions
# ---------------------------------------------------------------------------


class TestVendorVersions:
    def test_load_default_versions_file(self) -> None:
        pins = load_vendor_pins()
        # Every vendor the matrix runner constructs must be pinned.
        for name in ("omniscience", "holmesgpt", "sourcegraph_mcp", "github_mcp"):
            assert name in pins, f"missing pin for {name}"
            pin = pins[name]
            assert pin.endpoint_env, f"{name} missing endpoint_env"
            assert pin.endpoint_default.startswith(("http://", "https://"))

    def test_versions_json_is_machine_parseable(self) -> None:
        path = Path(__file__).resolve().parents[1] / "bench" / "vendors" / "versions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert "as_of" in payload
        assert set(payload["vendors"].keys()) >= {
            "omniscience",
            "holmesgpt",
            "sourcegraph_mcp",
            "github_mcp",
        }


# ---------------------------------------------------------------------------
# Per-adapter mock-mode behavior + normaliser unit tests
# ---------------------------------------------------------------------------


class TestOmniscienceAdapter:
    def test_uses_mock_table_when_not_live(self) -> None:
        payload = {"alert": 1}
        table = _mock_table_with(payload, MCPResponse(("ent-1",), 0.9))
        adapter = OmniscienceAdapter(mock_table=table)
        resp = adapter.resolve_incident(payload, timeout=1.0)
        assert resp.retrieved_entities == ("ent-1",)
        assert resp.confidence == pytest.approx(0.9)

    def test_unregistered_payload_raises_on_mock_miss(self) -> None:
        # An explicitly-empty mock table makes a miss loud — surfaces
        # CI mismatches between the corpus and the canned response set.
        adapter = OmniscienceAdapter(mock_table=MockTable(by_key={}))
        with pytest.raises(KeyError):
            adapter.resolve_incident({"never": "registered"}, timeout=1.0)

    def test_endpoint_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_MCP_ENDPOINT", "http://example.invalid:1234")
        adapter = OmniscienceAdapter(mock_table=MockTable(by_key={}))
        assert adapter.endpoint == "http://example.invalid:1234"


class TestHolmesGPTAdapter:
    def test_uses_mock_table_when_not_live(self) -> None:
        payload = {"alert": "x"}
        table = _mock_table_with(payload, MCPResponse(("ev://a",), 0.6))
        adapter = HolmesGPTAdapter(mock_table=table)
        resp = adapter.resolve_incident(payload, timeout=1.0)
        assert resp.retrieved_entities == ("ev://a",)

    def test_normaliser_flattens_evidence_in_rank_order(self) -> None:
        bundle = {
            "analysis": "...",
            "evidence": [
                {"uri": "https://x/1", "score": 0.81},
                {"uri": "https://x/2", "score": 0.74},
                {"uri": "https://x/3", "score": 0.55},
            ],
        }
        resp = _holmes_to_response(bundle)
        assert resp.retrieved_entities == (
            "https://x/1",
            "https://x/2",
            "https://x/3",
        )
        assert resp.confidence == pytest.approx(0.81)

    def test_normaliser_skips_malformed_entries(self) -> None:
        bundle = {
            "evidence": [
                {"uri": "https://x/1", "score": 0.5},
                {"score": 0.4},  # missing uri
                "not-a-dict",
            ]
        }
        resp = _holmes_to_response(bundle)
        assert resp.retrieved_entities == ("https://x/1",)

    def test_normaliser_handles_empty_evidence(self) -> None:
        resp = _holmes_to_response({"evidence": []})
        assert resp.retrieved_entities == ()
        assert resp.confidence == pytest.approx(0.0)


class TestSourcegraphMCPAdapter:
    def test_uses_mock_table_when_not_live(self) -> None:
        payload = {"alert": "y"}
        table = _mock_table_with(
            payload, MCPResponse(("https://github.com/a/b/blob/HEAD/c",), 0.3)
        )
        adapter = SourcegraphMCPAdapter(mock_table=table)
        resp = adapter.resolve_incident(payload, timeout=1.0)
        assert resp.retrieved_entities == ("https://github.com/a/b/blob/HEAD/c",)

    def test_normaliser_synthesises_canonical_urls(self) -> None:
        bundle = {
            "host": "github.com",
            "results": [
                {"repository": "acme/api", "file": "src/foo.py", "score": 0.92},
                {"repository": "acme/api", "file": "src/bar.py", "score": 0.4},
            ],
        }
        resp = _sourcegraph_to_response(bundle)
        assert resp.retrieved_entities == (
            "https://github.com/acme/api/blob/HEAD/src/foo.py",
            "https://github.com/acme/api/blob/HEAD/src/bar.py",
        )
        assert resp.confidence == pytest.approx(0.92)

    def test_normaliser_handles_missing_results(self) -> None:
        resp = _sourcegraph_to_response({"results": []})
        assert resp.retrieved_entities == ()


class TestGitHubMCPAdapter:
    def test_uses_mock_table_when_not_live(self) -> None:
        payload = {"alert": "z"}
        table = _mock_table_with(
            payload, MCPResponse(("https://github.com/acme/foo/issues/1",), 0.2)
        )
        adapter = GitHubMCPAdapter(mock_table=table)
        resp = adapter.resolve_incident(payload, timeout=1.0)
        assert resp.retrieved_entities == ("https://github.com/acme/foo/issues/1",)

    def test_normaliser_top_level_items(self) -> None:
        bundle = {
            "items": [
                {"html_url": "https://github.com/a/b/issues/1", "score": 7.5},
                {"html_url": "https://github.com/a/b/issues/2", "score": 3.0},
            ]
        }
        resp = _github_to_response(bundle)
        assert resp.retrieved_entities == (
            "https://github.com/a/b/issues/1",
            "https://github.com/a/b/issues/2",
        )
        # 7.5 / 10.0 -> 0.75 (soft-capped normalisation).
        assert resp.confidence == pytest.approx(0.75)

    def test_normaliser_nested_result_items(self) -> None:
        # GitHub MCP wraps responses under {"result": {"items": [...]}}.
        bundle = {
            "result": {"items": [{"html_url": "https://github.com/a/b/issues/9", "score": 0.5}]}
        }
        resp = _github_to_response(bundle)
        assert resp.retrieved_entities == ("https://github.com/a/b/issues/9",)

    def test_normaliser_clamps_score_above_ten(self) -> None:
        bundle = {"items": [{"html_url": "https://x/1", "score": 100.0}]}
        resp = _github_to_response(bundle)
        assert resp.confidence == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Live-mode safety: nothing makes outbound calls without the env var
# ---------------------------------------------------------------------------


class TestLiveModeOptIn:
    def test_holmes_requires_env_for_live_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with mock_table=None, default mode is mock; no network.
        adapter = HolmesGPTAdapter(mock_table=MockTable(by_key={}))
        with pytest.raises(KeyError):
            adapter.resolve_incident({"unmatched": True}, timeout=0.1)

    def test_sourcegraph_live_requires_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LIVE_ENV_VAR, "1")
        monkeypatch.delenv("SOURCEGRAPH_ACCESS_TOKEN", raising=False)
        adapter = SourcegraphMCPAdapter()
        with pytest.raises(RuntimeError, match="SOURCEGRAPH_ACCESS_TOKEN"):
            adapter.resolve_incident({"summary": "x"}, timeout=0.1)
