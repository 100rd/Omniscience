"""Tests for TfStateParser and extract_tfstate_graph.

Covers Issue #86 — Terraform state parser for Omniscience.
"""

from __future__ import annotations

import json

from omniscience_parsers import (
    ParsedDocument,
    TfStateParser,
    default_dispatch,
    extract_infra_graph,
)
from omniscience_parsers.infra.graph import extract_tfstate_graph

# ============================================================================
# Fixture helpers
# ============================================================================


def _make_state(
    resources: list[dict],
    version: int = 4,
    terraform_version: str = "1.5.0",
    serial: int = 42,
    lineage: str = "abc-123",
) -> bytes:
    """Build a minimal v4 tfstate JSON bytes fixture."""
    return json.dumps(
        {
            "version": version,
            "terraform_version": terraform_version,
            "serial": serial,
            "lineage": lineage,
            "outputs": {},
            "resources": resources,
        }
    ).encode()


def _s3_resource(
    name: str = "logs",
    bucket: str = "my-logs-bucket",
    arn: str = "arn:aws:s3:::my-logs-bucket",
    region: str = "us-east-1",
    account_id: str = "123456789012",
    module: str = "",
) -> dict:
    instance: dict = {
        "schema_version": 0,
        "attributes": {
            "id": bucket,
            "arn": arn,
            "bucket": bucket,
            "region": region,
            "account_id": account_id,
            "tags": {},
        },
        "sensitive_attributes": [],
    }
    resource: dict = {
        "mode": "managed",
        "type": "aws_s3_bucket",
        "name": name,
        "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
        "instances": [instance],
    }
    if module:
        resource["module"] = module
    return resource


# ============================================================================
# Fixture state documents
# ============================================================================

_SIMPLE_S3 = _make_state([_s3_resource()])

_MULTI_RESOURCE = _make_state(
    [
        _s3_resource(name="logs"),
        {
            "mode": "managed",
            "type": "aws_iam_role",
            "name": "app",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "schema_version": 0,
                    "attributes": {
                        "id": "app-role",
                        "arn": "arn:aws:iam::123456789012:role/app-role",
                        "name": "app-role",
                    },
                    "sensitive_attributes": [],
                }
            ],
        },
    ]
)

_WITH_MODULE = _make_state([_s3_resource(name="private", module="module.storage")])

_DATA_SOURCE = _make_state(
    [
        {
            "mode": "data",
            "type": "aws_ami",
            "name": "ubuntu",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "schema_version": 0,
                    "attributes": {
                        "id": "ami-0abc123",
                        "name": "ubuntu-22-04",
                    },
                    "sensitive_attributes": [],
                }
            ],
        }
    ]
)

_FOR_EACH_RESOURCE = _make_state(
    [
        {
            "mode": "managed",
            "type": "aws_s3_bucket",
            "name": "env_buckets",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "index_key": "dev",
                    "schema_version": 0,
                    "attributes": {
                        "id": "dev-bucket",
                        "arn": "arn:aws:s3:::dev-bucket",
                    },
                    "sensitive_attributes": [],
                },
                {
                    "index_key": "prod",
                    "schema_version": 0,
                    "attributes": {
                        "id": "prod-bucket",
                        "arn": "arn:aws:s3:::prod-bucket",
                    },
                    "sensitive_attributes": [],
                },
            ],
        }
    ]
)

_COUNT_RESOURCE = _make_state(
    [
        {
            "mode": "managed",
            "type": "aws_instance",
            "name": "workers",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "index_key": 0,
                    "schema_version": 0,
                    "attributes": {"id": "i-aaa", "region": "us-east-1"},
                    "sensitive_attributes": [],
                },
                {
                    "index_key": 1,
                    "schema_version": 0,
                    "attributes": {"id": "i-bbb", "region": "us-east-1"},
                    "sensitive_attributes": [],
                },
            ],
        }
    ]
)

_EMPTY_STATE = _make_state([])

_NO_INSTANCES = _make_state(
    [
        {
            "mode": "managed",
            "type": "aws_s3_bucket",
            "name": "orphan",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [],
        }
    ]
)

_MISSING_ARN = _make_state(
    [
        {
            "mode": "managed",
            "type": "aws_lambda_function",
            "name": "handler",
            "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
            "instances": [
                {
                    "schema_version": 0,
                    "attributes": {
                        "id": "handler",
                        "function_arn": "arn:aws:lambda:us-east-1:123:function:handler",
                    },
                    "sensitive_attributes": [],
                }
            ],
        }
    ]
)

