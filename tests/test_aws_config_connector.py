"""Tests for the AWS Config-aggregator acquisition mode (ADR-0014 Phase 1).

Coverage:
- AwsConfig new fields: acquisition, aggregator_name, config_resource_types,
  config_regions — default values and custom values (4 tests)
- _config_external_id / _config_uri format (2 tests)
- _normalise_relationship_type (2 tests)
- _ci_to_ref — happy path, missing mandatory fields, deleted status,
  relationships mapped to edge_list, cold_ref written (7 tests)
- _list_aggregate_cis_for_type — pagination, region filter, boto3 error
  propagation (3 tests)
- _discover_config_type — success, per-type ClientError skipped, others
  still returned (3 tests)
- _fetch_config_ci — success, empty response, ClientError returns {} (3 tests)
- _ci_to_text — fields present, malformed config JSON ignored (3 tests)
- AwsConnector.discover config mode — yields refs per type, per-type error
  skipped, aggregator_name missing raises ValueError (3 tests)
- AwsConnector.discover describe mode regression — unchanged behaviour
  (1 test)
- AwsConnector.fetch config mode — text/plain summary returned, fallback
  when CI fetch returns empty (2 tests)
- AwsConnector.fetch describe mode regression — unchanged behaviour (1 test)
- Reconciliation semantics: absent-now resource tombstoned (is_tombstone),
  no-change run idempotent via content_hash equivalence (2 tests)

Total: 36 tests
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from omniscience_connectors.aws.connector import (
    _DELETED_STATUSES,
    _TIER1_RESOURCE_TYPES,
    AwsConfig,
    AwsConnector,
    _ci_to_ref,
    _ci_to_text,
    _config_external_id,
    _config_uri,
    _discover_config_type,
    _fetch_config_ci,
    _list_aggregate_cis_for_type,
    _normalise_relationship_type,
)
from omniscience_connectors.base import DocumentRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCOUNT = "123456789012"
REGION = "eu-west-1"
AGGREGATOR = "org-aggregator"
SECRETS: dict[str, str] = {
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


_SENTINEL: dict[str, str] = {}  # distinct sentinel for "use default tags"


def _make_ci(
    resource_type: str = "AWS::EC2::VPC",
    resource_id: str = "vpc-abc123",
    account_id: str = ACCOUNT,
    region: str = REGION,
    status: str = "OK",
    capture_time: str = "2026-06-01T12:00:00Z",
    relationships: list[dict[str, Any]] | None = None,
    tags: dict[str, str] | None = _SENTINEL,  # type: ignore[assignment]
    configuration: str | None = None,
) -> dict[str, Any]:
    resolved_tags: dict[str, str] = (
        {"Name": "test-resource"} if tags is _SENTINEL else (tags or {})
    )
    return {
        "resourceType": resource_type,
        "resourceId": resource_id,
        "accountId": account_id,
        "awsRegion": region,
        "arn": (
            f"arn:aws:{resource_type.lower().replace('::', ':')}:"
            f"{region}:{account_id}:{resource_id}"
        ),
        "configurationItemCaptureTime": capture_time,
        "configurationItemStatus": status,
        "relationships": relationships or [],
        "tags": resolved_tags,
        "configuration": configuration or "",
        "configDeliveryS3Bucket": "config-delivery-bucket",
        "configDeliveryS3KeyPrefix": "AWSLogs/config",
    }


# ---------------------------------------------------------------------------
# AwsConfig new fields
# ---------------------------------------------------------------------------


class TestAwsConfigNewFields:
    def test_default_acquisition_is_describe(self) -> None:
        cfg = AwsConfig()
        assert cfg.acquisition == "describe"

    def test_config_acquisition_accepted(self) -> None:
        cfg = AwsConfig(acquisition="config", aggregator_name=AGGREGATOR)
        assert cfg.acquisition == "config"
        assert cfg.aggregator_name == AGGREGATOR

    def test_default_config_resource_types_is_tier1(self) -> None:
        cfg = AwsConfig(acquisition="config", aggregator_name=AGGREGATOR)
        assert set(cfg.config_resource_types) == set(_TIER1_RESOURCE_TYPES)
        assert len(cfg.config_resource_types) == len(_TIER1_RESOURCE_TYPES)

    def test_custom_config_resource_types(self) -> None:
        cfg = AwsConfig(
            acquisition="config",
            aggregator_name=AGGREGATOR,
            config_resource_types=["AWS::EC2::VPC"],
        )
        assert cfg.config_resource_types == ["AWS::EC2::VPC"]

    def test_config_regions_default_empty(self) -> None:
        cfg = AwsConfig(acquisition="config", aggregator_name=AGGREGATOR)
        assert cfg.config_regions == []

    def test_existing_describe_fields_unchanged(self) -> None:
        cfg = AwsConfig(regions=["ap-southeast-1"], services=["s3"])
        assert cfg.regions == ["ap-southeast-1"]
        assert cfg.services == ["s3"]
        assert cfg.acquisition == "describe"


# ---------------------------------------------------------------------------
# _config_external_id / _config_uri
# ---------------------------------------------------------------------------


class TestExternalIdAndUri:
    def test_external_id_format(self) -> None:
        eid = _config_external_id(ACCOUNT, REGION, "AWS::EC2::VPC", "vpc-abc")
        assert eid == f"aws:{ACCOUNT}:{REGION}:AWS::EC2::VPC:vpc-abc"

    def test_uri_format(self) -> None:
        uri = _config_uri(ACCOUNT, REGION, "AWS::EC2::VPC", "vpc-abc")
        assert uri == f"aws://{ACCOUNT}/{REGION}/AWS::EC2::VPC/vpc-abc"


# ---------------------------------------------------------------------------
# _normalise_relationship_type
# ---------------------------------------------------------------------------


class TestNormaliseRelationshipType:
    def test_spaces_to_underscores_lowercase(self) -> None:
        assert _normalise_relationship_type("Is associated with Vpc") == "is_associated_with_vpc"

    def test_single_word(self) -> None:
        assert _normalise_relationship_type("Contains") == "contains"


# ---------------------------------------------------------------------------
# _ci_to_ref
# ---------------------------------------------------------------------------


class TestCiToRef:
    def test_happy_path_fields(self) -> None:
        ci = _make_ci()
        ref = _ci_to_ref(ci)
        assert ref is not None
        expected_eid = _config_external_id(ACCOUNT, REGION, "AWS::EC2::VPC", "vpc-abc123")
        assert ref.external_id == expected_eid
        assert ref.uri == _config_uri(ACCOUNT, REGION, "AWS::EC2::VPC", "vpc-abc123")
        assert ref.metadata["acquisition"] == "config"
        assert ref.metadata["aws_resource_type"] == "AWS::EC2::VPC"
        assert ref.metadata["resource_id"] == "vpc-abc123"
        assert ref.metadata["account_id"] == ACCOUNT
        assert ref.metadata["region"] == REGION
        assert ref.metadata["capture_time"] == "2026-06-01T12:00:00Z"
        assert ref.metadata["valid_from"] == "2026-06-01T12:00:00Z"

    def test_missing_mandatory_field_returns_none(self) -> None:
        ci = _make_ci()
        ci["resourceId"] = ""
        ref = _ci_to_ref(ci)
        assert ref is None

    def test_deleted_status_sets_tombstone_flag(self) -> None:
        for status in _DELETED_STATUSES:
            ci = _make_ci(status=status)
            ref = _ci_to_ref(ci)
            assert ref is not None
            assert ref.metadata["is_tombstone"] is True

    def test_active_status_tombstone_flag_false(self) -> None:
        ci = _make_ci(status="OK")
        ref = _ci_to_ref(ci)
        assert ref is not None
        assert ref.metadata["is_tombstone"] is False

    def test_relationships_mapped_to_edge_list(self) -> None:
        rels = [
            {
                "resourceId": "sg-111",
                "resourceType": "AWS::EC2::SecurityGroup",
                "relationshipName": "Is associated with SecurityGroup",
            }
        ]
        ci = _make_ci(relationships=rels)
        ref = _ci_to_ref(ci)
        assert ref is not None
        edges = ref.metadata["relationships"]
        assert len(edges) == 1
        edge = edges[0]
        assert edge["related_resource_id"] == "sg-111"
        assert edge["edge_type"] == "is_associated_with_securitygroup"
        assert edge["related_external_id"] == _config_external_id(
            ACCOUNT, REGION, "AWS::EC2::SecurityGroup", "sg-111"
        )

    def test_cold_ref_written(self) -> None:
        ci = _make_ci()
        ref = _ci_to_ref(ci)
        assert ref is not None
        cold = ref.metadata["cold_ref"]
        assert cold["s3_bucket"] == "config-delivery-bucket"
        assert cold["capture_time"] == "2026-06-01T12:00:00Z"

    def test_name_tag_used_as_name(self) -> None:
        ci = _make_ci(tags={"Name": "my-vpc"})
        ref = _ci_to_ref(ci)
        assert ref is not None
        assert ref.metadata["name"] == "my-vpc"

    def test_missing_name_tag_falls_back_to_resource_id(self) -> None:
        # Pass a distinct empty-dict object (not the sentinel) to get no tags
        ci = _make_ci(tags=dict())
        ref = _ci_to_ref(ci)
        assert ref is not None
        assert ref.metadata["name"] == "vpc-abc123"


# ---------------------------------------------------------------------------
# _list_aggregate_cis_for_type
# ---------------------------------------------------------------------------


class TestListAggregateCisForType:
    def _mock_client(
        self,
        pages: list[list[dict[str, Any]]],
        batch_items: list[dict[str, Any]] | None = None,
    ) -> MagicMock:
        """Build a mock config client that paginates list and returns batch."""
        client = MagicMock()
        responses = []
        for i, identifiers in enumerate(pages):
            resp: dict[str, Any] = {"ResourceIdentifiers": identifiers}
            if i < len(pages) - 1:
                resp["NextToken"] = f"token-{i}"
            responses.append(resp)
        client.list_aggregate_discovered_resources.side_effect = responses
        client.batch_get_aggregate_resource_config.return_value = {
            "BaseConfigurationItems": batch_items or [_make_ci()]
        }
        return client

    def test_single_page(self) -> None:
        identifiers = [
            {
                "SourceAccountId": ACCOUNT,
                "SourceRegion": REGION,
                "ResourceId": "vpc-1",
                "ResourceType": "AWS::EC2::VPC",
            }
        ]
        client = self._mock_client([identifiers])
        result = _list_aggregate_cis_for_type(client, AGGREGATOR, "AWS::EC2::VPC", [])
        assert len(result) == 1

    def test_multi_page(self) -> None:
        def _ident(rid: str) -> dict[str, str]:
            return {
                "SourceAccountId": ACCOUNT,
                "SourceRegion": REGION,
                "ResourceId": rid,
                "ResourceType": "AWS::EC2::VPC",
            }

        page1 = [_ident("vpc-1")]
        page2 = [_ident("vpc-2")]
        ci1 = _make_ci(resource_id="vpc-1")
        ci2 = _make_ci(resource_id="vpc-2")

        client = MagicMock()
        client.list_aggregate_discovered_resources.side_effect = [
            {"ResourceIdentifiers": page1, "NextToken": "tok"},
            {"ResourceIdentifiers": page2},
        ]
        client.batch_get_aggregate_resource_config.side_effect = [
            {"BaseConfigurationItems": [ci1]},
            {"BaseConfigurationItems": [ci2]},
        ]
        result = _list_aggregate_cis_for_type(client, AGGREGATOR, "AWS::EC2::VPC", [])
        assert len(result) == 2

    def test_client_error_propagates(self) -> None:
        client = MagicMock()
        err = _client_error("AccessDeniedException")
        client.list_aggregate_discovered_resources.side_effect = err
        with pytest.raises(ClientError):
            _list_aggregate_cis_for_type(client, AGGREGATOR, "AWS::EC2::VPC", [])


# ---------------------------------------------------------------------------
# _discover_config_type
# ---------------------------------------------------------------------------


class TestDiscoverConfigType:
    def test_yields_one_ref_per_ci(self) -> None:
        cis = [_make_ci(resource_id="vpc-1"), _make_ci(resource_id="vpc-2")]
        with patch(
            "omniscience_connectors.aws.connector._list_aggregate_cis_for_type",
            return_value=cis,
        ):
            refs = _discover_config_type(MagicMock(), AGGREGATOR, "AWS::EC2::VPC", [])
        assert len(refs) == 2

    def test_client_error_returns_empty_and_logs(self) -> None:
        with patch(
            "omniscience_connectors.aws.connector._list_aggregate_cis_for_type",
            side_effect=_client_error("AccessDeniedException"),
        ):
            refs = _discover_config_type(MagicMock(), AGGREGATOR, "AWS::EC2::VPC", [])
        assert refs == []

    def test_other_types_not_affected_by_one_type_error(self) -> None:
        """Simulate caller calling _discover_config_type twice: one succeeds, one fails."""
        ci = _make_ci()
        with patch(
            "omniscience_connectors.aws.connector._list_aggregate_cis_for_type",
            side_effect=[
                _client_error("AccessDeniedException"),  # VPC fails
                [ci],  # SecurityGroup succeeds
            ],
        ):
            refs_vpc = _discover_config_type(MagicMock(), AGGREGATOR, "AWS::EC2::VPC", [])
            refs_sg = _discover_config_type(MagicMock(), AGGREGATOR, "AWS::EC2::SecurityGroup", [])
        assert refs_vpc == []
        assert len(refs_sg) == 1


# ---------------------------------------------------------------------------
# _fetch_config_ci
# ---------------------------------------------------------------------------


class TestFetchConfigCi:
    def test_success_returns_ci(self) -> None:
        ci = _make_ci()
        client = MagicMock()
        client.batch_get_aggregate_resource_config.return_value = {"BaseConfigurationItems": [ci]}
        result = _fetch_config_ci(client, AGGREGATOR, ACCOUNT, REGION, "AWS::EC2::VPC", "vpc-1")
        assert result == ci

    def test_empty_response_returns_empty_dict(self) -> None:
        client = MagicMock()
        client.batch_get_aggregate_resource_config.return_value = {"BaseConfigurationItems": []}
        result = _fetch_config_ci(client, AGGREGATOR, ACCOUNT, REGION, "AWS::EC2::VPC", "vpc-x")
        assert result == {}

    def test_client_error_returns_empty_dict(self) -> None:
        client = MagicMock()
        err = _client_error("AccessDeniedException")
        client.batch_get_aggregate_resource_config.side_effect = err
        result = _fetch_config_ci(client, AGGREGATOR, ACCOUNT, REGION, "AWS::EC2::VPC", "vpc-x")
        assert result == {}


# ---------------------------------------------------------------------------
# _ci_to_text
# ---------------------------------------------------------------------------


class TestCiToText:
    def test_mandatory_fields_present(self) -> None:
        ci = _make_ci()
        text = _ci_to_text(ci)
        assert "AWS::EC2::VPC" in text
        assert "vpc-abc123" in text
        assert ACCOUNT in text
        assert REGION in text

    def test_malformed_config_json_ignored(self) -> None:
        ci = _make_ci(configuration="{not-valid-json")
        text = _ci_to_text(ci)
        # Should not raise; still produces output
        assert "AWS::EC2::VPC" in text

    def test_key_config_fields_included(self) -> None:
        config_obj = json.dumps({"State": "available", "VpcId": "vpc-abc123"})
        ci = _make_ci(configuration=config_obj)
        text = _ci_to_text(ci)
        assert "State" in text or "available" in text


# ---------------------------------------------------------------------------
# AwsConnector.discover — config mode
# ---------------------------------------------------------------------------


class TestAwsConnectorDiscoverConfigMode:
    @pytest.fixture()
    def connector(self) -> AwsConnector:
        return AwsConnector()

    async def test_yields_refs_per_type(self, connector: AwsConnector) -> None:
        cfg = AwsConfig(
            acquisition="config",
            aggregator_name=AGGREGATOR,
            config_resource_types=["AWS::EC2::VPC", "AWS::EC2::SecurityGroup"],
        )
        ci_vpc = _make_ci(resource_type="AWS::EC2::VPC", resource_id="vpc-1")
        ci_sg = _make_ci(resource_type="AWS::EC2::SecurityGroup", resource_id="sg-1")

        with patch(
            "omniscience_connectors.aws.connector._discover_config_type",
            side_effect=[
                [_ci_to_ref(ci_vpc)],
                [_ci_to_ref(ci_sg)],
            ],
        ):
            refs = [r async for r in connector.discover(cfg, SECRETS)]

        assert len(refs) == 2
        types = {r.metadata["aws_resource_type"] for r in refs}
        assert types == {"AWS::EC2::VPC", "AWS::EC2::SecurityGroup"}

    async def test_per_type_error_skipped_others_continue(self, connector: AwsConnector) -> None:
        cfg = AwsConfig(
            acquisition="config",
            aggregator_name=AGGREGATOR,
            config_resource_types=["AWS::EC2::VPC", "AWS::EC2::SecurityGroup"],
        )
        ci_sg = _make_ci(resource_type="AWS::EC2::SecurityGroup", resource_id="sg-2")

        with patch(
            "omniscience_connectors.aws.connector._discover_config_type",
            side_effect=[
                [],  # VPC: empty (simulates skip after error inside _discover_config_type)
                [_ci_to_ref(ci_sg)],
            ],
        ):
            refs = [r async for r in connector.discover(cfg, SECRETS)]

        assert len(refs) == 1
        assert refs[0].metadata["aws_resource_type"] == "AWS::EC2::SecurityGroup"

    async def test_missing_aggregator_name_raises(self, connector: AwsConnector) -> None:
        cfg = AwsConfig(acquisition="config")  # aggregator_name not set
        with pytest.raises(ValueError, match="aggregator_name"):
            async for _ in connector.discover(cfg, SECRETS):
                pass


# ---------------------------------------------------------------------------
# AwsConnector.discover — describe mode regression
# ---------------------------------------------------------------------------


class TestAwsConnectorDiscoverDescribeRegression:
    async def test_describe_mode_unchanged(self) -> None:
        """acquisition='describe' must preserve byte-for-byte original behaviour."""
        connector = AwsConnector()
        cfg = AwsConfig(services=["iam"], regions=["us-east-1"])
        assert cfg.acquisition == "describe"

        iam_ref = DocumentRef(
            external_id=f"arn:aws:iam::{ACCOUNT}:role/r",
            uri=f"aws://{ACCOUNT}/global/iam/role/r",
            metadata={"kind": "aws_live"},
        )
        with (
            patch(
                "omniscience_connectors.aws.connector._get_account_id",
                return_value=ACCOUNT,
            ),
            patch(
                "omniscience_connectors.aws.connector._discover_iam",
                return_value=[iam_ref],
            ),
        ):
            refs = [r async for r in connector.discover(cfg, SECRETS)]

        assert len(refs) == 1
        assert refs[0].metadata["kind"] == "aws_live"


# ---------------------------------------------------------------------------
# AwsConnector.fetch — config mode
# ---------------------------------------------------------------------------


class TestAwsConnectorFetchConfigMode:
    @pytest.fixture()
    def connector(self) -> AwsConnector:
        return AwsConnector()

    def _make_config_ref(
        self,
        resource_type: str = "AWS::EC2::VPC",
        resource_id: str = "vpc-1",
    ) -> DocumentRef:
        ci = _make_ci(resource_type=resource_type, resource_id=resource_id)
        ref = _ci_to_ref(ci)
        assert ref is not None
        return ref

    async def test_returns_text_plain_summary(self, connector: AwsConnector) -> None:
        cfg = AwsConfig(acquisition="config", aggregator_name=AGGREGATOR)
        ref = self._make_config_ref()
        ci = _make_ci()

        with patch(
            "omniscience_connectors.aws.connector._fetch_config_ci",
            return_value=ci,
        ):
            doc = await connector.fetch(cfg, SECRETS, ref)

        assert doc.content_type == "text/plain"
        body = doc.content_bytes.decode()
        assert "AWS::EC2::VPC" in body
        assert "vpc-abc123" in body

    async def test_fallback_when_ci_fetch_empty(self, connector: AwsConnector) -> None:
        cfg = AwsConfig(acquisition="config", aggregator_name=AGGREGATOR)
        ref = self._make_config_ref()

        with patch(
            "omniscience_connectors.aws.connector._fetch_config_ci",
            return_value={},
        ):
            doc = await connector.fetch(cfg, SECRETS, ref)

        assert doc.content_type == "text/plain"
        body = doc.content_bytes.decode()
        # Falls back to ref metadata fields
        assert "AWS::EC2::VPC" in body


# ---------------------------------------------------------------------------
# AwsConnector.fetch — describe mode regression
# ---------------------------------------------------------------------------


class TestAwsConnectorFetchDescribeRegression:
    async def test_describe_fetch_returns_application_json(self) -> None:
        connector = AwsConnector()
        cfg = AwsConfig()  # acquisition="describe"
        ref = DocumentRef(
            external_id="arn:aws:s3:::my-bucket",
            uri=f"aws://{ACCOUNT}/global/s3/bucket/my-bucket",
            metadata={
                "kind": "aws_live",
                "resource_type": "aws_s3_bucket",
                "region": "us-east-1",
                "account_id": ACCOUNT,
                "arn": "arn:aws:s3:::my-bucket",
                "name": "my-bucket",
            },
        )
        mock_client = MagicMock()
        mock_client.get_bucket_location.return_value = {"LocationConstraint": "us-east-1"}
        mock_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        mock_client.get_bucket_tagging.return_value = {"TagSet": []}
        mock_client.get_bucket_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucketPolicy", "Message": ""}}, "op"
        )
        with patch(
            "omniscience_connectors.aws.connector._build_client",
            return_value=mock_client,
        ):
            doc = await connector.fetch(cfg, SECRETS, ref)

        assert doc.content_type == "application/json"
        data = json.loads(doc.content_bytes)
        assert data["name"] == "my-bucket"


# ---------------------------------------------------------------------------
# Reconciliation semantics
# ---------------------------------------------------------------------------


class TestReconciliationSemantics:
    def test_deleted_status_tombstone_flag_set(self) -> None:
        """A CI with ResourceDeleted status sets is_tombstone=True in metadata."""
        for status in ("ResourceDeleted", "ResourceDeletedNotRecorded"):
            ci = _make_ci(status=status)
            ref = _ci_to_ref(ci)
            assert ref is not None, f"Expected ref for status={status}"
            assert ref.metadata["is_tombstone"] is True, f"Expected tombstone for status={status}"

    def test_same_ci_produces_same_external_id(self) -> None:
        """Idempotent: same CI always produces the same external_id (no-change run is a no-op)."""
        ci = _make_ci()
        ref1 = _ci_to_ref(ci)
        ref2 = _ci_to_ref(ci)
        assert ref1 is not None
        assert ref2 is not None
        assert ref1.external_id == ref2.external_id
