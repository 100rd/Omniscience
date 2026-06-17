"""Tests for the Terraform config<->state drift engine (Gap C).

Covers ``omniscience_parsers.infra.drift.compute_tf_drift`` over the
``tfstate_of`` graph:

- state-not-in-config: a tfstate_instance whose tfstate_of target has no
  matching terraform_resource node.
- config-not-in-state: a terraform_resource that no tfstate_instance points at.
- attribute divergence: a resource on both sides with a differing attribute
  (and: shared-key-only comparison, no false positives from state-only
  computed attributes, no divergence when attribute maps are absent).
- a clean graph reports no drift.
- determinism: outputs are sorted.
- end-to-end: real TerraformParser + TfStateParser graphs line up on the
  ``resource.{type}.{name}`` symbol so the engine matches them.
"""

from __future__ import annotations

import json

from omniscience_parsers import TerraformParser, TfStateParser, extract_infra_graph
from omniscience_parsers.infra.drift import (
    DriftReport,
    compute_tf_drift,
    drift_report_to_dict,
)
from omniscience_parsers.infra.graph import EdgeData, EntityData


def _config(symbol: str, attributes: dict[str, str] | None = None) -> EntityData:
    extra: dict[str, object] = {"block_type": "resource"}
    if attributes is not None:
        extra["attributes"] = attributes
    return EntityData(
        symbol=symbol, kind="terraform_resource", name=symbol.split(".")[-1], extra=extra
    )


def _state(symbol: str, attributes: dict[str, str] | None = None) -> EntityData:
    extra: dict[str, object] = {}
    if attributes is not None:
        extra["attributes"] = attributes
    return EntityData(
        symbol=symbol, kind="tfstate_instance", name=symbol.split(".")[-1], extra=extra
    )


def _tfstate_of(state_symbol: str, config_symbol: str) -> EdgeData:
    return EdgeData(from_symbol=state_symbol, to_symbol=config_symbol, edge_type="tfstate_of")


# ---------------------------------------------------------------------------
# Membership categories
# ---------------------------------------------------------------------------


def test_clean_graph_reports_no_drift() -> None:
    entities = [_config("resource.aws_s3_bucket.logs"), _state("state.logs")]
    edges = [_tfstate_of("state.logs", "resource.aws_s3_bucket.logs")]

    report = compute_tf_drift(entities, edges)
    assert not report.has_drift
    assert report.state_not_in_config == []
    assert report.config_not_in_state == []
    assert report.attribute_divergences == []


def test_state_not_in_config() -> None:
    # State instance points at a config symbol that does not exist.
    entities = [_state("state.orphan")]
    edges = [_tfstate_of("state.orphan", "resource.aws_s3_bucket.gone")]

    report = compute_tf_drift(entities, edges)
    assert report.state_not_in_config == ["state.orphan"]
    assert report.config_not_in_state == []
    assert report.has_drift


def test_state_with_no_tfstate_of_edge_is_state_not_in_config() -> None:
    # An instance with no tfstate_of edge cannot be matched to config.
    entities = [_state("state.dangling")]
    report = compute_tf_drift(entities, [])
    assert report.state_not_in_config == ["state.dangling"]


def test_config_not_in_state() -> None:
    # Config resource that no instance points at (declared, never applied).
    entities = [
        _config("resource.aws_s3_bucket.logs"),
        _config("resource.aws_sqs_queue.jobs"),
        _state("state.logs"),
    ]
    edges = [_tfstate_of("state.logs", "resource.aws_s3_bucket.logs")]

    report = compute_tf_drift(entities, edges)
    assert report.config_not_in_state == ["resource.aws_sqs_queue.jobs"]
    assert report.state_not_in_config == []


# ---------------------------------------------------------------------------
# Attribute divergence
# ---------------------------------------------------------------------------


def test_attribute_divergence_detected() -> None:
    entities = [
        _config("resource.aws_s3_bucket.logs", attributes={"acl": "private"}),
        _state("state.logs", attributes={"acl": "public-read"}),
    ]
    edges = [_tfstate_of("state.logs", "resource.aws_s3_bucket.logs")]

    report = compute_tf_drift(entities, edges)
    assert len(report.attribute_divergences) == 1
    div = report.attribute_divergences[0]
    assert div.config_symbol == "resource.aws_s3_bucket.logs"
    assert div.state_symbol == "state.logs"
    assert div.attribute == "acl"
    assert div.config_value == "private"
    assert div.state_value == "public-read"