_UNKNOWN_VERSION = json.dumps(
    {
        "version": 3,
        "terraform_version": "0.12.0",
        "serial": 1,
        "resources": [],
    }
).encode()

_INVALID_JSON = b"{not valid json}"

_NOT_AN_OBJECT = b"[1, 2, 3]"


# ============================================================================
# TfStateParser.can_handle tests
# ============================================================================


class TestTfStateParserCanHandle:
    def setup_method(self) -> None:
        self.parser = TfStateParser()

    def test_handles_tfstate_extension(self) -> None:
        assert self.parser.can_handle("", ".tfstate") is True

    def test_handles_tfstate_extension_uppercase(self) -> None:
        assert self.parser.can_handle("", ".TFSTATE") is True

    def test_handles_tfstate_content_type(self) -> None:
        assert self.parser.can_handle("application/x-tfstate", "") is True

    def test_does_not_handle_tf_extension(self) -> None:
        assert self.parser.can_handle("", ".tf") is False

    def test_does_not_handle_json_extension_alone(self) -> None:
        assert self.parser.can_handle("application/json", ".json") is False

    def test_does_not_handle_yaml(self) -> None:
        assert self.parser.can_handle("", ".yaml") is False

    def test_does_not_handle_python(self) -> None:
        assert self.parser.can_handle("text/x-python", ".py") is False


# ============================================================================
# TfStateParser.parse — basic section extraction
# ============================================================================


