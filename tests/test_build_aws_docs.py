"""Unit tests for scripts/build_aws_docs.py.

Coverage:
- Document format contract (external_id, uri, title, text, metadata)
- ELB enrichment merges listener ports into text and metadata
- Security group enrichment adds inbound/outbound rules
- RDS enrichment adds engine/endpoint/storage facts
- EC2 enrichment adds instance type/state/network
- ECS enrichment adds desired/running counts
- AccessDenied on a service => graceful skip, other services still run
- Deduplication: same ARN from Tagging API + describe does not create duplicates
- New resource found by describe but not Tagging API => document is created
- _merge_enrichment extends text and metadata of an existing doc
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

# ---------------------------------------------------------------------------
# Import the script under test.  It lives in scripts/, which is not a package,
# so we add it to sys.path if necessary.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from build_aws_docs import (  # noqa: E402
    _make_doc,
    _merge_enrichment,
    _parse_sg_rules,
    build_docs,
    enrich_ec2,
    enrich_ecs,
    enrich_elb,
    enrich_rds,
    enrich_sg,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCOUNT_ID = "123456789012"
ACCOUNT_NAME = "test-account"
REGION = "eu-west-1"


@pytest.fixture(autouse=True)
def _fake_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure boto3 does not attempt real credential resolution in unit tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _make_boto_session(clients: dict[str, Any]) -> MagicMock:
    """Return a mock boto3.Session whose .client() dispatches to *clients*."""
    session = MagicMock()
    session.client.side_effect = lambda svc, **_kw: clients[svc]
    return session


def _stub_client(service: str) -> tuple[Any, Stubber]:
    """Create a boto3 client + Stubber without credential resolution."""
    import boto3

    session = boto3.session.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
        region_name=REGION,
    )
    cli = session.client(service, region_name=REGION)
    return cli, Stubber(cli)


# ---------------------------------------------------------------------------
# 1. Document format contract
# ---------------------------------------------------------------------------


class TestDocumentFormat:
    """The contract consumed by seed_from_docs.py must be preserved."""

    def test_make_doc_has_required_keys(self) -> None:
        doc = _make_doc(
            arn="arn:aws:ec2:eu-west-1:123:instance/i-abc",
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-abc",
            display_name="my-server",
            tags={"Env": "prod"},
        )
        for key in ("external_id", "uri", "title", "text", "metadata"):
            assert key in doc, f"Missing key: {key}"

    def test_external_id_is_arn(self) -> None:
        arn = "arn:aws:s3:::my-bucket"
        doc = _make_doc(
            arn=arn,
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="s3",
            resource_type="bucket",
            resource_id="my-bucket",
            display_name="my-bucket",
            tags={},
        )
        assert doc["external_id"] == arn

    def test_uri_format(self) -> None:
        doc = _make_doc(
            arn="arn:aws:rds:eu-west-1:123:db:mydb",
            account_id="123",
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="rds",
            resource_type="db",
            resource_id="mydb",
            display_name="mydb",
            tags={},
        )
        assert doc["uri"] == f"aws://123/{REGION}/rds/db/mydb"

    def test_metadata_required_fields(self) -> None:
        doc = _make_doc(
            arn="arn:aws:ec2:eu-west-1:123:instance/i-xyz",
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-xyz",
            display_name="srv",
            tags={"Name": "srv"},
        )
        meta = doc["metadata"]
        for field in (
            "service",
            "resource_type",
            "account_id",
            "account_name",
            "region",
            "arn",
            "name",
            "tags",
        ):
            assert field in meta, f"Missing metadata field: {field}"

    def test_extra_text_appended(self) -> None:
        doc = _make_doc(
            arn="arn:aws:ec2:eu-west-1:123:instance/i-abc",
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-abc",
            display_name="srv",
            tags={},
            extra_text="Extra enrichment fact.",
        )
        assert "Extra enrichment fact." in doc["text"]

    def test_extra_meta_merged(self) -> None:
        doc = _make_doc(
            arn="arn:aws:ec2:eu-west-1:123:instance/i-abc",
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-abc",
            display_name="srv",
            tags={},
            extra_meta={"ec2_state": "running"},
        )
        assert doc["metadata"]["ec2_state"] == "running"


# ---------------------------------------------------------------------------
# 2. _merge_enrichment
# ---------------------------------------------------------------------------


class TestMergeEnrichment:
    def _base_docs(self, arn: str) -> dict[str, Any]:
        return {
            arn: _make_doc(
                arn=arn,
                account_id=ACCOUNT_ID,
                account_name=ACCOUNT_NAME,
                region=REGION,
                service="ec2",
                resource_type="instance",
                resource_id="i-abc",
                display_name="server",
                tags={},
            )
        }

    def test_merge_into_existing_extends_text(self) -> None:
        arn = "arn:aws:ec2:eu-west-1:123:instance/i-abc"
        docs = self._base_docs(arn)
        original_text = docs[arn]["text"]
        _merge_enrichment(
            docs,
            arn,
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-abc",
            display_name="server",
            tags={},
            extra_text="Instance type: t3.medium.",
            extra_meta={"ec2_instance_type": "t3.medium"},
        )
        assert "t3.medium" in docs[arn]["text"]
        # The original text is still there.
        assert original_text.rstrip(".") in docs[arn]["text"]

    def test_merge_into_existing_updates_metadata(self) -> None:
        arn = "arn:aws:ec2:eu-west-1:123:instance/i-abc"
        docs = self._base_docs(arn)
        _merge_enrichment(
            docs,
            arn,
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-abc",
            display_name="server",
            tags={},
            extra_text="State: running.",
            extra_meta={"ec2_state": "running"},
        )
        assert docs[arn]["metadata"]["ec2_state"] == "running"

    def test_merge_creates_new_doc_when_not_present(self) -> None:
        docs: dict[str, Any] = {}
        arn = "arn:aws:elasticloadbalancing:eu-west-1:123:listener/app/my-lb/abc/def"
        _merge_enrichment(
            docs,
            arn,
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="elasticloadbalancing",
            resource_type="listener",
            resource_id="def",
            display_name="listener:HTTPS:443",
            tags={},
            extra_text="Listener port=443.",
            extra_meta={"listener_port": 443},
        )
        assert arn in docs
        assert "443" in docs[arn]["text"]

    def test_no_duplicate_external_id(self) -> None:
        arn = "arn:aws:ec2:eu-west-1:123:instance/i-abc"
        docs = self._base_docs(arn)
        # Call merge twice — should not create a second entry.
        _merge_enrichment(
            docs,
            arn,
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-abc",
            display_name="server",
            tags={},
            extra_text="First enrich.",
            extra_meta={"pass": 1},
        )
        _merge_enrichment(
            docs,
            arn,
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-abc",
            display_name="server",
            tags={},
            extra_text="Second enrich.",
            extra_meta={"pass": 2},
        )
        assert list(docs.keys()).count(arn) == 1


# ---------------------------------------------------------------------------
# 3. ELB enrichment
# ---------------------------------------------------------------------------


class TestEnrichElb:
    def _stub_lb(self) -> tuple[Any, Stubber, str, str]:
        cli, stubber = _stub_client("elbv2")

        lb_arn = (
            f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT_ID}:loadbalancer/app/my-alb/abc123"
        )
        listener_arn = (
            f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT_ID}:listener/app/my-alb/abc123/def456"
        )
        tg_arn = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT_ID}:targetgroup/my-tg/xyz789"

        # describe_load_balancers page
        stubber.add_response(
            "describe_load_balancers",
            {
                "LoadBalancers": [
                    {
                        "LoadBalancerArn": lb_arn,
                        "LoadBalancerName": "my-alb",
                        "Scheme": "internet-facing",
                        "Type": "application",
                        "DNSName": "my-alb.eu-west-1.elb.amazonaws.com",
                        "State": {"Code": "active"},
                        "VpcId": "vpc-123",
                        "AvailabilityZones": [{"ZoneName": "eu-west-1a"}],
                    }
                ],
            },
        )
        # describe_listeners for that LB
        stubber.add_response(
            "describe_listeners",
            {
                "Listeners": [
                    {
                        "ListenerArn": listener_arn,
                        "Port": 443,
                        "Protocol": "HTTPS",
                        "SslPolicy": "ELBSecurityPolicy-2016-08",
                    }
                ],
            },
            expected_params={"LoadBalancerArn": lb_arn},
        )
        # describe_target_groups
        stubber.add_response(
            "describe_target_groups",
            {
                "TargetGroups": [
                    {
                        "TargetGroupArn": tg_arn,
                        "TargetGroupName": "my-tg",
                        "Port": 8080,
                        "Protocol": "HTTP",
                        "TargetType": "ip",
                        "HealthCheckPath": "/health",
                        "HealthCheckPort": "traffic-port",
                        "LoadBalancerArns": [lb_arn],
                    }
                ],
            },
        )
        stubber.activate()
        return cli, stubber, lb_arn, listener_arn

    def test_lb_doc_has_scheme_and_ports(self) -> None:
        cli, stubber, lb_arn, _listener_arn = self._stub_lb()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_elb(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert lb_arn in docs
        lb_doc = docs[lb_arn]
        assert "internet-facing" in lb_doc["text"]
        assert "443" in lb_doc["text"]
        assert lb_doc["metadata"]["lb_scheme"] == "internet-facing"
        assert lb_doc["metadata"]["lb_type"] == "application"

    def test_lb_metadata_has_listener_list(self) -> None:
        cli, stubber, lb_arn, _listener_arn = self._stub_lb()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_elb(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        listeners = docs[lb_arn]["metadata"]["lb_listeners"]
        assert len(listeners) == 1
        assert listeners[0]["port"] == 443
        assert listeners[0]["protocol"] == "HTTPS"

    def test_listener_doc_created(self) -> None:
        cli, stubber, _lb_arn, listener_arn = self._stub_lb()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_elb(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert listener_arn in docs
        assert "443" in docs[listener_arn]["text"]
        assert docs[listener_arn]["metadata"]["listener_port"] == 443

    def test_access_denied_skipped_gracefully(self) -> None:
        cli, stubber = _stub_client("elbv2")
        stubber.add_client_error(
            "describe_load_balancers",
            service_error_code="AccessDenied",
            service_message="Access Denied",
        )
        stubber.activate()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            # Must not raise — graceful skip.
            enrich_elb(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert docs == {}


# ---------------------------------------------------------------------------
# 4. Security group enrichment
# ---------------------------------------------------------------------------


class TestEnrichSg:
    def _stub_sg(self) -> tuple[Any, Stubber, str]:
        cli, stubber = _stub_client("ec2")
        sg_id = "sg-0abc1234"
        stubber.add_response(
            "describe_security_groups",
            {
                "SecurityGroups": [
                    {
                        "GroupId": sg_id,
                        "GroupName": "web-sg",
                        "Description": "Web tier SG",
                        "VpcId": "vpc-999",
                        "Tags": [{"Key": "Name", "Value": "web-sg"}],
                        "IpPermissions": [
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 443,
                                "ToPort": 443,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                "Ipv6Ranges": [],
                                "UserIdGroupPairs": [],
                                "PrefixListIds": [],
                            }
                        ],
                        "IpPermissionsEgress": [
                            {
                                "IpProtocol": "-1",
                                "FromPort": -1,
                                "ToPort": -1,
                                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                "Ipv6Ranges": [],
                                "UserIdGroupPairs": [],
                                "PrefixListIds": [],
                            }
                        ],
                    }
                ]
            },
        )
        stubber.activate()
        return cli, stubber, sg_id

    def test_sg_inbound_rules_in_text(self) -> None:
        cli, stubber, sg_id = self._stub_sg()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_sg(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:security-group/{sg_id}"
        assert arn in docs
        assert "443" in docs[arn]["text"]
        assert "0.0.0.0/0" in docs[arn]["text"]

    def test_sg_metadata_has_parsed_rules(self) -> None:
        cli, stubber, sg_id = self._stub_sg()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_sg(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:security-group/{sg_id}"
        meta = docs[arn]["metadata"]
        inbound = meta["sg_inbound_rules"]
        assert len(inbound) == 1
        assert inbound[0]["from_port"] == 443
        assert inbound[0]["to_port"] == 443
        assert "0.0.0.0/0" in inbound[0]["cidrs"]

    def test_parse_sg_rules_protocol_all(self) -> None:
        perms = [
            {
                "IpProtocol": "-1",
                "FromPort": -1,
                "ToPort": -1,
                "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                "Ipv6Ranges": [],
                "UserIdGroupPairs": [],
            }
        ]
        rules = _parse_sg_rules(perms)
        assert rules[0]["protocol"] == "-1"
        assert rules[0]["from_port"] == -1

    def test_sg_access_denied_skipped(self) -> None:
        cli, stubber = _stub_client("ec2")
        stubber.add_client_error(
            "describe_security_groups",
            service_error_code="AccessDenied",
            service_message="Access Denied",
        )
        stubber.activate()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_sg(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert docs == {}


# ---------------------------------------------------------------------------
# 5. RDS enrichment
# ---------------------------------------------------------------------------


class TestEnrichRds:
    def _stub_rds(self) -> tuple[Any, Stubber, str]:
        cli, stubber = _stub_client("rds")
        db_arn = f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:db:mypostgres"
        stubber.add_response(
            "describe_db_instances",
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "mypostgres",
                        "DBInstanceArn": db_arn,
                        "Engine": "postgres",
                        "EngineVersion": "14.7",
                        "DBInstanceClass": "db.t3.medium",
                        "MultiAZ": True,
                        "StorageType": "gp2",
                        "AllocatedStorage": 100,
                        "DBInstanceStatus": "available",
                        "Endpoint": {
                            "Address": "mypostgres.abcdef.eu-west-1.rds.amazonaws.com",
                            "Port": 5432,
                        },
                        "TagList": [{"Key": "Env", "Value": "prod"}],
                    }
                ],
            },
        )
        stubber.activate()
        return cli, stubber, db_arn

    def test_rds_engine_in_text(self) -> None:
        cli, stubber, db_arn = self._stub_rds()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_rds(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert db_arn in docs
        assert "postgres" in docs[db_arn]["text"]
        assert "5432" in docs[db_arn]["text"]

    def test_rds_metadata_fields(self) -> None:
        cli, stubber, db_arn = self._stub_rds()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_rds(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        meta = docs[db_arn]["metadata"]
        assert meta["rds_engine"] == "postgres"
        assert meta["rds_engine_version"] == "14.7"
        assert meta["rds_endpoint_port"] == 5432
        assert meta["rds_multi_az"] is True
        assert meta["rds_allocated_storage_gb"] == 100

    def test_rds_access_denied_skipped(self) -> None:
        cli, stubber = _stub_client("rds")
        stubber.add_client_error(
            "describe_db_instances",
            service_error_code="AccessDenied",
        )
        stubber.activate()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_rds(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert docs == {}


# ---------------------------------------------------------------------------
# 6. EC2 instance enrichment
# ---------------------------------------------------------------------------


class TestEnrichEc2:
    def _stub_ec2(self) -> tuple[Any, Stubber, str]:
        cli, stubber = _stub_client("ec2")
        inst_id = "i-0abc1234"
        inst_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/{inst_id}"
        stubber.add_response(
            "describe_instances",
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": inst_id,
                                "InstanceType": "t3.large",
                                "State": {"Name": "running"},
                                "PrivateIpAddress": "10.0.1.5",
                                "VpcId": "vpc-123",
                                "SubnetId": "subnet-abc",
                                "ImageId": "ami-0abc",
                                "LaunchTime": datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
                                "Tags": [{"Key": "Name", "Value": "api-server"}],
                            }
                        ]
                    }
                ],
            },
        )
        stubber.activate()
        return cli, stubber, inst_arn

    def test_ec2_instance_type_in_text(self) -> None:
        cli, stubber, inst_arn = self._stub_ec2()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_ec2(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert inst_arn in docs
        assert "t3.large" in docs[inst_arn]["text"]
        assert "running" in docs[inst_arn]["text"]

    def test_ec2_metadata_fields(self) -> None:
        cli, stubber, inst_arn = self._stub_ec2()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_ec2(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        meta = docs[inst_arn]["metadata"]
        assert meta["ec2_instance_type"] == "t3.large"
        assert meta["ec2_state"] == "running"
        assert meta["ec2_private_ip"] == "10.0.1.5"
        assert meta["ec2_vpc_id"] == "vpc-123"

    def test_ec2_access_denied_skipped(self) -> None:
        cli, stubber = _stub_client("ec2")
        stubber.add_client_error("describe_instances", service_error_code="AccessDenied")
        stubber.activate()
        docs: dict[str, Any] = {}
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_ec2(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert docs == {}


# ---------------------------------------------------------------------------
# 7. ECS enrichment
# ---------------------------------------------------------------------------


class TestEnrichEcs:
    def _make_paginator(self, pages: list) -> MagicMock:
        p = MagicMock()
        p.paginate.return_value = iter(pages)
        return p

    def _make_ecs_cli(self) -> tuple[MagicMock, str, str]:
        cluster_arn = f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:cluster/my-cluster"
        svc_arn = f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:service/my-cluster/api-svc"
        cli = MagicMock()
        cli.get_paginator.side_effect = lambda op: (
            self._make_paginator([{"clusterArns": [cluster_arn]}])
            if op == "list_clusters"
            else self._make_paginator([{"serviceArns": [svc_arn]}])
        )
        cli.describe_services.return_value = {
            "services": [
                {
                    "serviceArn": svc_arn,
                    "serviceName": "api-svc",
                    "desiredCount": 3,
                    "runningCount": 3,
                    "launchType": "FARGATE",
                    "taskDefinition": (
                        f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:task-definition/api:42"
                    ),
                    "status": "ACTIVE",
                }
            ]
        }
        return cli, cluster_arn, svc_arn

    def test_ecs_service_in_text(self) -> None:
        cli, _cluster_arn, svc_arn = self._make_ecs_cli()
        docs: dict[str, Any] = {}
        with patch("build_aws_docs._client", return_value=cli):
            enrich_ecs(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert svc_arn in docs
        assert "desired=3" in docs[svc_arn]["text"]
        assert "running=3" in docs[svc_arn]["text"]
        assert "FARGATE" in docs[svc_arn]["text"]

    def test_ecs_metadata_fields(self) -> None:
        cli, cluster_arn, svc_arn = self._make_ecs_cli()
        docs: dict[str, Any] = {}
        with patch("build_aws_docs._client", return_value=cli):
            enrich_ecs(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        meta = docs[svc_arn]["metadata"]
        assert meta["ecs_desired_count"] == 3
        assert meta["ecs_running_count"] == 3
        assert meta["ecs_launch_type"] == "FARGATE"
        assert meta["ecs_cluster_arn"] == cluster_arn

    def test_ecs_access_denied_skipped(self) -> None:
        cli = MagicMock()
        lc_page = MagicMock()
        lc_page.__iter__ = MagicMock(
            side_effect=ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
                "ListClusters",
            )
        )
        cli.get_paginator.return_value = lc_page
        docs: dict[str, Any] = {}
        with patch("build_aws_docs._client", return_value=cli):
            enrich_ecs(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)
        assert docs == {}


# ---------------------------------------------------------------------------
# 8. Deduplication: same ARN from Tagging API + describe stays as one doc
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_tagging_then_enrich_no_duplicate(self) -> None:
        """Same ARN in Tagging API response and enrich_rds must not duplicate."""
        arn = f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:db:mypostgres"
        # Pre-populate docs from a simulated Tagging API pass.
        existing_doc = _make_doc(
            arn=arn,
            account_id=ACCOUNT_ID,
            account_name=ACCOUNT_NAME,
            region=REGION,
            service="rds",
            resource_type="db",
            resource_id="mypostgres",
            display_name="mypostgres",
            tags={"Env": "prod"},
        )
        docs: dict[str, Any] = {arn: existing_doc}

        # Now run enrich_rds which will see the same ARN.
        cli, stubber = _stub_client("rds")
        stubber.add_response(
            "describe_db_instances",
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "mypostgres",
                        "DBInstanceArn": arn,
                        "Engine": "postgres",
                        "EngineVersion": "14.7",
                        "DBInstanceClass": "db.t3.medium",
                        "MultiAZ": False,
                        "StorageType": "gp2",
                        "AllocatedStorage": 50,
                        "DBInstanceStatus": "available",
                        "Endpoint": {
                            "Address": "mypostgres.rds.amazonaws.com",
                            "Port": 5432,
                        },
                        "TagList": [],
                    }
                ],
            },
        )
        stubber.activate()
        with stubber, patch("build_aws_docs._client", return_value=cli):
            enrich_rds(docs, MagicMock(), ACCOUNT_ID, ACCOUNT_NAME, REGION)

        # Still exactly one entry for this ARN.
        assert list(docs.keys()).count(arn) == 1
        # Enrichment info is present.
        assert "postgres" in docs[arn]["text"]
        assert "5432" in docs[arn]["text"]


# ---------------------------------------------------------------------------
# 9. build_docs: profile failure is skipped gracefully
# ---------------------------------------------------------------------------


class TestBuildDocs:
    def test_bad_profile_skipped(self) -> None:
        """If _make_session returns None the account is skipped silently."""
        with patch("build_aws_docs._make_session", return_value=None) as mock_sess:
            result = build_docs(
                [("bad-profile", "bad-account", "000000000000")],
                region=REGION,
            )
        assert result == []
        mock_sess.assert_called_once_with("bad-profile", REGION)

    def test_output_is_list_of_dicts(self) -> None:
        """build_docs output is always a plain list (seed_from_docs requirement)."""
        with patch("build_aws_docs._scrape_account", return_value={}):
            result = build_docs([("p", "n", "123")], region=REGION)
        assert isinstance(result, list)

    def test_dedup_across_accounts_not_possible(self) -> None:
        """Two different accounts produce different ARNs — no collisions."""
        arn_a = f"arn:aws:ec2:{REGION}:111111111111:instance/i-aaa"
        arn_b = f"arn:aws:ec2:{REGION}:222222222222:instance/i-bbb"
        doc_a = _make_doc(
            arn=arn_a,
            account_id="111111111111",
            account_name="acct-a",
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-aaa",
            display_name="srv-a",
            tags={},
        )
        doc_b = _make_doc(
            arn=arn_b,
            account_id="222222222222",
            account_name="acct-b",
            region=REGION,
            service="ec2",
            resource_type="instance",
            resource_id="i-bbb",
            display_name="srv-b",
            tags={},
        )
        with patch(
            "build_aws_docs._scrape_account",
            side_effect=[{arn_a: doc_a}, {arn_b: doc_b}],
        ):
            result = build_docs(
                [("p1", "acct-a", "111111111111"), ("p2", "acct-b", "222222222222")],
                region=REGION,
            )
        assert len(result) == 2
        external_ids = {d["external_id"] for d in result}
        assert arn_a in external_ids
        assert arn_b in external_ids