def test_matching_attribute_is_not_drift() -> None:
    entities = [
        _config("resource.aws_s3_bucket.logs", attributes={"acl": "private"}),
        _state("state.logs", attributes={"acl": "private"}),
    ]
    edges = [_tfstate_of("state.logs", "resource.aws_s3_bucket.logs")]
    assert compute_tf_drift(entities, edges).attribute_divergences == []


def test_state_only_attribute_is_not_divergence() -> None:
    # Provider-computed attribute present only in state (arn) is NOT drift.
    entities = [
        _config("resource.aws_s3_bucket.logs", attributes={"acl": "private"}),
        _state("state.logs", attributes={"acl": "private", "arn": "arn:aws:s3:::x"}),
    ]
    edges = [_tfstate_of("state.logs", "resource.aws_s3_bucket.logs")]
    assert compute_tf_drift(entities, edges).attribute_divergences == []


def test_absent_attribute_maps_skip_attribute_comparison() -> None:
    # Neither side carries an attributes map -> membership only, no divergence.
    entities = [
        _config("resource.aws_s3_bucket.logs"),
        _state("state.logs"),
    ]
    edges = [_tfstate_of("state.logs", "resource.aws_s3_bucket.logs")]
    assert compute_tf_drift(entities, edges).attribute_divergences == []


# ---------------------------------------------------------------------------
# Determinism + serialisation
# ---------------------------------------------------------------------------


def test_outputs_are_sorted() -> None:
    entities = [
        _config("resource.b.two"),
        _config("resource.a.one"),
        _state("state.x"),
        _state("state.y"),
    ]
    edges = [
        _tfstate_of("state.y", "resource.missing.z"),
        _tfstate_of("state.x", "resource.missing.q"),
    ]
    report = compute_tf_drift(entities, edges)
    assert report.config_not_in_state == ["resource.a.one", "resource.b.two"]
    assert report.state_not_in_config == ["state.x", "state.y"]


def test_drift_report_to_dict_shape() -> None:
    report = DriftReport(
        state_not_in_config=["state.orphan"],
        config_not_in_state=["resource.a.one"],
    )
    out = drift_report_to_dict(report)
    assert out["has_drift"] is True
    assert out["state_not_in_config"] == ["state.orphan"]
    assert out["config_not_in_state"] == ["resource.a.one"]
    assert out["attribute_divergences"] == []


def test_empty_graph_is_clean() -> None:
    assert not compute_tf_drift([], []).has_drift


# ---------------------------------------------------------------------------
# End-to-end: real parsers -> graph -> engine
# ---------------------------------------------------------------------------


def _state_doc(resource_type: str, name: str, attrs: dict) -> bytes:
    return json.dumps(
        {
            "version": 4,
            "terraform_version": "1.5.0",
            "serial": 1,
            "lineage": "abc",
            "outputs": {},
            "resources": [
                {
                    "mode": "managed",
                    "type": resource_type,
                    "name": name,
                    "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                    "instances": [
                        {"schema_version": 0, "attributes": attrs, "sensitive_attributes": []}
                    ],
                }
            ],
        }
    ).encode()


def test_end_to_end_config_in_state_matches() -> None:
    config = 'resource "aws_s3_bucket" "logs" {\n  bucket = "my-logs"\n}\n'
    config_parsed = TerraformParser().parse(config.encode("utf-8"))
    c_ents, c_edges = extract_infra_graph(config_parsed)

    state_parsed = TfStateParser().parse(
        _state_doc("aws_s3_bucket", "logs", {"id": "my-logs", "bucket": "my-logs"})
    )
    s_ents, s_edges = extract_infra_graph(state_parsed)

    report = compute_tf_drift([*c_ents, *s_ents], [*c_edges, *s_edges])
    # The config resource and the state instance match via the tfstate_of edge,
    # so neither membership category fires.
    assert report.config_not_in_state == []
    assert report.state_not_in_config == []


def test_end_to_end_config_without_state_is_config_not_in_state() -> None:
    config = 'resource "aws_sqs_queue" "jobs" {\n  name = "jobs"\n}\n'
    config_parsed = TerraformParser().parse(config.encode("utf-8"))
    c_ents, c_edges = extract_infra_graph(config_parsed)

    report = compute_tf_drift(c_ents, c_edges)
    assert report.config_not_in_state == ["resource.aws_sqs_queue.jobs"]