class TestTfStateParserParse:
    def setup_method(self) -> None:
        self.parser = TfStateParser()

    def test_parse_returns_parsed_document(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert isinstance(doc, ParsedDocument)

    def test_content_type_is_tfstate(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.content_type == "application/x-tfstate"

    def test_language_is_json(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.language == "json"

    def test_single_resource_one_section(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert len(doc.sections) == 1

    def test_section_symbol_is_resource_address(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.sections[0].symbol == "aws_s3_bucket.logs"

    def test_section_heading_path(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        s = doc.sections[0]
        assert s.heading_path == ["aws_s3_bucket", "logs"]

    def test_section_text_is_valid_json(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        # Should not raise
        attrs = json.loads(doc.sections[0].text)
        assert isinstance(attrs, dict)

    def test_section_text_contains_attributes(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        attrs = json.loads(doc.sections[0].text)
        assert attrs["bucket"] == "my-logs-bucket"

    def test_metadata_arn(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.sections[0].metadata["arn"] == "arn:aws:s3:::my-logs-bucket"

    def test_metadata_id(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.sections[0].metadata["id"] == "my-logs-bucket"

    def test_metadata_region(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.sections[0].metadata["region"] == "us-east-1"

    def test_metadata_provider(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        meta = doc.sections[0].metadata
        assert "hashicorp/aws" in meta["provider"]

    def test_metadata_mode_managed(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.sections[0].metadata["mode"] == "managed"

    def test_doc_metadata_terraform_version(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.metadata["terraform_version"] == "1.5.0"

    def test_doc_metadata_serial(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        assert doc.metadata["serial"] == 42

    def test_line_numbers_positive(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        for section in doc.sections:
            assert section.line_start >= 1
            assert section.line_end >= section.line_start


# ============================================================================
# Multi-resource, modules, data sources, for_each / count
# ============================================================================


class TestTfStateParserMultiResource:
    def setup_method(self) -> None:
        self.parser = TfStateParser()

    def test_multi_resource_section_count(self) -> None:
        doc = self.parser.parse(_MULTI_RESOURCE, "terraform.tfstate")
        assert len(doc.sections) == 2

    def test_multi_resource_symbols(self) -> None:
        doc = self.parser.parse(_MULTI_RESOURCE, "terraform.tfstate")
        symbols = [s.symbol for s in doc.sections]
        assert "aws_s3_bucket.logs" in symbols
        assert "aws_iam_role.app" in symbols

    def test_module_resource_symbol_has_module_prefix(self) -> None:
        doc = self.parser.parse(_WITH_MODULE, "terraform.tfstate")
        assert doc.sections[0].symbol == "module.storage.aws_s3_bucket.private"

    def test_module_resource_heading_path_starts_with_module(self) -> None:
        doc = self.parser.parse(_WITH_MODULE, "terraform.tfstate")
        assert doc.sections[0].heading_path[0] == "module.storage"

    def test_data_source_symbol_has_data_prefix(self) -> None:
        doc = self.parser.parse(_DATA_SOURCE, "terraform.tfstate")
        assert doc.sections[0].symbol == "data.aws_ami.ubuntu"

    def test_data_source_mode_is_data(self) -> None:
        doc = self.parser.parse(_DATA_SOURCE, "terraform.tfstate")
        assert doc.sections[0].metadata["mode"] == "data"

    def test_for_each_creates_two_sections(self) -> None:
        doc = self.parser.parse(_FOR_EACH_RESOURCE, "terraform.tfstate")
        assert len(doc.sections) == 2

    def test_for_each_symbols_include_key(self) -> None:
        doc = self.parser.parse(_FOR_EACH_RESOURCE, "terraform.tfstate")
        symbols = {s.symbol for s in doc.sections}
        assert 'aws_s3_bucket.env_buckets["dev"]' in symbols
        assert 'aws_s3_bucket.env_buckets["prod"]' in symbols

    def test_count_symbols_include_index(self) -> None:
        doc = self.parser.parse(_COUNT_RESOURCE, "terraform.tfstate")
        symbols = {s.symbol for s in doc.sections}
        assert "aws_instance.workers[0]" in symbols
        assert "aws_instance.workers[1]" in symbols

    def test_empty_state_no_sections(self) -> None:
        doc = self.parser.parse(_EMPTY_STATE, "terraform.tfstate")
        assert doc.sections == []

    def test_no_instances_produces_no_sections(self) -> None:
        doc = self.parser.parse(_NO_INSTANCES, "terraform.tfstate")
        assert doc.sections == []

    def test_fallback_arn_key_function_arn(self) -> None:
        doc = self.parser.parse(_MISSING_ARN, "terraform.tfstate")
        meta = doc.sections[0].metadata
        assert meta["arn"] == "arn:aws:lambda:us-east-1:123:function:handler"


# ============================================================================
# Error handling
# ============================================================================


class TestTfStateParserErrors:
    def setup_method(self) -> None:
        self.parser = TfStateParser()

    def test_invalid_json_returns_empty_document(self) -> None:
        doc = self.parser.parse(_INVALID_JSON, "bad.tfstate")
        assert doc.sections == []

    def test_invalid_json_sets_parse_error(self) -> None:
        doc = self.parser.parse(_INVALID_JSON, "bad.tfstate")
        assert "parse_error" in doc.metadata

    def test_unknown_version_returns_empty_document(self) -> None:
        doc = self.parser.parse(_UNKNOWN_VERSION, "old.tfstate")
        assert doc.sections == []

    def test_unknown_version_sets_parse_error(self) -> None:
        doc = self.parser.parse(_UNKNOWN_VERSION, "old.tfstate")
        assert "parse_error" in doc.metadata
        assert "3" in doc.metadata["parse_error"]

    def test_non_object_json_returns_empty(self) -> None:
        doc = self.parser.parse(_NOT_AN_OBJECT, "array.tfstate")
        assert doc.sections == []

    def test_none_version_returns_error(self) -> None:
        data = json.dumps({"version": None, "resources": []}).encode()
        doc = self.parser.parse(data, "null-ver.tfstate")
        assert "parse_error" in doc.metadata

    def test_content_type_always_set_on_error(self) -> None:
        doc = self.parser.parse(_INVALID_JSON, "bad.tfstate")
        assert doc.content_type == "application/x-tfstate"


# ============================================================================
# Graph extraction — extract_tfstate_graph
# ============================================================================


class TestExtractTfstateGraph:
    def setup_method(self) -> None:
        self.parser = TfStateParser()

    def test_returns_entities_for_resource(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        entities, _ = extract_tfstate_graph(doc)
        symbols = [e.symbol for e in entities]
        assert "aws_s3_bucket.logs" in symbols

    def test_entity_kind_is_tfstate_instance(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        entities, _ = extract_tfstate_graph(doc)
        entity = next(e for e in entities if e.symbol == "aws_s3_bucket.logs")
        assert entity.kind == "tfstate_instance"

    def test_entity_extra_contains_arn(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        entities, _ = extract_tfstate_graph(doc)
        entity = next(e for e in entities if e.symbol == "aws_s3_bucket.logs")
        assert entity.extra["arn"] == "arn:aws:s3:::my-logs-bucket"

    def test_entity_extra_contains_id(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        entities, _ = extract_tfstate_graph(doc)
        entity = next(e for e in entities if e.symbol == "aws_s3_bucket.logs")
        assert entity.extra["id"] == "my-logs-bucket"

    def test_entity_extra_contains_region(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        entities, _ = extract_tfstate_graph(doc)
        entity = next(e for e in entities if e.symbol == "aws_s3_bucket.logs")
        assert entity.extra["region"] == "us-east-1"

    def test_tfstate_of_edge_produced(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        _, edges = extract_tfstate_graph(doc)
        assert len(edges) == 1
        assert edges[0].edge_type == "tfstate_of"

    def test_tfstate_of_edge_from_symbol(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        _, edges = extract_tfstate_graph(doc)
        assert edges[0].from_symbol == "aws_s3_bucket.logs"

    def test_tfstate_of_edge_to_symbol_is_config_address(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        _, edges = extract_tfstate_graph(doc)
        # Config address uses "resource." prefix
        assert edges[0].to_symbol == "resource.aws_s3_bucket.logs"

    def test_data_source_tfstate_of_edge_uses_data_prefix(self) -> None:
        doc = self.parser.parse(_DATA_SOURCE, "terraform.tfstate")
        _, edges = extract_tfstate_graph(doc)
        assert edges[0].to_symbol == "data.aws_ami.ubuntu"

    def test_empty_state_no_entities(self) -> None:
        doc = self.parser.parse(_EMPTY_STATE, "terraform.tfstate")
        entities, edges = extract_tfstate_graph(doc)
        assert entities == []
        assert edges == []

    def test_non_tfstate_doc_returns_empty(self) -> None:
        from omniscience_parsers import MarkdownParser

        doc = MarkdownParser().parse(b"# Hello\n", "README.md")
        entities, edges = extract_tfstate_graph(doc)
        assert entities == []
        assert edges == []

    def test_multi_resource_multiple_entities(self) -> None:
        doc = self.parser.parse(_MULTI_RESOURCE, "terraform.tfstate")
        entities, _ = extract_tfstate_graph(doc)
        assert len(entities) == 2

    def test_for_each_multiple_entities(self) -> None:
        doc = self.parser.parse(_FOR_EACH_RESOURCE, "terraform.tfstate")
        entities, edges = extract_tfstate_graph(doc)
        assert len(entities) == 2
        assert len(edges) == 2


# ============================================================================
# extract_infra_graph dispatches to tfstate extractor
# ============================================================================


class TestExtractInfraGraphDispatchesTfstate:
    def setup_method(self) -> None:
        self.parser = TfStateParser()

    def test_extract_infra_graph_handles_tfstate_doc(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        entities, edges = extract_infra_graph(doc)
        assert any(e.symbol == "aws_s3_bucket.logs" for e in entities)
        assert any(e.edge_type == "tfstate_of" for e in edges)

    def test_extract_infra_graph_entity_kind(self) -> None:
        doc = self.parser.parse(_SIMPLE_S3, "terraform.tfstate")
        entities, _ = extract_infra_graph(doc)
        entity = next(e for e in entities if e.symbol == "aws_s3_bucket.logs")
        assert entity.kind == "tfstate_instance"


# ============================================================================
# Dispatch routing
# ============================================================================


class TestDispatchRoutesTfstate:
    def setup_method(self) -> None:
        self.dispatch = default_dispatch()

    def test_routes_tfstate_extension(self) -> None:
        parser = self.dispatch.get_parser("", ".tfstate")
        assert isinstance(parser, TfStateParser)

    def test_routes_tfstate_content_type(self) -> None:
        parser = self.dispatch.get_parser("application/x-tfstate", "")
        assert isinstance(parser, TfStateParser)

    def test_tfstate_does_not_intercept_tf_extension(self) -> None:
        from omniscience_parsers import TerraformParser

        parser = self.dispatch.get_parser("", ".tf")
        assert isinstance(parser, TerraformParser)

    def test_end_to_end_parse_via_dispatch(self) -> None:
        doc = self.dispatch.parse(
            _SIMPLE_S3,
            content_type="",
            file_extension=".tfstate",
            file_path="terraform.tfstate",
        )
        assert doc.content_type == "application/x-tfstate"
        symbols = [s.symbol for s in doc.sections]
        assert "aws_s3_bucket.logs" in symbols
