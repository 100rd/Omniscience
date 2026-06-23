"""Unit tests for the MCP Apps card wrapper (issue #238).

Covers:

1. **Card schema** — :class:`ResolveIncidentCard` validates a
   well-formed payload and rejects bad shapes.
2. **Pure builder** — ``build_resolve_incident_card`` produces a
   stable, deterministic card from the legacy ``resolve_incident``
   response dict; tolerates absence of optional fields
   (``target_resource``, ``responsible_pr``, ``similar_past``).
3. **Backwards-compat** — ``wrap_resolve_incident_response`` returns
   the legacy dict **unchanged** when the client does not advertise
   ``omniscience/apps``.  This is the non-negotiable invariant from
   the issue spec.
4. **Capability detection** — ``client_supports_apps`` parses both
   pydantic ``ClientCapabilities`` and plain ``dict`` shapes, and
   degrades to ``False`` on anything malformed.
5. **#233 forward-compat** — when the inner response carries a
   ``similar_past`` field (added by the parallel agent for #233)
   the card surfaces it; when the field is absent the card is built
   without it (no ``KeyError``).
6. **Bucket boundaries** — ``bucket_for_score`` honours the
   documented 0.8/0.5/0.2 thresholds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from omniscience_server.mcp.apps import (
    APPS_CAPABILITY_KEY,
    APPS_SCHEMA_VERSION,
    ResolveIncidentCard,
    build_resolve_incident_card,
    client_supports_apps,
    should_emit_card,
    wrap_resolve_incident_response,
)
from omniscience_server.mcp.apps.card_schemas import (
    BUCKET_HIGH_THRESHOLD,
    BUCKET_LOW_THRESHOLD,
    BUCKET_MEDIUM_THRESHOLD,
    bucket_for_score,
)

# ---------------------------------------------------------------------------
# Test fixtures — minimal legacy responses
# ---------------------------------------------------------------------------

_ALERT_ID = "alert://pagerduty/INC-9001"
_ALERT_FIRED_AT = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC).isoformat()
_PR_URI = "https://github.com/acme/api/pull/42"
_PR_MERGED_AT = datetime(2026, 5, 22, 11, 30, 0, tzinfo=UTC).isoformat()
_RESOURCE_NAME = "default/api"
_SLACK_THREAD = "slack://channel/C12345/thread/1716383700.000100"


def _legacy_full() -> dict[str, Any]:
    """A legacy response with every optional block populated."""
    return {
        "alert": {
            "name": _ALERT_ID,
            "kind": "alert",
            "source": "pagerduty",
            "chunk_text": "API 500s spiking on /v1/widgets",
            "valid_from": _ALERT_FIRED_AT,
        },
        "target_resource": {
            "name": _RESOURCE_NAME,
            "kind": "k8s_deployment",
            "source": "kubernetes",
            "edge_type": "alerts_on",
        },
        "responsible_pr": {
            "name": _PR_URI,
            "kind": "pull_request",
            "source": "github",
            "edge_type": "deployed_to",
            "merged_at": _PR_MERGED_AT,
        },
        "slack_threads": [
            {
                "name": _SLACK_THREAD,
                "kind": "slack_thread",
                "source": "slack",
                "chunk_text": "@oncall I'm seeing the same",
                "edge_type": "discusses",
            }
        ],
        "resolution_confidence": 0.9,
        "effective_as_of": _ALERT_FIRED_AT,
        "meta": None,
    }


def _legacy_alert_only() -> dict[str, Any]:
    """A legacy response with no neighbours — score 0.1."""
    return {
        "alert": {
            "name": _ALERT_ID,
            "kind": "alert",
            "source": "pagerduty",
            "chunk_text": None,
            "valid_from": _ALERT_FIRED_AT,
        },
        "target_resource": None,
        "responsible_pr": None,
        "slack_threads": [],
        "resolution_confidence": 0.1,
        "effective_as_of": _ALERT_FIRED_AT,
        "meta": None,
    }


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


class TestClientSupportsApps:
    def test_dict_with_capability_returns_true(self) -> None:
        caps = {"experimental": {APPS_CAPABILITY_KEY: {}}}
        assert client_supports_apps(caps) is True

    def test_dict_without_capability_returns_false(self) -> None:
        caps = {"experimental": {"some.other/cap": {}}}
        assert client_supports_apps(caps) is False

    def test_dict_no_experimental_returns_false(self) -> None:
        assert client_supports_apps({}) is False

    def test_none_returns_false(self) -> None:
        assert client_supports_apps(None) is False

    def test_pydantic_shape_returns_true(self) -> None:
        from mcp.types import ClientCapabilities

        caps = ClientCapabilities(experimental={APPS_CAPABILITY_KEY: {}})
        assert client_supports_apps(caps) is True

    def test_pydantic_without_capability_returns_false(self) -> None:
        from mcp.types import ClientCapabilities

        caps = ClientCapabilities()
        assert client_supports_apps(caps) is False

    def test_garbage_input_returns_false(self) -> None:
        assert client_supports_apps("not a capabilities object") is False
        assert client_supports_apps(42) is False


class TestShouldEmitCard:
    def test_none_ctx_returns_false(self) -> None:
        assert should_emit_card(None) is False

    def test_ctx_with_session_advertising_capability_returns_true(self) -> None:
        client_params = SimpleNamespace(capabilities={"experimental": {APPS_CAPABILITY_KEY: {}}})
        session = SimpleNamespace(client_params=client_params)
        ctx = SimpleNamespace(session=session)
        assert should_emit_card(ctx) is True

    def test_ctx_without_session_returns_false(self) -> None:
        ctx = SimpleNamespace(session=None)
        assert should_emit_card(ctx) is False

    def test_ctx_session_without_client_params_returns_false(self) -> None:
        session = SimpleNamespace(client_params=None)
        ctx = SimpleNamespace(session=session)
        assert should_emit_card(ctx) is False


# ---------------------------------------------------------------------------
# Pure builder
# ---------------------------------------------------------------------------


class TestBuildResolveIncidentCard:
    def test_full_legacy_response_renders_all_sections(self) -> None:
        card = build_resolve_incident_card(_legacy_full())
        assert isinstance(card, ResolveIncidentCard)
        assert card.apps_version == APPS_SCHEMA_VERSION
        assert card.card_type == "resolve_incident"

        # Summary
        assert card.summary.alert_uri == _ALERT_ID
        assert card.summary.headline == "API 500s spiking on /v1/widgets"
        assert card.summary.fired_at == _ALERT_FIRED_AT
        assert card.summary.below_trust_threshold is False

        # Three candidates: PR (rank 1), resource (rank 2), thread (rank 3)
        assert len(card.candidates) == 3
        assert card.candidates[0].rank == 1
        assert card.candidates[0].kind == "pull_request"
        assert card.candidates[0].title == "acme/api#42"
        assert card.candidates[0].confidence.value == 0.9
        assert card.candidates[0].confidence.bucket == "high"

        assert card.candidates[1].rank == 2
        assert card.candidates[1].kind == "resource"
        assert card.candidates[1].title == _RESOURCE_NAME

        assert card.candidates[2].rank == 3
        assert card.candidates[2].kind == "slack_thread"
        assert card.candidates[2].title.startswith("#C12345 / ")

        # Citations
        assert card.candidates[0].citations[0].uri == _PR_URI
        assert card.candidates[1].citations[0].uri == _RESOURCE_NAME
        assert card.candidates[2].citations[0].uri == _SLACK_THREAD

    def test_alert_only_renders_empty_candidates(self) -> None:
        card = build_resolve_incident_card(_legacy_alert_only())
        assert card.candidates == []
        assert card.summary.alert_uri == _ALERT_ID
        # Headline falls back to the tail of the alert URI.
        assert card.summary.headline == "INC-9001"

    def test_below_trust_threshold_propagates_to_summary(self) -> None:
        legacy = _legacy_alert_only()
        legacy["meta"] = {"below_trust_threshold": True, "confidence_threshold": 0.5}
        card = build_resolve_incident_card(legacy)
        assert card.summary.below_trust_threshold is True

    def test_top_3_cap_truncates_extra_candidates(self) -> None:
        # Even if upstream changes one day surface >3 candidates, the card
        # is capped — pin the contract.
        legacy = _legacy_full()
        legacy["slack_threads"] = [legacy["slack_threads"][0]] * 5
        card = build_resolve_incident_card(legacy)
        assert len(card.candidates) <= 3

    def test_card_is_deterministic_for_identical_input(self) -> None:
        card_a = build_resolve_incident_card(_legacy_full()).model_dump(mode="json")
        card_b = build_resolve_incident_card(_legacy_full()).model_dump(mode="json")
        assert card_a == card_b

    def test_missing_alert_block_raises(self) -> None:
        with pytest.raises(KeyError):
            build_resolve_incident_card({"resolution_confidence": 0.1})

    def test_malformed_alert_block_raises_typeerror(self) -> None:
        with pytest.raises(TypeError):
            build_resolve_incident_card({"alert": "not a dict", "resolution_confidence": 0.1})


# ---------------------------------------------------------------------------
# Backwards-compat invariant
# ---------------------------------------------------------------------------


class TestWrapResolveIncidentResponseBackwardsCompat:
    def test_legacy_client_gets_unchanged_response(self) -> None:
        legacy = _legacy_full()
        ctx = SimpleNamespace(session=None)
        wrapped = wrap_resolve_incident_response(legacy, ctx=ctx)
        # The same object is returned; no _meta key was injected.
        assert wrapped is legacy
        assert "_meta" not in wrapped

    def test_client_without_capability_gets_unchanged_response(self) -> None:
        legacy = _legacy_full()
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(capabilities={"experimental": {}})
            )
        )
        wrapped = wrap_resolve_incident_response(legacy, ctx=ctx)
        assert "_meta" not in wrapped

    def test_supported_client_gets_card_under_meta(self) -> None:
        legacy = _legacy_full()
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities={"experimental": {APPS_CAPABILITY_KEY: {}}}
                )
            )
        )
        wrapped = wrap_resolve_incident_response(legacy, ctx=ctx)
        assert "_meta" in wrapped
        assert APPS_CAPABILITY_KEY in wrapped["_meta"]
        card = wrapped["_meta"][APPS_CAPABILITY_KEY]
        # Re-validate to ensure it round-trips through the schema.
        ResolveIncidentCard.model_validate(card)
        # Legacy fields preserved verbatim.
        for key in legacy:
            assert wrapped[key] == legacy[key]

    def test_wrapper_does_not_mutate_legacy_dict(self) -> None:
        legacy = _legacy_full()
        snapshot = {**legacy}
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities={"experimental": {APPS_CAPABILITY_KEY: {}}}
                )
            )
        )
        wrap_resolve_incident_response(legacy, ctx=ctx)
        assert legacy == snapshot, "wrap_resolve_incident_response mutated legacy input"

    def test_malformed_legacy_falls_back_to_legacy(self) -> None:
        # A legacy dict that fails the card builder must NOT propagate
        # — the user gets the legacy response back, no exception.
        legacy = {"resolution_confidence": 0.5}  # missing 'alert' block
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities={"experimental": {APPS_CAPABILITY_KEY: {}}}
                )
            )
        )
        wrapped = wrap_resolve_incident_response(legacy, ctx=ctx)
        assert wrapped == legacy
        assert "_meta" not in wrapped


# ---------------------------------------------------------------------------
# Forward-compat with #233 (similar_past)
# ---------------------------------------------------------------------------


class TestSimilarPastForwardCompat:
    def test_similar_past_absent_does_not_break_card(self) -> None:
        legacy = _legacy_full()
        assert "similar_past" not in legacy
        card = build_resolve_incident_card(legacy)
        assert card.similar_past == []

    def test_similar_past_present_renders_rows(self) -> None:
        legacy = _legacy_full()
        legacy["similar_past"] = [
            {
                "alert_id": "alert://pagerduty/INC-1000",
                "title": "Earlier API 500s spike",
                "occurred_at": "2026-05-15T12:00:00+00:00",
                "similarity_score": 0.87,
                "resolution_hint": "Rolled back PR #41",
            },
            {
                "alert_id": "alert://pagerduty/INC-0999",
                "title": "Latency regression",
                "similarity_score": 0.71,
            },
        ]
        card = build_resolve_incident_card(legacy)
        assert len(card.similar_past) == 2
        first = card.similar_past[0]
        assert first.alert_id == "alert://pagerduty/INC-1000"
        assert first.similarity_score == 0.87
        assert first.resolution_hint == "Rolled back PR #41"
        assert card.similar_past[1].similarity_score == 0.71

    def test_similar_past_malformed_entries_skipped(self) -> None:
        legacy = _legacy_full()
        legacy["similar_past"] = [
            "not a dict",
            {},  # missing alert_id
            {"alert_id": ""},  # empty alert_id
            {"alert_id": "alert://x/y", "title": "ok"},  # valid
            {"alert_id": "alert://a/b", "title": "ok2", "similarity_score": 1.5},  # OOB clamped
        ]
        card = build_resolve_incident_card(legacy)
        # Two valid rows survive; second one drops similarity_score.
        assert len(card.similar_past) == 2
        assert card.similar_past[0].alert_id == "alert://x/y"
        assert card.similar_past[1].similarity_score is None

    def test_similar_past_not_a_list_is_silently_ignored(self) -> None:
        legacy = _legacy_full()
        legacy["similar_past"] = "garbage"
        card = build_resolve_incident_card(legacy)
        assert card.similar_past == []


# ---------------------------------------------------------------------------
# Bucket boundaries
# ---------------------------------------------------------------------------


class TestBucketBoundaries:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (1.0, "high"),
            (BUCKET_HIGH_THRESHOLD, "high"),
            (BUCKET_HIGH_THRESHOLD - 0.01, "medium"),
            (BUCKET_MEDIUM_THRESHOLD, "medium"),
            (BUCKET_MEDIUM_THRESHOLD - 0.01, "low"),
            (BUCKET_LOW_THRESHOLD, "low"),
            (BUCKET_LOW_THRESHOLD - 0.01, "very_low"),
            (0.0, "very_low"),
        ],
    )
    def test_bucket_boundaries(self, score: float, expected: str) -> None:
        assert bucket_for_score(score) == expected
