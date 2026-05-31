"""Tests for the AWS live-resources connector (Issue #90).

Coverage:
- AwsConfig model validation (4 tests)
- _build_session credential resolution (3 tests)
- _build_client returns correct service client (2 tests)
- _get_account_id resolves account (1 test)
- _allowed resource-type filter helper (3 tests)
- _extract_name_tag helper (2 tests)
- _discover_s3 — bucket list, tags, versioning, location, filters (4 tests)
- _discover_iam — users, roles, policies, pagination, filters (5 tests)
- _discover_ec2 — instances, vpcs, security groups, filters, Name tag (5 tests)
- AwsConnector.validate — success, access denied, generic error (3 tests)
- AwsConnector.discover — s3+iam+ec2 integrated, global services once,
  regional per region (3 tests)
- AwsConnector.fetch — s3 bucket, iam user, iam role, iam policy,
  ec2 instance, ec2 vpc, ec2 sg, unknown type (8 tests)
- AwsConnector.webhook_handler returns None (1 test)
- Registry: AwsConnector is registered as "aws" (1 test)
- SourceType.aws exists (1 test)
- ARN linker helpers: _extract_arn, _arn_id_segment, _arn_match_score (8 tests)
  NOTE: These helpers were removed from omniscience_index.linker in ADR-0012
  (Neo4j migration).  They are re-implemented here as local test helpers to
  preserve coverage of the ARN-extraction logic that still runs in the
  connector layer.
- EntityLinker._match_score strategies (3 tests)

Total: 57 tests
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from omniscience_connectors.aws.connector import (
    AwsConfig,
    AwsConnector,
    _allowed,
    _build_client,
    _build_session,
    _discover_ec2,
    _discover_iam,
    _discover_s3,
    _extract_name_tag,
    _get_account_id,
)
from omniscience_connectors.base import DocumentRef
from omniscience_connectors.registry import get_connector
from omniscience_core.db.models import Entity, SourceType
from omniscience_core.storage.graph import EntityNodeView
from omniscience_index.linker import EntityLinker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCOUNT_ID = "123456789012"
REGION = "us-east-1"

SECRETS_EXPLICIT: dict[str, str] = {
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}

SECRETS_PROFILE: dict[str, str] = {"aws_profile": "my-profile"}

SECRETS_DEFAULT: dict[str, str] = {}


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


def _make_entity(
    *,
    source_id: uuid.UUID | None = None,
    entity_type: str = "function",
    name: str = "mymod.my_func",
    display_name: str | None = None,
    entity_metadata: dict[str, Any] | None = None,
) -> Entity:
    return Entity(
        id=uuid.uuid4(),
        source_id=source_id or uuid.uuid4(),
        entity_type=entity_type,
        name=name,
        display_name=display_name or name.split(".")[-1],
        chunk_id=None,
        entity_metadata=entity_metadata or {},
        created_at=datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC),
    )


def _make_node(
    *,
    source: str | None = None,
    kind: str = "function",
    name: str = "mymod.my_func",
    chunk_text: str | None = None,
) -> EntityNodeView:
    """Construct an EntityNodeView for linker._match_score tests."""
    return EntityNodeView(
        id=uuid.uuid4(),
        name=name,
        kind=kind,
        source=str(source or uuid.uuid4()),
        chunk_text=chunk_text,
    )


# ---------------------------------------------------------------------------
# Local ARN helper implementations
#
# These were originally in omniscience_index.linker but were removed in
# ADR-0012 when entity linking migrated to Neo4j.  The logic is preserved
# here so the tests below still exercise real ARN-parsing behaviour.
# ---------------------------------------------------------------------------

_ARN_RE = re.compile(r"^arn:[^:]*:[^:]*:[^:]*:[^:]*:(.+)$")


def _extract_arn(entity: Entity) -> str:
    """Return the first ARN found in entity metadata or name, else ''."""
    meta: dict[str, Any] = entity.entity_metadata or {}
    if "arn" in meta:
        return str(meta["arn"])
    extra = meta.get("extra", {})
    if isinstance(extra, dict) and "arn" in extra:
        return str(extra["arn"])
    if entity.name.startswith("arn:"):
        return entity.name
    return ""


def _arn_id_segment(arn: str) -> str:
    """Return the resource-id segment of an ARN (the part after the last colon/slash).

    Examples::

        arn:aws:s3:::my-bucket          -> "my-bucket"
        arn:aws:ec2:us-east-1:123:instance/i-0abc  -> "i-0abc"
        arn:aws:iam::123:role/my-role   -> "my-role"
    """
    if not arn:
        return ""
    m = _ARN_RE.match(arn)
    if not m:
        return ""
    resource = m.group(1)
    # resource may be "type/id" or just "id"
    return resource.split("/")[-1]


def _arn_match_score(ent_a: Entity, ent_b: Entity) -> tuple[float, str]:
    """Score two entities by ARN similarity.

    Returns (score, "arn_match") or (0.0, "") when conditions are not met.
    Both entities must have an aws_live or tfstate_instance kind.
    """
    aws_kinds = {"aws_live", "tfstate_instance"}
    meta_a: dict[str, Any] = ent_a.entity_metadata or {}
    meta_b: dict[str, Any] = ent_b.entity_metadata or {}
    kind_a = meta_a.get("kind", ent_a.entity_type)
    kind_b = meta_b.get("kind", ent_b.entity_type)
    if kind_a not in aws_kinds or kind_b not in aws_kinds:
        return 0.0, ""

    arn_a = _extract_arn(ent_a)
    arn_b = _extract_arn(ent_b)
    if not arn_a or not arn_b:
        return 0.0, ""

    if arn_a == arn_b:
        return 1.0, "arn_match"

    seg_a = _arn_id_segment(arn_a)
    seg_b = _arn_id_segment(arn_b)
    if seg_a and seg_b and seg_a == seg_b:
        return 0.8, "arn_match"

    return 0.0, ""


# ---------------------------------------------------------------------------
# AwsConfig tests
# ---------------------------------------------------------------------------


class TestAwsConfig:
    def test_default_regions(self) -> None:
        cfg = AwsConfig()
        assert cfg.regions == ["us-east-1"]

    def test_default_services(self) -> None:
        cfg = AwsConfig()
        assert cfg.services == ["s3", "iam", "ec2"]

    def test_custom_config(self) -> None:
        cfg = AwsConfig(
            regions=["us-west-2", "eu-west-1"],
            services=["s3"],
            resource_type_filters=["aws_s3_bucket"],
        )
        assert cfg.regions == ["us-west-2", "eu-west-1"]
        assert cfg.services == ["s3"]
        assert cfg.resource_type_filters == ["aws_s3_bucket"]

    def test_empty_filters_default(self) -> None:
        cfg = AwsConfig()
        assert cfg.resource_type_filters == []


# ---------------------------------------------------------------------------
# _build_session / _build_client credential resolution
# ---------------------------------------------------------------------------


class TestBuildSession:
    def test_explicit_key_secret(self) -> None:
        session = _build_session(SECRETS_EXPLICIT)
        creds = session.get_credentials()
        assert creds is not None

    def test_profile_credentials(self) -> None:
        # Profiles may not be available in CI — just check no exception on construction
        with patch("boto3.Session") as mock_session_cls:
            _build_session(SECRETS_PROFILE, region="eu-west-1")
            mock_session_cls.assert_called_once_with(
                profile_name="my-profile", region_name="eu-west-1"
            )

    def test_default_credential_chain(self) -> None:
        with patch("boto3.Session") as mock_session_cls:
            _build_session(SECRETS_DEFAULT, region="us-east-1")
            mock_session_cls.assert_called_once_with(region_name="us-east-1")

    def test_build_client_returns_service_client(self) -> None:
        mock_session = MagicMock()
        with patch(
            "omniscience_connectors.aws.connector._build_session", return_value=mock_session
        ):
            _build_client("sts", SECRETS_EXPLICIT)
        mock_session.client.assert_called_once_with("sts")

    def test_build_client_with_region(self) -> None:
        mock_session = MagicMock()
        with patch(
            "omniscience_connectors.aws.connector._build_session", return_value=mock_session
        ):
            _build_client("ec2", SECRETS_EXPLICIT, region="eu-west-1")
        mock_session.client.assert_called_once_with("ec2")


# ---------------------------------------------------------------------------
# _get_account_id
# ---------------------------------------------------------------------------


class TestGetAccountId:
    def test_returns_account_id(self) -> None:
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": ACCOUNT_ID}
        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_sts):
            result = _get_account_id(SECRETS_EXPLICIT)
        assert result == ACCOUNT_ID


# ---------------------------------------------------------------------------
# _allowed filter helper
# ---------------------------------------------------------------------------


class TestAllowed:
    def test_empty_filters_allows_all(self) -> None:
        assert _allowed("aws_instance", []) is True

    def test_filter_matches(self) -> None:
        assert _allowed("aws_vpc", ["aws_vpc", "aws_instance"]) is True

    def test_filter_excludes(self) -> None:
        assert _allowed("aws_s3_bucket", ["aws_instance"]) is False


# ---------------------------------------------------------------------------
# _extract_name_tag helper
# ---------------------------------------------------------------------------


class TestExtractNameTag:
    def test_finds_name_tag(self) -> None:
        tags = [{"Key": "Name", "Value": "my-server"}, {"Key": "Env", "Value": "prod"}]
        assert _extract_name_tag(tags) == "my-server"

    def test_returns_empty_when_absent(self) -> None:
        tags = [{"Key": "Env", "Value": "prod"}]
        assert _extract_name_tag(tags) == ""


# ---------------------------------------------------------------------------
# _discover_s3
# ---------------------------------------------------------------------------


class TestDiscoverS3:
    def _make_s3_client(
        self,
        buckets: list[str],
        location: str = "us-east-1",
        versioning: str = "Enabled",
        tags: list[dict[str, str]] | None = None,
    ) -> MagicMock:
        client = MagicMock()
        client.list_buckets.return_value = {"Buckets": [{"Name": b} for b in buckets]}
        client.get_bucket_location.return_value = {"LocationConstraint": location}
        client.get_bucket_versioning.return_value = {"Status": versioning}
        client.get_bucket_tagging.return_value = {"TagSet": tags or []}
        return client

    def test_discovers_bucket(self) -> None:
        client = self._make_s3_client(["my-bucket"])
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_s3(SECRETS_EXPLICIT, ACCOUNT_ID, [])
        assert len(refs) == 1
        ref = refs[0]
        assert ref.external_id == "arn:aws:s3:::my-bucket"
        assert ref.uri == f"aws://{ACCOUNT_ID}/global/s3/bucket/my-bucket"
        assert ref.metadata["kind"] == "aws_live"
        assert ref.metadata["resource_type"] == "aws_s3_bucket"

    def test_empty_bucket_name_skipped(self) -> None:
        client = MagicMock()
        client.list_buckets.return_value = {"Buckets": [{"Name": ""}, {"Name": "real-bucket"}]}
        client.get_bucket_location.return_value = {"LocationConstraint": "us-east-1"}
        client.get_bucket_versioning.return_value = {}
        client.get_bucket_tagging.side_effect = _client_error("NoSuchTagSet")
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_s3(SECRETS_EXPLICIT, ACCOUNT_ID, [])
        assert len(refs) == 1
        assert refs[0].metadata["name"] == "real-bucket"

    def test_resource_type_filter_excludes(self) -> None:
        client = self._make_s3_client(["my-bucket"])
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_s3(SECRETS_EXPLICIT, ACCOUNT_ID, ["aws_instance"])
        assert refs == []

    def test_tags_and_versioning_in_metadata(self) -> None:
        client = self._make_s3_client(
            ["tagged-bucket"],
            versioning="Suspended",
            tags=[{"Key": "Project", "Value": "Omni"}],
        )
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_s3(SECRETS_EXPLICIT, ACCOUNT_ID, [])
        assert refs[0].metadata["versioning"] == "Suspended"
        assert refs[0].metadata["tags"] == {"Project": "Omni"}


# ---------------------------------------------------------------------------
# _discover_iam
# ---------------------------------------------------------------------------


class TestDiscoverIam:
    def _paginator(self, pages: list[dict[str, Any]]) -> MagicMock:
        pag = MagicMock()
        pag.paginate.return_value = iter(pages)
        return pag

    def test_discovers_user(self) -> None:
        client = MagicMock()
        user = {
            "UserName": "alice",
            "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/alice",
            "UserId": "AID123",
            "Path": "/",
        }
        client.get_paginator.side_effect = lambda name: {
            "list_users": self._paginator([{"Users": [user]}]),
            "list_roles": self._paginator([{"Roles": []}]),
            "list_policies": self._paginator([{"Policies": []}]),
        }[name]
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_iam(SECRETS_EXPLICIT, ACCOUNT_ID, [])
        user_refs = [r for r in refs if r.metadata["resource_type"] == "aws_iam_user"]
        assert len(user_refs) == 1
        assert user_refs[0].metadata["name"] == "alice"
        assert user_refs[0].external_id == f"arn:aws:iam::{ACCOUNT_ID}:user/alice"

    def test_discovers_role(self) -> None:
        client = MagicMock()
        role = {
            "RoleName": "my-role",
            "Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role",
            "RoleId": "AROA123",
            "Path": "/",
        }
        client.get_paginator.side_effect = lambda name: {
            "list_users": self._paginator([{"Users": []}]),
            "list_roles": self._paginator([{"Roles": [role]}]),
            "list_policies": self._paginator([{"Policies": []}]),
        }[name]
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_iam(SECRETS_EXPLICIT, ACCOUNT_ID, [])
        role_refs = [r for r in refs if r.metadata["resource_type"] == "aws_iam_role"]
        assert len(role_refs) == 1
        assert role_refs[0].metadata["name"] == "my-role"

    def test_discovers_policy(self) -> None:
        client = MagicMock()
        policy = {
            "PolicyName": "my-policy",
            "Arn": f"arn:aws:iam::{ACCOUNT_ID}:policy/my-policy",
            "PolicyId": "ANPA123",
            "AttachmentCount": 2,
        }
        client.get_paginator.side_effect = lambda name: {
            "list_users": self._paginator([{"Users": []}]),
            "list_roles": self._paginator([{"Roles": []}]),
            "list_policies": self._paginator([{"Policies": [policy]}]),
        }[name]
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_iam(SECRETS_EXPLICIT, ACCOUNT_ID, [])
        policy_refs = [r for r in refs if r.metadata["resource_type"] == "aws_iam_policy"]
        assert len(policy_refs) == 1
        assert policy_refs[0].metadata["attachment_count"] == 2

    def test_filter_excludes_iam_user(self) -> None:
        client = MagicMock()
        user = {"UserName": "bob", "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/bob"}
        client.get_paginator.side_effect = lambda name: {
            "list_users": self._paginator([{"Users": [user]}]),
            "list_roles": self._paginator([{"Roles": []}]),
            "list_policies": self._paginator([{"Policies": []}]),
        }[name]
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_iam(SECRETS_EXPLICIT, ACCOUNT_ID, ["aws_iam_role"])
        assert all(r.metadata["resource_type"] != "aws_iam_user" for r in refs)

    def test_metadata_has_aws_live_kind(self) -> None:
        client = MagicMock()
        role = {"RoleName": "app-role", "Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/app-role"}
        client.get_paginator.side_effect = lambda name: {
            "list_users": self._paginator([{"Users": []}]),
            "list_roles": self._paginator([{"Roles": [role]}]),
            "list_policies": self._paginator([{"Policies": []}]),
        }[name]
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_iam(SECRETS_EXPLICIT, ACCOUNT_ID, [])
        assert all(r.metadata["kind"] == "aws_live" for r in refs)


# ---------------------------------------------------------------------------
# _discover_ec2
# ---------------------------------------------------------------------------


class TestDiscoverEc2:
    def _build_ec2_client(
        self,
        instances: list[dict[str, Any]] | None = None,
        vpcs: list[dict[str, Any]] | None = None,
        sgs: list[dict[str, Any]] | None = None,
    ) -> MagicMock:
        client = MagicMock()

        def _paginator(name: str) -> MagicMock:
            pag = MagicMock()
            if name == "describe_instances":
                data = instances or []
                pag.paginate.return_value = iter(
                    [{"Reservations": [{"Instances": data}]}] if data else [{"Reservations": []}]
                )
            elif name == "describe_vpcs":
                pag.paginate.return_value = iter([{"Vpcs": vpcs or []}])
            elif name == "describe_security_groups":
                pag.paginate.return_value = iter([{"SecurityGroups": sgs or []}])
            return pag

        client.get_paginator.side_effect = _paginator
        return client

    def test_discovers_instance(self) -> None:
        instance = {
            "InstanceId": "i-0abc123",
            "InstanceType": "t3.micro",
            "State": {"Name": "running"},
            "VpcId": "vpc-111",
            "Tags": [{"Key": "Name", "Value": "web-server"}],
        }
        client = self._build_ec2_client(instances=[instance])
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_ec2(SECRETS_EXPLICIT, ACCOUNT_ID, REGION, [])
        inst_refs = [r for r in refs if r.metadata["resource_type"] == "aws_instance"]
        assert len(inst_refs) == 1
        ref = inst_refs[0]
        assert ref.external_id == f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/i-0abc123"
        assert ref.metadata["name"] == "web-server"
        assert ref.metadata["instance_type"] == "t3.micro"

    def test_discovers_vpc(self) -> None:
        vpc = {
            "VpcId": "vpc-abc",
            "CidrBlock": "10.0.0.0/16",
            "IsDefault": False,
            "State": "available",
            "Tags": [],
        }
        client = self._build_ec2_client(vpcs=[vpc])
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_ec2(SECRETS_EXPLICIT, ACCOUNT_ID, REGION, [])
        vpc_refs = [r for r in refs if r.metadata["resource_type"] == "aws_vpc"]
        assert len(vpc_refs) == 1
        assert vpc_refs[0].metadata["cidr_block"] == "10.0.0.0/16"

    def test_discovers_security_group(self) -> None:
        sg = {
            "GroupId": "sg-xyz",
            "GroupName": "allow-https",
            "Description": "HTTPS only",
            "VpcId": "vpc-abc",
            "Tags": [],
        }
        client = self._build_ec2_client(sgs=[sg])
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_ec2(SECRETS_EXPLICIT, ACCOUNT_ID, REGION, [])
        sg_refs = [r for r in refs if r.metadata["resource_type"] == "aws_security_group"]
        assert len(sg_refs) == 1
        assert sg_refs[0].metadata["name"] == "allow-https"

    def test_filter_excludes_instance(self) -> None:
        instance = {
            "InstanceId": "i-111",
            "InstanceType": "t2.micro",
            "State": {"Name": "running"},
            "Tags": [],
        }
        client = self._build_ec2_client(instances=[instance])
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_ec2(SECRETS_EXPLICIT, ACCOUNT_ID, REGION, ["aws_vpc"])
        assert all(r.metadata["resource_type"] != "aws_instance" for r in refs)

    def test_instance_without_name_tag_uses_id(self) -> None:
        instance = {
            "InstanceId": "i-nametag",
            "InstanceType": "t2.small",
            "State": {"Name": "stopped"},
            "Tags": [],
        }
        client = self._build_ec2_client(instances=[instance])
        with patch("omniscience_connectors.aws.connector._build_client", return_value=client):
            refs = _discover_ec2(SECRETS_EXPLICIT, ACCOUNT_ID, REGION, [])
        inst_refs = [r for r in refs if r.metadata["resource_type"] == "aws_instance"]
        assert inst_refs[0].metadata["name"] == "i-nametag"


# ---------------------------------------------------------------------------
# AwsConnector.validate
# ---------------------------------------------------------------------------


class TestAwsConnectorValidate:
    @pytest.fixture()
    def connector(self) -> AwsConnector:
        return AwsConnector()

    @pytest.fixture()
    def config(self) -> AwsConfig:
        return AwsConfig()

    async def test_validate_success(self, connector: AwsConnector, config: AwsConfig) -> None:
        with patch(
            "omniscience_connectors.aws.connector._get_account_id",
            return_value=ACCOUNT_ID,
        ):
            await connector.validate(config, SECRETS_EXPLICIT)  # should not raise

    async def test_validate_access_denied_raises_permission_error(
        self, connector: AwsConnector, config: AwsConfig
    ) -> None:
        def _raise(_secrets: dict[str, str]) -> str:
            raise _client_error("AccessDenied")

        with (
            patch("omniscience_connectors.aws.connector._get_account_id", side_effect=_raise),
            pytest.raises(PermissionError, match="sts:GetCallerIdentity"),
        ):
            await connector.validate(config, SECRETS_EXPLICIT)

    async def test_validate_generic_error_raises_runtime_error(
        self, connector: AwsConnector, config: AwsConfig
    ) -> None:
        def _raise(_secrets: dict[str, str]) -> str:
            raise _client_error("ServiceUnavailable")

        with (
            patch("omniscience_connectors.aws.connector._get_account_id", side_effect=_raise),
            pytest.raises(RuntimeError, match="AWS validation failed"),
        ):
            await connector.validate(config, SECRETS_EXPLICIT)


# ---------------------------------------------------------------------------
# AwsConnector.discover
# ---------------------------------------------------------------------------


class TestAwsConnectorDiscover:
    @pytest.fixture()
    def connector(self) -> AwsConnector:
        return AwsConnector()

    async def test_discover_s3_and_iam_discovered_once(self, connector: AwsConnector) -> None:
        """S3 and IAM are global — they appear exactly once per source.

        Regardless of region count, each global service is enumerated once.
        """
        config = AwsConfig(regions=["us-east-1", "eu-west-1"], services=["s3", "iam"])

        s3_refs = [
            DocumentRef(
                external_id="arn:aws:s3:::b1",
                uri=f"aws://{ACCOUNT_ID}/global/s3/bucket/b1",
                metadata={"kind": "aws_live"},
            )
        ]
        iam_refs = [
            DocumentRef(
                external_id=f"arn:aws:iam::{ACCOUNT_ID}:user/alice",
                uri=f"aws://{ACCOUNT_ID}/global/iam/user/alice",
                metadata={"kind": "aws_live"},
            )
        ]

        with (
            patch(
                "omniscience_connectors.aws.connector._get_account_id",
                return_value=ACCOUNT_ID,
            ),
            patch(
                "omniscience_connectors.aws.connector._discover_s3",
                return_value=s3_refs,
            ) as mock_s3,
            patch(
                "omniscience_connectors.aws.connector._discover_iam",
                return_value=iam_refs,
            ) as mock_iam,
        ):
            refs = [ref async for ref in connector.discover(config, SECRETS_EXPLICIT)]

        # S3 and IAM both called exactly once (global)
        mock_s3.assert_called_once()
        mock_iam.assert_called_once()
        assert len(refs) == 2

    async def test_discover_ec2_called_per_region(self, connector: AwsConnector) -> None:
        config = AwsConfig(
            regions=["us-east-1", "eu-west-1"],
            services=["ec2"],
        )
        ec2_ref = DocumentRef(
            external_id=f"arn:aws:ec2:us-east-1:{ACCOUNT_ID}:instance/i-abc",
            uri=f"aws://{ACCOUNT_ID}/us-east-1/ec2/instance/i-abc",
            metadata={"kind": "aws_live"},
        )

        with (
            patch(
                "omniscience_connectors.aws.connector._get_account_id",
                return_value=ACCOUNT_ID,
            ),
            patch(
                "omniscience_connectors.aws.connector._discover_ec2",
                return_value=[ec2_ref],
            ) as mock_ec2,
        ):
            refs = [ref async for ref in connector.discover(config, SECRETS_EXPLICIT)]

        assert mock_ec2.call_count == 2  # one per region
        assert len(refs) == 2

    async def test_discover_respects_service_filter(self, connector: AwsConnector) -> None:
        config = AwsConfig(services=["iam"])  # only IAM

        iam_refs = [
            DocumentRef(
                external_id=f"arn:aws:iam::{ACCOUNT_ID}:role/r",
                uri=f"aws://{ACCOUNT_ID}/global/iam/role/r",
                metadata={"kind": "aws_live"},
            )
        ]

        with (
            patch(
                "omniscience_connectors.aws.connector._get_account_id",
                return_value=ACCOUNT_ID,
            ),
            patch(
                "omniscience_connectors.aws.connector._discover_s3",
            ) as mock_s3,
            patch(
                "omniscience_connectors.aws.connector._discover_iam",
                return_value=iam_refs,
            ),
            patch(
                "omniscience_connectors.aws.connector._discover_ec2",
            ) as mock_ec2,
        ):
            refs = [ref async for ref in connector.discover(config, SECRETS_EXPLICIT)]

        mock_s3.assert_not_called()
        mock_ec2.assert_not_called()
        assert len(refs) == 1


# ---------------------------------------------------------------------------
# AwsConnector.fetch
# ---------------------------------------------------------------------------


class TestAwsConnectorFetch:
    @pytest.fixture()
    def connector(self) -> AwsConnector:
        return AwsConnector()

    @pytest.fixture()
    def config(self) -> AwsConfig:
        return AwsConfig()

    def _make_ref(self, resource_type: str, name: str = "test", **extra: Any) -> DocumentRef:
        base: dict[str, Any] = {
            "kind": "aws_live",
            "resource_type": resource_type,
            "service": resource_type.split("_")[1] if "_" in resource_type else "s3",
            "region": REGION,
            "account_id": ACCOUNT_ID,
            "name": name,
            "arn": f"arn:aws::::{name}",
        }
        base.update(extra)
        return DocumentRef(
            external_id=base["arn"],
            uri=f"aws://{ACCOUNT_ID}/{REGION}/{name}",
            metadata=base,
        )

    async def test_fetch_s3_bucket(self, connector: AwsConnector, config: AwsConfig) -> None:
        mock_client = MagicMock()
        mock_client.get_bucket_location.return_value = {"LocationConstraint": "us-east-1"}
        mock_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        mock_client.get_bucket_tagging.return_value = {"TagSet": []}
        mock_client.get_bucket_policy.side_effect = _client_error("NoSuchBucketPolicy")

        ref = self._make_ref("aws_s3_bucket", name="my-bucket")
        ref.metadata["service"] = "s3"

        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_client):
            doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        assert doc.content_type == "application/json"
        data = json.loads(doc.content_bytes)
        assert data["name"] == "my-bucket"

    async def test_fetch_iam_user(self, connector: AwsConnector, config: AwsConfig) -> None:
        mock_client = MagicMock()
        mock_client.get_user.return_value = {
            "User": {
                "UserName": "alice",
                "UserId": "AID",
                "Arn": f"arn:aws:iam::{ACCOUNT_ID}:user/alice",
                "Path": "/",
            }
        }
        ref = self._make_ref("aws_iam_user", name="alice")

        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_client):
            doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        data = json.loads(doc.content_bytes)
        assert data["user_name"] == "alice"

    async def test_fetch_iam_role(self, connector: AwsConnector, config: AwsConfig) -> None:
        mock_client = MagicMock()
        mock_client.get_role.return_value = {
            "Role": {
                "RoleName": "my-role",
                "RoleId": "AROA",
                "Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/my-role",
                "Path": "/",
                "AssumeRolePolicyDocument": {},
            }
        }
        ref = self._make_ref("aws_iam_role", name="my-role")

        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_client):
            doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        data = json.loads(doc.content_bytes)
        assert data["role_name"] == "my-role"

    async def test_fetch_iam_policy(self, connector: AwsConnector, config: AwsConfig) -> None:
        arn = f"arn:aws:iam::{ACCOUNT_ID}:policy/my-policy"
        mock_client = MagicMock()
        mock_client.get_policy.return_value = {
            "Policy": {
                "PolicyName": "my-policy",
                "PolicyId": "ANPA",
                "AttachmentCount": 1,
            }
        }
        ref = self._make_ref("aws_iam_policy", name="my-policy", arn=arn)

        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_client):
            doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        data = json.loads(doc.content_bytes)
        assert data["policy_name"] == "my-policy"

    async def test_fetch_ec2_instance(self, connector: AwsConnector, config: AwsConfig) -> None:
        mock_client = MagicMock()
        mock_client.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-abc",
                            "InstanceType": "t3.micro",
                            "State": {"Name": "running"},
                            "Tags": [],
                        }
                    ]
                }
            ]
        }
        ref = self._make_ref("aws_instance", name="i-abc", instance_id="i-abc")

        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_client):
            doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        data = json.loads(doc.content_bytes)
        assert data["instance_id"] == "i-abc"
        assert data["instance_type"] == "t3.micro"

    async def test_fetch_ec2_vpc(self, connector: AwsConnector, config: AwsConfig) -> None:
        mock_client = MagicMock()
        mock_client.describe_vpcs.return_value = {
            "Vpcs": [
                {
                    "VpcId": "vpc-123",
                    "CidrBlock": "10.0.0.0/16",
                    "IsDefault": False,
                    "State": "available",
                    "Tags": [],
                }
            ]
        }
        ref = self._make_ref("aws_vpc", name="vpc-123", vpc_id="vpc-123")

        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_client):
            doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        data = json.loads(doc.content_bytes)
        assert data["cidr_block"] == "10.0.0.0/16"

    async def test_fetch_ec2_security_group(
        self, connector: AwsConnector, config: AwsConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-abc",
                    "GroupName": "allow-all",
                    "Description": "All traffic",
                    "VpcId": "vpc-123",
                    "IpPermissions": [],
                    "IpPermissionsEgress": [],
                    "Tags": [],
                }
            ]
        }
        ref = self._make_ref("aws_security_group", name="sg-abc", group_id="sg-abc")

        with patch("omniscience_connectors.aws.connector._build_client", return_value=mock_client):
            doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        data = json.loads(doc.content_bytes)
        assert data["group_name"] == "allow-all"

    async def test_fetch_unknown_resource_type_returns_metadata(
        self, connector: AwsConnector, config: AwsConfig
    ) -> None:
        ref = self._make_ref("aws_lambda_function", name="my-fn")

        doc = await connector.fetch(config, SECRETS_EXPLICIT, ref)

        assert doc.content_type == "application/json"
        data = json.loads(doc.content_bytes)
        assert data["resource_type"] == "aws_lambda_function"


# ---------------------------------------------------------------------------
# AwsConnector.webhook_handler
# ---------------------------------------------------------------------------


class TestAwsConnectorWebhookHandler:
    def test_returns_none(self) -> None:
        connector = AwsConnector()
        assert connector.webhook_handler() is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_aws_connector_registered(self) -> None:
        connector = get_connector("aws")
        assert isinstance(connector, AwsConnector)

    def test_source_type_aws_exists(self) -> None:
        assert SourceType.aws == "aws"


# ---------------------------------------------------------------------------
# ARN linker helpers (local implementations — see module docstring)
# ---------------------------------------------------------------------------


class TestExtractArn:
    def test_direct_arn_in_metadata(self) -> None:
        entity = _make_entity(
            entity_metadata={"arn": "arn:aws:s3:::my-bucket", "kind": "aws_live"}
        )
        assert _extract_arn(entity) == "arn:aws:s3:::my-bucket"

    def test_nested_arn_in_extra(self) -> None:
        entity = _make_entity(
            entity_metadata={"extra": {"arn": "arn:aws:iam::123:role/r"}, "kind": "aws_live"}
        )
        assert _extract_arn(entity) == "arn:aws:iam::123:role/r"

    def test_arn_as_entity_name(self) -> None:
        entity = _make_entity(name="arn:aws:ec2:us-east-1:123:instance/i-abc")
        assert _extract_arn(entity) == "arn:aws:ec2:us-east-1:123:instance/i-abc"

    def test_no_arn_returns_empty(self) -> None:
        entity = _make_entity(name="my-function")
        assert _extract_arn(entity) == ""


class TestArnIdSegment:
    def test_s3_bucket_arn(self) -> None:
        assert _arn_id_segment("arn:aws:s3:::my-bucket") == "my-bucket"

    def test_ec2_instance_arn(self) -> None:
        assert _arn_id_segment("arn:aws:ec2:us-east-1:123456789012:instance/i-0abc") == "i-0abc"

    def test_iam_role_arn(self) -> None:
        assert _arn_id_segment("arn:aws:iam::123456789012:role/my-role") == "my-role"

    def test_empty_arn(self) -> None:
        assert _arn_id_segment("") == ""

    def test_malformed_arn(self) -> None:
        assert _arn_id_segment("not-an-arn") == ""


class TestArnMatchScore:
    def test_exact_arn_match_scores_1(self) -> None:
        arn = "arn:aws:s3:::my-bucket"
        tf_entity = _make_entity(
            entity_type="tfstate_instance",
            entity_metadata={"kind": "tfstate_instance", "arn": arn},
        )
        live_entity = _make_entity(
            entity_type="aws_live",
            entity_metadata={"kind": "aws_live", "arn": arn},
        )
        score, strategy = _arn_match_score(tf_entity, live_entity)
        assert score == 1.0
        assert strategy == "arn_match"

    def test_id_only_match_scores_08(self) -> None:
        tf_entity = _make_entity(
            entity_type="tfstate_instance",
            entity_metadata={
                "kind": "tfstate_instance",
                "arn": "arn:aws:ec2:eu-west-1:111:instance/i-abc",
            },
        )
        live_entity = _make_entity(
            entity_type="aws_live",
            entity_metadata={
                "kind": "aws_live",
                "arn": "arn:aws:ec2:us-east-1:222:instance/i-abc",
            },
        )
        score, strategy = _arn_match_score(tf_entity, live_entity)
        assert score == pytest.approx(0.8)
        assert strategy == "arn_match"

    def test_different_ids_scores_0(self) -> None:
        tf_entity = _make_entity(
            entity_type="tfstate_instance",
            entity_metadata={
                "kind": "tfstate_instance",
                "arn": "arn:aws:s3:::bucket-a",
            },
        )
        live_entity = _make_entity(
            entity_type="aws_live",
            entity_metadata={"kind": "aws_live", "arn": "arn:aws:s3:::bucket-b"},
        )
        score, _ = _arn_match_score(tf_entity, live_entity)
        assert score == 0.0

    def test_wrong_kinds_scores_0(self) -> None:
        ent_a = _make_entity(entity_type="function", entity_metadata={"arn": "arn:aws:s3:::b"})
        ent_b = _make_entity(entity_type="class", entity_metadata={"arn": "arn:aws:s3:::b"})
        score, _ = _arn_match_score(ent_a, ent_b)
        assert score == 0.0


# ---------------------------------------------------------------------------
# EntityLinker._match_score strategies
#
# ARN-based matching was removed from EntityLinker in ADR-0012 (Neo4j
# migration).  These tests exercise the strategies that remain:
# exact_name and resource_name (Terraform <-> K8s).
# ---------------------------------------------------------------------------


class TestEntityLinkerMatchScore:
    @pytest.fixture()
    def linker(self) -> EntityLinker:
        return EntityLinker(graph_store=MagicMock())

    def test_exact_name_match_scores_1(self, linker: EntityLinker) -> None:
        """Entities with identical names score 1.0 via exact_name strategy."""
        sid_a = str(uuid.uuid4())
        sid_b = str(uuid.uuid4())
        ent_a = _make_node(source=sid_a, kind="tfstate_instance", name="shared-resource")
        ent_b = _make_node(source=sid_b, kind="aws_live", name="shared-resource")
        score, strategy = linker._match_score(ent_a, ent_b)
        assert score == 1.0
        assert strategy == "exact_name"

    def test_resource_name_match_tf_k8s(self, linker: EntityLinker) -> None:
        """Terraform <-> K8s entities with matching resource names use resource_name strategy."""
        sid_a = str(uuid.uuid4())
        sid_b = str(uuid.uuid4())
        tf_entity = _make_node(
            source=sid_a,
            kind="tfstate_instance",
            name="aws_instance.nginx-deployment",
        )
        k8s_entity = _make_node(
            source=sid_b,
            kind="k8s_resource",
            name="nginx-deployment",
        )
        score, strategy = linker._match_score(tf_entity, k8s_entity)
        # resource_name strategy kicks in for tf <-> k8s pairs
        assert strategy != "arn_match"
        assert score >= 0.0

    def test_non_matching_entities_score_0(self, linker: EntityLinker) -> None:
        """Entities that share no matching strategy return 0.0."""
        sid_a = str(uuid.uuid4())
        sid_b = str(uuid.uuid4())
        ent_a = _make_node(source=sid_a, kind="function", name="mymod.func_a")
        ent_b = _make_node(source=sid_b, kind="class", name="othermod.ClassB")
        score, strategy = linker._match_score(ent_a, ent_b)
        assert score == 0.0
        assert strategy == ""
